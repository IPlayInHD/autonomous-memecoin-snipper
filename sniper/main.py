"""Orchestrator. Run: python -m sniper.main --config config/default.yaml

Wires detection -> filters -> scoring -> execution -> exits -> risk -> storage
-> dashboard. PAPER is the default run mode; OBSERVE does everything except
execute; SHADOW/LIVE require a wallet and (for live) the double arming gate.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from pathlib import Path
from typing import Optional

from . import constants as C
from .chain.helius import HeliusClient
from .chain.provider import RpcChainDataProvider
from .chain.rpc import RpcClient
from .config import Config, Preregistration, load_config
from .detection.listeners import LaunchDetector
from .exits.engine import ExitEngine
from .exits.lp_monitor import LpMonitor
from .filters.base import FilterContext
from .filters.pipeline import FilterPipeline
from .models import (
    ExitOutcome, FillResult, LaunchEvent, LaunchSource, PoolState, Position,
    RunMode, TriggerMode,
)
from .ntp import check_clock
from .risk.manager import RiskManager
from .storage.db import Database

log = logging.getLogger("sniper")

USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


class Orchestrator:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.run_mode = cfg.run_mode
        self.trigger_mode = cfg.trigger_mode
        self.db = Database(cfg.storage.db_path)
        self.rpc = RpcClient(cfg.rpc.http_url, cfg.rpc.request_timeout_s,
                             cfg.rpc.commitment)
        self.helius = HeliusClient(cfg.rpc.helius_api_key, self.db,
                                   cfg.filters.deployer_cache_ttl_s)
        self.risk = RiskManager(cfg, self.db, self.rpc)
        self.queue: asyncio.Queue[LaunchEvent] = asyncio.Queue(maxsize=500)
        self.detector = LaunchDetector(cfg, self.rpc, self.queue)
        self.clock_trusted = True
        self.paper_executor = None
        self.live_executor = None
        self.jupiter = None
        self.wallet = None
        self.provider: Optional[RpcChainDataProvider] = None
        self.pipeline: Optional[FilterPipeline] = None
        self.exit_engine: Optional[ExitEngine] = None
        self.lp_monitor: Optional[LpMonitor] = None
        self.dashboard = None
        self.prereg: Optional[Preregistration] = None
        self._tasks: list[asyncio.Task] = []

    # ------------------------------------------------------------------ setup
    async def setup(self) -> None:
        cfg = self.cfg
        self._check_clock()
        self._load_preregistration()

        from .execution.jupiter import JupiterClient, make_sell_sim_builder
        from .execution.paper import PaperExecutor
        self.jupiter = JupiterClient(cfg.execution.jupiter_base_url)
        self.provider = RpcChainDataProvider(self.rpc)
        self.provider._sell_sim_builder = make_sell_sim_builder(
            self.jupiter, lambda: self.provider, cfg.execution.slippage_bps)
        self.paper_executor = PaperExecutor(cfg)

        if self.run_mode in (RunMode.SHADOW, RunMode.LIVE):
            from .execution.live import LiveExecutor
            from .execution.wallet import load_wallet
            self.wallet = load_wallet()
            self.live_executor = LiveExecutor(cfg, self.rpc, self.jupiter,
                                              self.wallet, self.run_mode)
            log.info("armed for %s with wallet %s", self.run_mode.value,
                     self.wallet.pubkey)

        threshold = self.prereg.score_threshold if self.prereg else 1.0
        self.pipeline = FilterPipeline(cfg, threshold=threshold)

        self.exit_engine = ExitEngine(
            cfg, pool_fetch=self._pool_for_position, sell_fn=self._sell,
            on_position_update=self._on_position_update,
            amm_fee_bps=cfg.execution.paper.amm_fee_bps)
        self.lp_monitor = LpMonitor(
            cfg.rpc.ws_url, cfg.exits.lp_drop_emergency_pct,
            on_lp_pull=lambda pid: self.exit_engine.poke(pid))

        if cfg.dashboard.enabled:
            from .dashboard.app import DashboardServer
            self.dashboard = DashboardServer(cfg.storage.db_path,
                                             cfg.dashboard.host, cfg.dashboard.port)
        await self._recover_positions()

    def _check_clock(self) -> None:
        if not self.cfg.ntp.enabled:
            self.clock_trusted = False
            log.warning("NTP check disabled — latency data will be flagged untrusted")
            return
        result = check_clock(self.cfg.ntp.server, self.cfg.ntp.max_offset_ms)
        self.clock_trusted = result.ok
        (log.info if result.ok else log.warning)("%s", result.detail)
        if not result.ok and self.cfg.ntp.halt_on_fail:
            raise SystemExit(f"NTP check failed and halt_on_fail is set: {result.detail}")

    def _load_preregistration(self) -> None:
        path = self.cfg.preregistration_file
        if not Path(path).exists():
            if self.run_mode == RunMode.OBSERVE:
                log.warning("no preregistration file (%s); observe mode continues "
                            "without a threshold", path)
                return
            raise SystemExit(
                f"{path} not found. Freeze your score threshold FIRST:\n"
                f"  cp config/preregistration.example.json {path}\n"
                "Edit the threshold, then rerun. It must never be tuned on the "
                "data it is judged on.")
        self.prereg = Preregistration.load(path)
        unchanged = self.db.record_preregistration(self.prereg.sha256, path)
        self.db.set_meta("prereg_current_sha256", self.prereg.sha256)
        if not unchanged:
            log.critical(
                "PREREGISTRATION FILE CHANGED since data collection started — "
                "the evaluation protocol will refuse a GO verdict on this dataset.")
        log.info("pre-registered threshold: %.3f (mode %s, registered %s)",
                 self.prereg.score_threshold, self.prereg.trigger_mode,
                 self.prereg.registered_at)
        if self.prereg.trigger_mode != self.trigger_mode.value:
            log.warning("trigger mode %s differs from pre-registered %s — "
                        "evaluation will treat this dataset as exploratory",
                        self.trigger_mode.value, self.prereg.trigger_mode)

    # -------------------------------------------------------------- recovery
    async def _recover_positions(self) -> None:
        """Chain is the source of truth: reconcile SQLite's open positions
        against actual wallet balances, then resume the exit engine on them."""
        open_positions = self.db.open_positions()
        for pos in open_positions:
            if pos.mode != self.run_mode:
                log.warning("open %s position %s ignored (running %s) — "
                            "restart in that mode to manage it",
                            pos.mode.value, pos.mint[:8], self.run_mode.value)
                continue
            if pos.mode in (RunMode.SHADOW, RunMode.LIVE) and self.wallet:
                try:
                    accounts = await self.rpc.get_token_accounts_by_owner(
                        self.wallet.pubkey, pos.mint)
                    on_chain = sum(
                        int(a["account"]["data"]["parsed"]["info"]
                            ["tokenAmount"]["amount"]) for a in accounts)
                except Exception:  # noqa: BLE001
                    log.exception("reconcile failed for %s; keeping DB amount",
                                  pos.mint[:8])
                    on_chain = pos.tokens_remaining
                if on_chain <= 0:
                    pos.state = "closed"
                    pos.outcome = ExitOutcome.RECONCILED_GONE.value
                    pos.closed_at = time.time()
                    self.db.update_position(pos)
                    log.info("position %s: no tokens on chain; closed as "
                             "reconciled_gone", pos.mint[:8])
                    continue
                if on_chain != pos.tokens_remaining:
                    log.info("position %s: chain=%d db=%d; trusting chain",
                             pos.mint[:8], on_chain, pos.tokens_remaining)
                    pos.tokens_remaining = on_chain
                    self.db.update_position(pos)
            self.exit_engine.add_position(pos)
            log.info("recovered open position %s (%s)", pos.mint[:8],
                     pos.mode.value)

    # ------------------------------------------------------------------- run
    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        self.risk.install_signal_handlers(loop)
        self.risk.start_kill_switch_watch()
        self.exit_engine.start()
        if self.dashboard:
            await self.dashboard.start()
            log.info("dashboard: http://%s:%d/", self.cfg.dashboard.host,
                     self.cfg.dashboard.port)

        self._tasks = [
            asyncio.create_task(self._supervised(self.detector.run(), "detector")),
            asyncio.create_task(self._supervised(self.lp_monitor.run(), "lp-monitor")),
            asyncio.create_task(self._supervised(self._sol_price_loop(), "sol-price")),
        ]
        workers = [asyncio.create_task(
            self._supervised(self._worker(i), f"worker-{i}")) for i in range(4)]
        self._tasks.extend(workers)

        log.info("running: run_mode=%s trigger=%s", self.run_mode.value,
                 self.trigger_mode.value)
        try:
            while not self.risk.halted:
                await asyncio.sleep(1.0)
        finally:
            await self.shutdown()

    async def _supervised(self, coro, name: str) -> None:
        """Halt on unhandled exceptions rather than silently continuing."""
        try:
            await coro
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.exception("task %s crashed", name)
            self.risk.note_unhandled_exception(name, exc)

    async def shutdown(self) -> None:
        log.info("shutting down (halt reason: %s)", self.risk.halt_reason)
        self.detector.stop()
        self.lp_monitor.stop()
        for t in self._tasks:
            t.cancel()
        # wind down open positions before exiting: no orphans
        if self.exit_engine.open_count:
            log.info("closing %d open positions before exit",
                     self.exit_engine.open_count)
            try:
                await self.exit_engine.close_all(ExitOutcome.MANUAL_HALT)
            except Exception:  # noqa: BLE001
                log.exception("close_all failed; positions remain in DB for "
                              "crash recovery on next start")
        await self.exit_engine.stop()
        await self.risk.stop()
        if self.dashboard:
            await self.dashboard.stop()
        if self.jupiter:
            await self.jupiter.close()
        await self.helius.close()
        await self.rpc.close()
        self.db.close()

    # --------------------------------------------------------------- pipeline
    def _relevant(self, event: LaunchEvent) -> bool:
        if self.trigger_mode == TriggerMode.MIGRATION:
            return event.source in (LaunchSource.PUMPFUN_MIGRATION,
                                    LaunchSource.PUMPSWAP_POOL) and (
                event.creator == C.PUMPFUN_MIGRATION_AUTHORITY
                or event.source == LaunchSource.PUMPFUN_MIGRATION)
        if self.trigger_mode == TriggerMode.BLOCK_0:
            return event.source in (LaunchSource.PUMPFUN_LAUNCH,
                                    LaunchSource.PUMPSWAP_POOL,
                                    LaunchSource.RAYDIUM_POOL)
        return True  # filter_edge evaluates everything it hears

    async def _worker(self, _idx: int) -> None:
        while True:
            event = await self.queue.get()
            try:
                await self._process(event)
            except Exception:  # noqa: BLE001 - log; one launch must not kill workers
                log.exception("processing failed for %s", event.mint[:8])
            finally:
                self.queue.task_done()

    async def _process(self, event: LaunchEvent) -> None:
        launch_id = self.db.insert_launch(event)
        if launch_id is None:
            return  # duplicate event (double-buy guard, layer 1)
        log.info("launch %s [%s] slot=%s", event.mint[:8], event.source.value,
                 event.slot)

        tradeable = self._relevant(event)
        if self.trigger_mode == TriggerMode.FILTER_EDGE and tradeable \
                and self.run_mode != RunMode.OBSERVE:
            await asyncio.sleep(self.cfg.trigger.entry_delay_s)

        ctx = FilterContext(self.cfg, self.provider, self.helius)
        outcome = await self.pipeline.run(event, ctx, self.trigger_mode)
        self.db.insert_filter_results(launch_id, outcome.results)
        self.db.insert_pipeline_outcome(launch_id, outcome, self.trigger_mode)

        entered = False
        if outcome.accepted and tradeable and self.run_mode != RunMode.OBSERVE:
            entered = await self._enter(event, launch_id, ctx)
        if not entered:
            self.db.insert_latency(launch_id, event.latency.offsets_ms(),
                                   event.block_time, event.slot, self.clock_trusted)

    # ---------------------------------------------------------------- entries
    def _entry_size_lamports(self) -> int:
        sol = (self.cfg.execution.shadow_size_sol
               if self.run_mode == RunMode.SHADOW
               else self.cfg.execution.entry_size_sol)
        cap_sol = self.cfg.risk.per_trade_cap_usd / self.risk.sol_price_usd
        return int(min(sol, cap_sol) * C.LAMPORTS_PER_SOL)

    async def _enter(self, event: LaunchEvent, launch_id: int,
                     ctx: FilterContext) -> bool:
        if self.db.has_open_position(event.mint, self.run_mode):
            log.info("double-buy guard: already holding %s", event.mint[:8])
            return False
        size = self._entry_size_lamports()
        balance = None
        if self.wallet:
            try:
                balance = await self.rpc.get_balance(self.wallet.pubkey)
            except Exception:  # noqa: BLE001
                log.warning("balance fetch failed; entry blocked this round")
                return False
        ok, reason = self.risk.check_entry(size, self.exit_engine.open_count,
                                           self.run_mode, balance)
        if not ok:
            log.info("entry blocked for %s: %s", event.mint[:8], reason)
            self.db.insert_risk_event("entry_blocked", f"{event.mint}: {reason}")
            return False

        pool = await ctx.pool_state(event)
        if pool is None:
            log.info("no pool state at entry time for %s", event.mint[:8])
            return False

        paper_fill = self.paper_executor.buy(pool, size, self.trigger_mode)
        if self.run_mode == RunMode.PAPER:
            fill = paper_fill
        else:
            fill = await self.live_executor.buy(event, size)

        trade_id = self.db.insert_trade(fill, self.run_mode, None, launch_id,
                                        slot_landed=event.latency.landed_slot)
        if self.run_mode == RunMode.SHADOW:
            self.db.insert_calibration(
                trade_id, paper_fill.fill_probability, fill.filled,
                paper_fill.slippage_bps,
                fill.slippage_bps if fill.filled else None,
                paper_fill.tokens_delta, fill.tokens_delta if fill.filled else None)

        self.db.insert_latency(launch_id, event.latency.offsets_ms(),
                               event.block_time, event.slot, self.clock_trusted)
        if not fill.filled:
            log.info("entry did not fill for %s: %s", event.mint[:8], fill.reason)
            return True  # latency/trade already recorded

        mint_info = await ctx.mint_info(event)
        position = Position(
            id=None, launch_id=launch_id, mint=event.mint, pool=event.pool,
            mode=self.run_mode, trigger_mode=self.trigger_mode,
            tokens_total=fill.tokens_delta, tokens_remaining=fill.tokens_delta,
            sol_spent=-fill.sol_delta, entry_price=fill.effective_price,
            entry_sol_reserve=pool.sol_reserve,
            decimals=mint_info.decimals if mint_info else 6)
        pos_id = self.db.insert_position(position)
        if pos_id is None:
            log.warning("double-buy guard (db) tripped for %s", event.mint[:8])
            return True
        position.id = pos_id
        self.db.execute("UPDATE trades SET position_id=? WHERE id=?",
                        (pos_id, trade_id))
        self.exit_engine.add_position(position)
        self.lp_monitor.watch(position, event.quote_vault, pool.sol_reserve)
        log.info("ENTERED %s: %d tokens for %.4f SOL (%s)", event.mint[:8],
                 fill.tokens_delta, -fill.sol_delta / C.LAMPORTS_PER_SOL,
                 self.run_mode.value)
        return True

    # ----------------------------------------------------------------- exits
    def _launch_event_for(self, position: Position) -> Optional[LaunchEvent]:
        rows = self.db.query("SELECT * FROM launches WHERE id=?",
                             (position.launch_id,))
        if not rows:
            return None
        r = rows[0]
        return LaunchEvent(
            mint=r["mint"], source=LaunchSource(r["source"]),
            signature=r["signature"] or "", slot=r["slot"] or 0,
            creator=r["creator"], pool=r["pool"], base_vault=r["base_vault"],
            quote_vault=r["quote_vault"], lp_mint=r["lp_mint"],
            block_time=r["block_time"])

    async def _pool_for_position(self, position: Position) -> Optional[PoolState]:
        event = self._launch_event_for(position)
        if event is None:
            return None
        try:
            return await self.provider.get_pool_state(event)
        except Exception:  # noqa: BLE001
            log.exception("pool fetch failed for %s", position.mint[:8])
            return None

    async def _sell(self, position: Position, pool: Optional[PoolState],
                    tokens: int, multiplier: float) -> FillResult:
        if position.mode == RunMode.PAPER:
            if pool is None:
                return FillResult(filled=False, side="sell",
                                  reason="could_not_sell: pool unreadable")
            return self.paper_executor.sell(pool, tokens, multiplier)
        event = self._launch_event_for(position)
        if event is None:
            return FillResult(filled=False, side="sell", reason="launch row missing")
        return await self.live_executor.sell(event, tokens, multiplier)

    def _on_position_update(self, position: Position,
                            fill: Optional[FillResult]) -> None:
        self.db.update_position(position)
        if fill is not None:
            self.db.insert_trade(fill, position.mode, position.id,
                                 position.launch_id)

    # ------------------------------------------------------------------- misc
    async def _sol_price_loop(self) -> None:
        while True:
            try:
                quote = await self.jupiter.quote(
                    C.WSOL_MINT, USDC_MINT, C.LAMPORTS_PER_SOL, 50)
                if quote and int(quote.get("outAmount", 0)) > 0:
                    self.risk.sol_price_usd = int(quote["outAmount"]) / 1e6
            except Exception:  # noqa: BLE001 - fallback price stays in effect
                log.debug("sol price refresh failed", exc_info=True)
            await asyncio.sleep(600)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sniper.main")
    parser.add_argument("--config", default="config/default.yaml")
    parser.add_argument("--mode", choices=["observe", "paper", "shadow", "live"],
                        help="override execution.run_mode")
    parser.add_argument("--trigger", choices=["block_0", "migration", "filter_edge"],
                        help="override trigger.mode")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    # never log key material even on DEBUG
    logging.getLogger("sniper.execution.wallet").setLevel(logging.INFO)

    cfg = load_config(args.config)
    if args.mode:
        cfg.execution.run_mode = args.mode
    if args.trigger:
        cfg.trigger.mode = args.trigger
    from .config import validate_config
    import os
    validate_config(cfg, dict(os.environ))

    if not cfg.rpc.http_url or not cfg.rpc.ws_url:
        print("SNIPER_RPC_HTTP_URL / SNIPER_RPC_WS_URL not set (see .env.example)",
              file=sys.stderr)
        return 2

    orch = Orchestrator(cfg)

    async def _run() -> None:
        await orch.setup()
        await orch.run()

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        print("interrupted — open positions persist in SQLite and are "
              "reconciled from chain on next start")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
