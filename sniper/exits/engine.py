"""Fully automatic exit engine. No manual intervention, no exceptions.

Decision logic (`evaluate_exit`) is a pure function so it is unit-testable;
the async engine around it handles polling, LP-pull wakeups, sell retries
with priority-fee escalation, and terminal bookkeeping.

Hard truth encoded here: you cannot stop-loss out of a rug. If liquidity is
pulled the sell fails — that is logged as COULD_NOT_SELL, a distinct outcome
category, never a normal stop-loss loss.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

from ..config import Config
from ..execution import costs
from ..models import ExitOutcome, FillResult, PoolState, Position

log = logging.getLogger(__name__)

PoolFetch = Callable[[Position], Awaitable[Optional[PoolState]]]
SellFn = Callable[[Position, Optional[PoolState], int, float], Awaitable[FillResult]]


@dataclass
class SellOrder:
    tokens: int
    reason: ExitOutcome
    emergency: bool = False
    tier_index: Optional[int] = None   # which TP tier fired, if any


def realizable_multiple(position: Position, pool: PoolState, amm_fee_bps: int) -> float:
    """Value multiple if we sold the REMAINING tokens right now, vs entry cost.
    Uses actual pool depth — a 10x on the chart is not a 10x for your size."""
    if position.tokens_remaining <= 0 or position.entry_price <= 0:
        return 0.0
    sol_out = costs.constant_product_sell(
        position.tokens_remaining, pool.sol_reserve, pool.token_reserve, amm_fee_bps)
    per_token = sol_out / position.tokens_remaining
    return per_token / position.entry_price


def evaluate_exit(position: Position, pool: Optional[PoolState], now: float,
                  cfg: Config, amm_fee_bps: int = 25) -> Optional[SellOrder]:
    """Priority order: LP pull -> max hold -> stop loss -> take-profit tiers."""
    if position.tokens_remaining <= 0:
        return None

    # 1. liquidity pulled / pool unreadable => emergency, sell everything NOW
    if pool is None:
        return SellOrder(tokens=position.tokens_remaining,
                         reason=ExitOutcome.EMERGENCY_LP_PULL, emergency=True)
    drop_trigger = position.entry_sol_reserve * \
        (1.0 - cfg.exits.lp_drop_emergency_pct / 100.0)
    if position.entry_sol_reserve > 0 and pool.sol_reserve < drop_trigger:
        return SellOrder(tokens=position.tokens_remaining,
                         reason=ExitOutcome.EMERGENCY_LP_PULL, emergency=True)

    # 2. max hold force-exit regardless of anything else
    if now - position.opened_at >= cfg.exits.max_hold_s:
        return SellOrder(tokens=position.tokens_remaining,
                         reason=ExitOutcome.MAX_HOLD)

    multiple = realizable_multiple(position, pool, amm_fee_bps)

    # 3. hard stop-loss on realizable value
    if multiple <= 1.0 - cfg.exits.stop_loss_pct / 100.0:
        return SellOrder(tokens=position.tokens_remaining,
                         reason=ExitOutcome.STOP_LOSS)

    # 4. tiered take-profits (e.g. 50% at 2x, 25% at 5x)
    tiers = cfg.exits.tiers
    if position.tiers_filled < len(tiers):
        tier = tiers[position.tiers_filled]
        if multiple >= tier.multiple:
            tokens = min(position.tokens_remaining,
                         int(position.tokens_total * tier.sell_pct / 100.0))
            if tokens > 0:
                return SellOrder(tokens=tokens, reason=ExitOutcome.TAKE_PROFIT,
                                 tier_index=position.tiers_filled)
    return None


class ExitEngine:
    """Owns every open position until it is closed. Survives restarts: the
    orchestrator re-adds reconciled positions and the loop resumes."""

    def __init__(self, cfg: Config, pool_fetch: PoolFetch, sell_fn: SellFn,
                 on_position_update: Callable[[Position, Optional[FillResult]], None],
                 amm_fee_bps: int = 25):
        self.cfg = cfg
        self.pool_fetch = pool_fetch
        self.sell_fn = sell_fn
        self.on_position_update = on_position_update
        self.amm_fee_bps = amm_fee_bps
        self._positions: dict[int, Position] = {}
        self._wakeups: dict[int, asyncio.Event] = {}
        self._task: Optional[asyncio.Task] = None
        self._running = False

    # -- lifecycle ------------------------------------------------------------
    def add_position(self, position: Position) -> None:
        assert position.id is not None
        self._positions[position.id] = position
        self._wakeups[position.id] = asyncio.Event()
        log.info("exit engine tracking position %s (%s, %d tokens)",
                 position.id, position.mint[:8], position.tokens_remaining)

    def poke(self, position_id: int) -> None:
        """External fast-path wakeup (LP monitor saw the vault drain)."""
        ev = self._wakeups.get(position_id)
        if ev:
            ev.set()

    @property
    def open_count(self) -> int:
        return len(self._positions)

    def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._run(), name="exit-engine")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def close_all(self, reason: ExitOutcome = ExitOutcome.MANUAL_HALT) -> None:
        """Force-exit every open position (kill switch / shutdown path)."""
        for pos in list(self._positions.values()):
            pool = await self.pool_fetch(pos)
            await self._execute_sell(
                pos, pool, SellOrder(tokens=pos.tokens_remaining, reason=reason,
                                     emergency=True))

    # -- main loop ---------------------------------------------------------------
    async def _run(self) -> None:
        while self._running:
            for pos in list(self._positions.values()):
                try:
                    await self._tick(pos)
                except Exception:  # noqa: BLE001 - one position must not stall others
                    log.exception("exit tick failed for position %s", pos.id)
            await self._sleep_until_poke(self.cfg.exits.poll_interval_s)

    async def _sleep_until_poke(self, seconds: float) -> None:
        events = list(self._wakeups.values())
        if not events:
            await asyncio.sleep(seconds)
            return
        waiters = [asyncio.create_task(e.wait()) for e in events]
        done, pending = await asyncio.wait(waiters, timeout=seconds,
                                           return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()
        for e in events:
            e.clear()

    async def _tick(self, position: Position) -> None:
        pool = await self.pool_fetch(position)
        order = evaluate_exit(position, pool, time.time(), self.cfg, self.amm_fee_bps)
        if order:
            await self._execute_sell(position, pool, order)

    # -- selling with fee escalation ------------------------------------------------
    async def _execute_sell(self, position: Position, pool: Optional[PoolState],
                            order: SellOrder) -> None:
        """In a dump everyone exits at once and priority fees spike; a fixed fee
        means the sell never lands. Walk the escalation ladder; emergencies jump
        straight to the top rung. Blockhash refresh is per-attempt (each retry
        rebuilds the transaction)."""
        ladder = self.cfg.exits.fee_escalation or [1.0]
        attempts = ([ladder[-1]] * min(3, len(ladder))) if order.emergency else ladder
        fill: Optional[FillResult] = None

        for i, multiplier in enumerate(attempts):
            if pool is None:
                pool = await self.pool_fetch(position)
            fill = await self.sell_fn(position, pool, order.tokens, multiplier)
            if fill.filled:
                break
            log.warning("sell attempt %d/%d failed for %s (fee x%.0f): %s",
                        i + 1, len(attempts), position.mint[:8], multiplier,
                        fill.reason)
            if i < len(attempts) - 1:
                await asyncio.sleep(self.cfg.exits.sell_retry_delay_s)
                pool = None  # refetch next attempt

        if fill and fill.filled:
            position.tokens_remaining += fill.tokens_delta  # tokens_delta < 0
            position.sol_received += max(0, fill.sol_delta)
            if order.tier_index is not None:
                position.tiers_filled = order.tier_index + 1
            terminal = order.reason in (
                ExitOutcome.STOP_LOSS, ExitOutcome.MAX_HOLD,
                ExitOutcome.EMERGENCY_LP_PULL, ExitOutcome.MANUAL_HALT)
            if position.tokens_remaining <= 0 or terminal:
                self._close(position, order.reason.value)
            self.on_position_update(position, fill)
        else:
            # ladder exhausted and the token still can't be sold: that IS the
            # rug outcome. Position closed, tokens worthless, logged distinctly.
            self._close(position, ExitOutcome.COULD_NOT_SELL.value)
            self.on_position_update(position, fill)

    def _close(self, position: Position, outcome: str) -> None:
        position.state = "closed"
        position.outcome = outcome
        position.closed_at = time.time()
        self._positions.pop(position.id or -1, None)
        self._wakeups.pop(position.id or -1, None)
        pnl = position.net_pnl_lamports / 1e9
        log.info("position %s closed: %s, net P&L %.6f SOL", position.mint[:8],
                 outcome, pnl)
