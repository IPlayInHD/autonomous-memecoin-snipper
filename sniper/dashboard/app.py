"""Read-only localhost dashboard.

Security posture:
- binds to 127.0.0.1 ONLY (config validation refuses anything else)
- registers GET routes exclusively; there is no mutating endpoint
- opens SQLite in read-only mode (`file:...?mode=ro`) per request, so even a
  bug here cannot write to the trade log

Shows: every launch seen, every filter rejection reason, paper/live P&L with
the full distribution (median + left tail, not just win rate), latency
percentiles p50/p95/p99 across the whole budget, and open positions.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

from aiohttp import web

from .. import constants as C


def _percentiles(values: list[float], pcts: tuple[float, ...] = (50, 95, 99)) -> dict:
    if not values:
        return {f"p{int(p)}": None for p in pcts}
    data = sorted(values)
    out = {}
    for p in pcts:
        k = (len(data) - 1) * p / 100.0
        lo, hi = int(k), min(int(k) + 1, len(data) - 1)
        out[f"p{int(p)}"] = round(data[lo] + (data[hi] - data[lo]) * (k - lo), 1)
    return out


class DashboardServer:
    def __init__(self, db_path: str, host: str, port: int):
        if host not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError("dashboard must bind to localhost only")
        self.db_path = db_path
        self.host = host
        self.port = port
        self.app = web.Application()
        self.app.router.add_get("/", self._index)
        self.app.router.add_get("/api/summary", self._summary)
        self.app.router.add_get("/api/pnl", self._pnl)
        self.app.router.add_get("/api/rejections", self._rejections)
        self.app.router.add_get("/api/latency", self._latency)
        self.app.router.add_get("/api/positions", self._positions)
        self.app.router.add_get("/api/launches", self._launches)
        self._runner: Optional[web.AppRunner] = None

    def _conn(self) -> sqlite3.Connection:
        uri = f"file:{Path(self.db_path).as_posix()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
        return conn

    def _rows(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        with self._conn() as conn:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]

    async def start(self) -> None:
        self._runner = web.AppRunner(self.app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.host, self.port)
        await site.start()

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()

    # -- API -------------------------------------------------------------------
    async def _summary(self, _req: web.Request) -> web.Response:
        launches = self._rows("SELECT COUNT(*) c FROM launches")[0]["c"]
        accepted = self._rows(
            "SELECT COUNT(*) c FROM pipeline_outcomes WHERE accepted=1")[0]["c"]
        evaluated = self._rows("SELECT COUNT(*) c FROM pipeline_outcomes")[0]["c"]
        pnls = [r["pnl"] / C.LAMPORTS_PER_SOL for r in self._rows(
            "SELECT sol_received - sol_spent pnl FROM positions WHERE state='closed'")]
        open_pos = self._rows("SELECT COUNT(*) c FROM positions WHERE state='open'")[0]["c"]
        outcomes = {r["outcome"] or "?": r["c"] for r in self._rows(
            "SELECT outcome, COUNT(*) c FROM positions WHERE state='closed'"
            " GROUP BY outcome")}
        fees = self._rows(
            "SELECT COALESCE(SUM(fee_base+fee_priority+fee_jito+fee_route),0) f"
            " FROM trades")[0]["f"]
        wins = sum(1 for p in pnls if p > 0)
        pnls_sorted = sorted(pnls)
        return web.json_response({
            "launches_seen": launches,
            "evaluated": evaluated,
            "accepted": accepted,
            "open_positions": open_pos,
            "closed_trades": len(pnls),
            "win_rate": round(wins / len(pnls), 4) if pnls else None,
            "net_pnl_sol": round(sum(pnls), 6),
            "expectancy_sol": round(sum(pnls) / len(pnls), 6) if pnls else None,
            "median_pnl_sol": round(pnls_sorted[len(pnls) // 2], 6) if pnls else None,
            "p5_pnl_sol": round(pnls_sorted[max(0, int(len(pnls) * 0.05) - 1)], 6)
            if pnls else None,
            "outcomes": outcomes,
            "total_fees_sol": round(fees / C.LAMPORTS_PER_SOL, 6),
            "generated_at": time.time(),
        })

    async def _pnl(self, _req: web.Request) -> web.Response:
        rows = self._rows(
            "SELECT closed_at, mode, outcome, sol_received - sol_spent pnl"
            " FROM positions WHERE state='closed' ORDER BY closed_at")
        pnls = [r["pnl"] / C.LAMPORTS_PER_SOL for r in rows]
        return web.json_response({"trades": rows, "pnls_sol": pnls})

    async def _rejections(self, _req: web.Request) -> web.Response:
        rows = self._rows(
            "SELECT name || ': ' || COALESCE(reason,'') reason, COUNT(*) c"
            " FROM filter_results WHERE passed=0 AND skipped=0"
            " GROUP BY name, reason ORDER BY c DESC LIMIT 25")
        by_filter = self._rows(
            "SELECT name, COUNT(*) c FROM filter_results"
            " WHERE passed=0 AND skipped=0 GROUP BY name ORDER BY c DESC")
        return web.json_response({"detailed": rows, "by_filter": by_filter})

    async def _latency(self, _req: web.Request) -> web.Response:
        out: dict[str, Any] = {}
        for stage in ("event_seen_ms", "filters_done_ms", "tx_built_ms",
                      "tx_sent_ms", "tx_landed_ms"):
            vals = [r["v"] for r in self._rows(
                f"SELECT {stage} v FROM latency_samples WHERE {stage} IS NOT NULL")]
            out[stage] = {"n": len(vals), **_percentiles(vals)}
        slots = [r["v"] for r in self._rows(
            "SELECT slot_delta v FROM latency_samples WHERE slot_delta IS NOT NULL")]
        out["slot_delta"] = {"n": len(slots), **_percentiles(slots)}
        untrusted = self._rows(
            "SELECT COUNT(*) c FROM latency_samples WHERE clock_trusted=0")[0]["c"]
        out["untrusted_clock_samples"] = untrusted
        return web.json_response(out)

    async def _positions(self, _req: web.Request) -> web.Response:
        return web.json_response(self._rows(
            "SELECT id, mint, mode, trigger_mode, state, outcome, tokens_remaining,"
            " sol_spent, sol_received, opened_at, closed_at FROM positions"
            " ORDER BY opened_at DESC LIMIT 100"))

    async def _launches(self, _req: web.Request) -> web.Response:
        return web.json_response(self._rows(
            "SELECT l.id, l.mint, l.source, l.slot, l.detected_wall,"
            " po.score, po.accepted, po.rejected_by"
            " FROM launches l LEFT JOIN pipeline_outcomes po ON po.launch_id = l.id"
            " ORDER BY l.id DESC LIMIT 100"))

    async def _index(self, _req: web.Request) -> web.Response:
        return web.Response(text=_HTML, content_type="text/html")


# --------------------------------------------------------------------------
# Single-file front end. Palette: validated reference instance (dataviz skill,
# references/palette.md) — series blue for magnitude, blue<->red diverging pair
# for P&L sign, chrome/ink tokens verbatim, light + dark selected explicitly.
# --------------------------------------------------------------------------

_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>sniper lab — read-only</title>
<style>
:root{
  --page:#f9f9f7; --surface:#fcfcfb; --ink:#0b0b0b; --ink2:#52514e;
  --muted:#898781; --grid:#e1e0d9; --axis:#c3c2b7;
  --border:rgba(11,11,11,.10);
  --pos:#2a78d6; --neg:#e34948; --good:#006300; --crit:#d03b3b;
}
@media (prefers-color-scheme: dark){:root{
  --page:#0d0d0d; --surface:#1a1a19; --ink:#ffffff; --ink2:#c3c2b7;
  --muted:#898781; --grid:#2c2c2a; --axis:#383835;
  --border:rgba(255,255,255,.10);
  --pos:#3987e5; --neg:#e66767; --good:#0ca30c; --crit:#d03b3b;
}}
*{box-sizing:border-box;margin:0}
body{background:var(--page);color:var(--ink);
  font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif;padding:20px}
h1{font-size:17px;font-weight:650;margin-bottom:2px}
.sub{color:var(--muted);font-size:12px;margin-bottom:18px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));
  gap:10px;margin-bottom:18px}
.tile{background:var(--surface);border:1px solid var(--border);border-radius:8px;
  padding:12px 14px}
.tile .k{color:var(--ink2);font-size:11px;text-transform:uppercase;
  letter-spacing:.04em}
.tile .v{font-size:24px;font-weight:650;margin-top:2px}
.tile .v.pos{color:var(--good)} .tile .v.neg{color:var(--crit)}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-bottom:14px}
@media(max-width:900px){.grid2{grid-template-columns:1fr}}
.card{background:var(--surface);border:1px solid var(--border);border-radius:8px;
  padding:14px;overflow-x:auto}
.card h2{font-size:13px;font-weight:650;margin-bottom:10px;color:var(--ink2)}
table{border-collapse:collapse;width:100%;font-size:12.5px}
th{color:var(--muted);text-align:left;font-weight:500;padding:4px 8px;
  border-bottom:1px solid var(--grid)}
td{padding:4px 8px;border-bottom:1px solid var(--grid);
  font-variant-numeric:tabular-nums}
td.r,th.r{text-align:right}
.bar-row{display:flex;align-items:center;gap:8px;margin:3px 0}
.bar-row .lbl{flex:0 0 46%;font-size:12px;color:var(--ink2);overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap}
.bar-row .bar{height:10px;border-radius:0 4px 4px 0;background:var(--pos);
  min-width:2px}
.bar-row .n{font-size:12px;color:var(--ink2);font-variant-numeric:tabular-nums}
svg text{fill:var(--muted);font:10.5px system-ui,sans-serif}
.tooltip{position:fixed;pointer-events:none;background:var(--surface);
  border:1px solid var(--border);border-radius:6px;padding:6px 9px;
  font-size:12px;box-shadow:0 2px 8px rgba(0,0,0,.15);display:none;z-index:9}
.tag{display:inline-block;padding:1px 7px;border-radius:9px;font-size:11px;
  border:1px solid var(--border)}
.tag.reject{color:var(--crit)} .tag.ok{color:var(--good)}
.empty{color:var(--muted);font-size:12px;padding:12px 0}
</style></head><body>
<h1>memecoin sniper lab</h1>
<div class="sub">read-only &middot; localhost &middot; auto-refresh 10s &middot;
paper P&amp;L is an optimistic upper bound</div>
<div class="tiles" id="tiles"></div>
<div class="grid2">
 <div class="card"><h2>Net P&amp;L per trade — distribution (SOL)</h2>
   <div id="hist"></div></div>
 <div class="card"><h2>Filter rejections</h2><div id="rej"></div></div>
</div>
<div class="grid2">
 <div class="card"><h2>Latency budget percentiles (ms after t0 = pool-creation
   block time)</h2><div id="lat"></div></div>
 <div class="card"><h2>Exit outcomes</h2><div id="outcomes"></div></div>
</div>
<div class="card" style="margin-bottom:14px"><h2>Recent positions</h2>
  <div id="positions"></div></div>
<div class="card"><h2>Recent launches</h2><div id="launches"></div></div>
<div class="tooltip" id="tt"></div>
<script>
const $=id=>document.getElementById(id);
const fmt=(x,d=4)=>x==null?'—':Number(x).toFixed(d);
const ts=t=>t?new Date(t*1000).toISOString().replace('T',' ').slice(5,19):'—';
const tt=$('tt');
function showTT(e,html){tt.innerHTML=html;tt.style.display='block';
  tt.style.left=(e.clientX+12)+'px';tt.style.top=(e.clientY+12)+'px';}
function hideTT(){tt.style.display='none';}

async function j(u){const r=await fetch(u);return r.json();}

function tiles(s){
  const t=[['launches seen',s.launches_seen],['evaluated',s.evaluated],
    ['accepted',s.accepted],['open',s.open_positions],
    ['closed trades',s.closed_trades],
    ['win rate',s.win_rate==null?'—':(100*s.win_rate).toFixed(1)+'%'],
    ['expectancy / trade',fmt(s.expectancy_sol,5),s.expectancy_sol],
    ['median / trade',fmt(s.median_pnl_sol,5),s.median_pnl_sol],
    ['net P&L (SOL)',fmt(s.net_pnl_sol,4),s.net_pnl_sol],
    ['fees paid (SOL)',fmt(s.total_fees_sol,4)]];
  $('tiles').innerHTML=t.map(([k,v,sign])=>`<div class="tile"><div class="k">${k}</div>
    <div class="v ${sign>0?'pos':sign<0?'neg':''}">${v}</div></div>`).join('');
}

function hist(pnls){
  const el=$('hist');
  if(!pnls.length){el.innerHTML='<div class="empty">no closed trades yet</div>';return;}
  const lo=Math.min(...pnls), hi=Math.max(...pnls);
  const span=(hi-lo)||1e-9, nb=Math.min(31,Math.max(9,Math.round(Math.sqrt(pnls.length)*2)));
  const w=Math.max(360,el.clientWidth||520), h=170, pad={l:8,r:8,t:8,b:26};
  const bins=Array(nb).fill(0);
  pnls.forEach(p=>{bins[Math.min(nb-1,Math.floor((p-lo)/span*nb))]++;});
  const bw=(w-pad.l-pad.r)/nb, maxc=Math.max(...bins);
  let bars='';
  bins.forEach((c,i)=>{
    const x0=lo+i*span/nb, x1=lo+(i+1)*span/nb;
    const bh=c/maxc*(h-pad.t-pad.b);
    const x=pad.l+i*bw+1, y=h-pad.b-bh, bwid=Math.max(1,bw-2);
    const col=(x1<=0)?'var(--neg)':(x0>=0)?'var(--pos)':'var(--muted)';
    const r=Math.min(3,bwid/2,bh);
    bars+=`<path d="M${x},${h-pad.b} L${x},${y+r} Q${x},${y} ${x+r},${y}
      L${x+bwid-r},${y} Q${x+bwid},${y} ${x+bwid},${y+r} L${x+bwid},${h-pad.b} Z"
      fill="${col}" data-n="${c}" data-a="${x0.toFixed(5)}" data-b="${x1.toFixed(5)}"/>`;
  });
  const zx=pad.l+(0-lo)/span*(w-pad.l-pad.r);
  const zero=(lo<0&&hi>0)?`<line x1="${zx}" y1="${pad.t}" x2="${zx}"
    y2="${h-pad.b}" stroke="var(--axis)" stroke-dasharray="3 3"/>`:'';
  el.innerHTML=`<svg viewBox="0 0 ${w} ${h}" width="100%" role="img"
    aria-label="P&L histogram">
    <line x1="${pad.l}" y1="${h-pad.b}" x2="${w-pad.r}" y2="${h-pad.b}"
      stroke="var(--axis)"/>${zero}${bars}
    <text x="${pad.l}" y="${h-8}">${lo.toFixed(4)}</text>
    <text x="${w-pad.r}" y="${h-8}" text-anchor="end">${hi.toFixed(4)}</text>
    <text x="${zx||pad.l}" y="${h-8}" text-anchor="middle">${(lo<0&&hi>0)?'0':''}</text>
  </svg>`;
  el.querySelectorAll('path').forEach(p=>{
    p.addEventListener('mousemove',e=>showTT(e,
      `<b>${p.dataset.n}</b> trades<br>${p.dataset.a} … ${p.dataset.b} SOL`));
    p.addEventListener('mouseleave',hideTT);
  });
}

function barlist(el,rows,kName,kVal){
  if(!rows.length){el.innerHTML='<div class="empty">nothing yet</div>';return;}
  const max=Math.max(...rows.map(r=>r[kVal]));
  el.innerHTML=rows.map(r=>`<div class="bar-row">
    <div class="lbl" title="${r[kName]}">${r[kName]}</div>
    <div class="bar" style="width:${Math.max(2,58*r[kVal]/max)}%"></div>
    <div class="n">${r[kVal]}</div></div>`).join('');
}

function latTable(d){
  const stages=['event_seen_ms','filters_done_ms','tx_built_ms','tx_sent_ms',
    'tx_landed_ms','slot_delta'];
  let rows=stages.map(s=>{const v=d[s]||{};return `<tr><td>${s.replace('_ms','')}</td>
    <td class="r">${v.n||0}</td><td class="r">${fmt(v.p50,1)}</td>
    <td class="r">${fmt(v.p95,1)}</td><td class="r">${fmt(v.p99,1)}</td></tr>`;}).join('');
  const warn=d.untrusted_clock_samples?`<div class="empty">⚠ ${d.untrusted_clock_samples}
    samples taken with UNTRUSTED clock (NTP offset out of bound)</div>`:'';
  $('lat').innerHTML=`<table><tr><th>stage</th><th class="r">n</th>
    <th class="r">p50</th><th class="r">p95</th><th class="r">p99</th></tr>${rows}</table>${warn}`;
}

function outcomes(s){
  const o=s.outcomes||{}; const rows=Object.entries(o).sort((a,b)=>b[1]-a[1]);
  if(!rows.length){$('outcomes').innerHTML='<div class="empty">no closed positions yet</div>';return;}
  $('outcomes').innerHTML='<table>'+rows.map(([k,v])=>
    `<tr><td>${k==='could_not_sell'?'⚠ '+k+' (rug — not a normal loss)':k}</td>
     <td class="r">${v}</td></tr>`).join('')+'</table>';
}

function positions(rows){
  if(!rows.length){$('positions').innerHTML='<div class="empty">none yet</div>';return;}
  $('positions').innerHTML=`<table><tr><th>mint</th><th>mode</th><th>state</th>
   <th>outcome</th><th class="r">spent</th><th class="r">received</th>
   <th class="r">net SOL</th><th>opened</th></tr>`+rows.map(p=>{
    const net=(p.sol_received-p.sol_spent)/1e9;
    return `<tr><td>${p.mint.slice(0,8)}…</td><td>${p.mode}</td><td>${p.state}</td>
     <td>${p.outcome||''}</td><td class="r">${fmt(p.sol_spent/1e9)}</td>
     <td class="r">${fmt(p.sol_received/1e9)}</td>
     <td class="r" style="color:${net>0?'var(--good)':net<0?'var(--crit)':'inherit'}">
     ${fmt(net,5)}</td><td>${ts(p.opened_at)}</td></tr>`;}).join('')+'</table>';
}

function launches(rows){
  if(!rows.length){$('launches').innerHTML='<div class="empty">waiting for the firehose…</div>';return;}
  $('launches').innerHTML=`<table><tr><th>mint</th><th>source</th>
   <th class="r">slot</th><th class="r">score</th><th>verdict</th><th>seen</th></tr>`+
   rows.map(l=>`<tr><td>${l.mint.slice(0,8)}…</td><td>${l.source}</td>
    <td class="r">${l.slot??''}</td><td class="r">${l.score==null?'—':l.score.toFixed(3)}</td>
    <td>${l.accepted==null?'<span class="tag">observed</span>':l.accepted?
     '<span class="tag ok">accepted</span>':
     `<span class="tag reject">${l.rejected_by||'below threshold'}</span>`}</td>
    <td>${ts(l.detected_wall)}</td></tr>`).join('')+'</table>';
}

async function refresh(){
  try{
    const [s,p,r,l,pos,lau]=await Promise.all([j('/api/summary'),j('/api/pnl'),
      j('/api/rejections'),j('/api/latency'),j('/api/positions'),j('/api/launches')]);
    tiles(s);hist(p.pnls_sol);barlist($('rej'),r.by_filter,'name','c');
    latTable(l);outcomes(s);positions(pos);launches(lau);
  }catch(e){console.error(e);}
}
refresh();setInterval(refresh,10000);
</script></body></html>
"""
