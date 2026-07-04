"""Risk layer: the part of the system whose job is to say NO.

Independent brakes (any one of them stops new entries):
- hard per-trade cap in USD
- max concurrent positions
- daily loss kill switch (realized P&L, per UTC day)
- daily FEE-SPEND cap — priority fees bleed capital even with zero fills
- SOL floor reserve — never entry-spend into being unable to pay exit fees
- RPC latency degradation halt (p95 over the rolling window)
- external kill switch: sentinel file OR SIGUSR1/SIGTERM
- unhandled-exception halt: crash loud, never trade silently broken
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import signal
import time
from pathlib import Path
from typing import Optional

from .. import constants as C
from ..chain.rpc import RpcClient
from ..config import Config
from ..models import RunMode
from ..storage.db import Database

log = logging.getLogger(__name__)


class RiskManager:
    def __init__(self, cfg: Config, db: Database, rpc: Optional[RpcClient] = None):
        self.cfg = cfg
        self.db = db
        self.rpc = rpc
        self.halted = False
        self.halt_reason: Optional[str] = None
        self.entries_halted = False        # soft halt: exits keep running
        self.sol_price_usd = cfg.risk.sol_price_usd_fallback
        self._watch_task: Optional[asyncio.Task] = None

    # -- global halt ------------------------------------------------------------
    def halt(self, reason: str, hard: bool = True) -> None:
        if hard:
            self.halted = True
        self.entries_halted = True
        self.halt_reason = reason
        self.db.insert_risk_event("halt" if hard else "entries_halted", reason)
        log.critical("RISK HALT (%s): %s", "hard" if hard else "entries", reason)

    def resume_entries(self, reason: str) -> None:
        if self.halted:
            return  # hard halts don't auto-resume
        self.entries_halted = False
        self.db.insert_risk_event("entries_resumed", reason)

    def note_unhandled_exception(self, where: str, exc: BaseException) -> None:
        if self.cfg.risk.halt_on_unhandled_exception:
            self.halt(f"unhandled exception in {where}: {exc!r}")

    # -- entry gate ----------------------------------------------------------------
    def check_entry(self, size_lamports: int, open_positions: int, mode: RunMode,
                    wallet_balance_lamports: Optional[int] = None) -> tuple[bool, str]:
        r = self.cfg.risk
        if self.halted:
            return False, f"halted: {self.halt_reason}"
        if self.entries_halted:
            return False, f"entries halted: {self.halt_reason}"
        if Path(r.kill_switch_file).exists():
            self.halt(f"kill switch file present: {r.kill_switch_file}")
            return False, "kill switch file"

        size_usd = size_lamports / C.LAMPORTS_PER_SOL * self.sol_price_usd
        if size_usd > r.per_trade_cap_usd * 1.001:
            return False, (f"size ${size_usd:.2f} > per-trade cap "
                           f"${r.per_trade_cap_usd:.2f}")
        if open_positions >= r.max_concurrent_positions:
            return False, f"max concurrent positions ({r.max_concurrent_positions})"

        day_start = _utc_day_start()
        pnl = self.db.realized_pnl_since(day_start, mode)
        pnl_usd = pnl / C.LAMPORTS_PER_SOL * self.sol_price_usd
        if pnl_usd <= -r.daily_loss_cap_usd:
            self.halt(f"daily loss cap hit: {pnl_usd:.2f} USD "
                      f"(cap {r.daily_loss_cap_usd})", hard=False)
            return False, "daily loss cap"

        fee_modes = ("paper",) if mode == RunMode.PAPER else ("shadow", "live")
        fees = self.db.fees_spent_since(day_start, fee_modes)
        if fees / C.LAMPORTS_PER_SOL >= r.daily_fee_cap_sol:
            self.halt(f"daily fee cap hit: {fees / 1e9:.4f} SOL "
                      f"(cap {r.daily_fee_cap_sol})", hard=False)
            return False, "daily fee cap"

        if wallet_balance_lamports is not None:
            floor = int(r.sol_floor * C.LAMPORTS_PER_SOL)
            if wallet_balance_lamports - size_lamports < floor:
                return False, (f"SOL floor: balance {wallet_balance_lamports / 1e9:.4f}"
                               f" - entry would breach {r.sol_floor} SOL reserve")

        if self.rpc is not None:
            p95 = self.rpc.latency_percentile(95)
            if p95 is not None and p95 > self.cfg.rpc.max_latency_ms_halt:
                self.halt(f"RPC degraded: p95 {p95:.0f} ms > "
                          f"{self.cfg.rpc.max_latency_ms_halt:.0f} ms", hard=False)
                return False, "rpc latency degraded"
            if p95 is not None and self.entries_halted and \
                    p95 < self.cfg.rpc.max_latency_ms_halt * 0.5:
                self.resume_entries("rpc latency recovered")

        return True, "ok"

    # -- position sizing (live only, if ever) -----------------------------------------
    def kelly_size_lamports(self, bankroll_lamports: int, win_rate: float,
                            avg_win: float, avg_loss: float) -> int:
        """Heavily discounted fractional Kelly, then capped by the per-trade cap.
        avg_win/avg_loss in the same (positive) units. Returns 0 when edge <= 0 —
        no positive edge, no position, regardless of what the caps would allow."""
        if avg_loss <= 0 or avg_win <= 0 or not (0 < win_rate < 1):
            return 0
        b = avg_win / avg_loss
        kelly = win_rate - (1 - win_rate) / b
        if kelly <= 0:
            return 0
        frac = kelly * self.cfg.risk.kelly_discount
        cap = int(self.cfg.risk.per_trade_cap_usd / self.sol_price_usd
                  * C.LAMPORTS_PER_SOL)
        return min(int(bankroll_lamports * frac), cap)

    # -- external kill switch -------------------------------------------------------
    def install_signal_handlers(self, loop: asyncio.AbstractEventLoop) -> None:
        for sig in (signal.SIGTERM, signal.SIGUSR1):
            try:
                loop.add_signal_handler(
                    sig, lambda s=sig: self.halt(f"signal {s.name} received"))
            except NotImplementedError:  # non-unix
                pass

    def start_kill_switch_watch(self, interval_s: float = 2.0) -> None:
        async def watch() -> None:
            path = Path(self.cfg.risk.kill_switch_file)
            while True:
                if path.exists() and not self.halted:
                    self.halt(f"kill switch file present: {path}")
                await asyncio.sleep(interval_s)
        self._watch_task = asyncio.create_task(watch(), name="kill-switch-watch")

    async def stop(self) -> None:
        if self._watch_task:
            self._watch_task.cancel()
            try:
                await self._watch_task
            except asyncio.CancelledError:
                pass


def _utc_day_start() -> float:
    now = dt.datetime.now(dt.timezone.utc)
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return day.timestamp()
