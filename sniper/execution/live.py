"""Live/shadow execution via the official Jupiter API.

Arming rules:
- SHADOW: real trades hard-clamped to `shadow_size_sol` (0.01–0.05 SOL). Their
  only purpose is calibration: measure real fill rate + realized slippage vs
  what the paper model predicted for the identical moment.
- LIVE: requires config `live_enabled: true` AND the env acknowledgment. The
  constructor re-checks both; config validation already refused once.

Every real trade records the full latency budget legs (tx_built, tx_sent,
tx_landed + slot delta) — submit-to-land is where money actually dies.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import time
from typing import Any, Optional

from .. import constants as C
from ..config import Config
from ..chain.rpc import RpcClient, RpcError
from ..models import FeeBreakdown, FillResult, LaunchEvent, RunMode
from . import costs
from .jupiter import JupiterClient
from .wallet import Wallet

log = logging.getLogger(__name__)


class LiveExecutor:
    def __init__(self, cfg: Config, rpc: RpcClient, jupiter: JupiterClient,
                 wallet: Wallet, mode: RunMode):
        if mode not in (RunMode.SHADOW, RunMode.LIVE):
            raise ValueError(f"LiveExecutor refuses mode {mode}")
        if mode == RunMode.LIVE:
            ack = os.environ.get(cfg.execution.live_ack_env, "")
            if not (cfg.execution.live_enabled and ack == cfg.execution.live_ack_value):
                raise ValueError("live mode not armed (config flag + env ack required)")
        self.cfg = cfg
        self.rpc = rpc
        self.jupiter = jupiter
        self.wallet = wallet
        self.mode = mode

    def entry_size_lamports(self) -> int:
        sol = (self.cfg.execution.shadow_size_sol if self.mode == RunMode.SHADOW
               else self.cfg.execution.entry_size_sol)
        return int(sol * C.LAMPORTS_PER_SOL)

    # -- swaps ---------------------------------------------------------------
    async def buy(self, event: LaunchEvent, sol_in: int) -> FillResult:
        sol_in = min(sol_in, self.entry_size_lamports())
        return await self._swap(event, C.WSOL_MINT, event.mint, sol_in, "buy",
                                priority_multiplier=1.0)

    async def sell(self, event: LaunchEvent, tokens: int,
                   priority_multiplier: float = 1.0) -> FillResult:
        return await self._swap(event, event.mint, C.WSOL_MINT, tokens, "sell",
                                priority_multiplier=priority_multiplier)

    async def _swap(self, event: LaunchEvent, input_mint: str, output_mint: str,
                    amount: int, side: str, priority_multiplier: float) -> FillResult:
        ex = self.cfg.execution
        prio_lamports = int(costs.priority_fee_lamports(
            ex.priority_fee_microlamports, ex.compute_units) * priority_multiplier)
        fees = FeeBreakdown(base_fee=C.BASE_FEE_LAMPORTS, priority_fee=prio_lamports,
                            jito_tip=ex.jito.tip_lamports if ex.jito.enabled else 0)

        quote = await self.jupiter.quote(input_mint, output_mint, amount,
                                         ex.slippage_bps)
        if quote is None:
            return FillResult(filled=False, side=side, fees=FeeBreakdown(),
                              reason="no route")
        tax = JupiterClient.buy_tax_bps(quote)
        max_tax = (self.cfg.filters.max_buy_tax_bps if side == "buy"
                   else self.cfg.filters.max_sell_tax_bps)
        if side == "buy" and tax is not None and tax > max_tax:
            return FillResult(filled=False, side=side, fees=FeeBreakdown(),
                              reason=f"quote tax/impact {tax} bps > max {max_tax}")

        tx_b64 = await self.jupiter.swap_transaction(
            quote, self.wallet.pubkey, priority_fee_lamports=prio_lamports,
            jito_tip_lamports=fees.jito_tip)
        if tx_b64 is None:
            return FillResult(filled=False, side=side, fees=FeeBreakdown(),
                              reason="swap build failed")
        signed = self.wallet.sign_versioned_tx_b64(tx_b64)
        event.latency.tx_built = time.time()

        try:
            signature = await self._send(signed)
        except RpcError as exc:
            return FillResult(filled=False, side=side,
                              sol_delta=0, fees=FeeBreakdown(),
                              reason=f"send failed: {exc}")
        event.latency.tx_sent = time.time()

        landed = await self._confirm(signature,
                                     timeout_s=self.cfg.exits.blockhash_max_age_s + 30)
        if landed is None:
            # blockhash likely expired unconfirmed; fee not necessarily paid,
            # but we book the attempt cost pessimistically
            return FillResult(filled=False, side=side, sol_delta=-fees.base_fee,
                              fees=FeeBreakdown(base_fee=fees.base_fee),
                              reason="unconfirmed before blockhash expiry",
                              signature=signature)
        event.latency.tx_landed = time.time()
        event.latency.landed_slot = landed.get("slot")

        if landed.get("err") is not None:
            return FillResult(filled=False, side=side, sol_delta=-fees.total_unrecoverable,
                              fees=fees, reason=f"tx reverted: {landed['err']}",
                              signature=signature)
        return await self._fill_from_chain(signature, side, fees, quote)

    async def _send(self, signed_b64: str) -> str:
        jito = self.cfg.execution.jito
        if jito.enabled:
            sig = await self._send_jito(signed_b64)
            if sig:
                return sig
            log.warning("jito submission failed; falling back to RPC send")
        return await self.rpc.send_transaction(signed_b64)

    async def _send_jito(self, signed_b64: str) -> Optional[str]:
        import aiohttp
        url = f"{self.cfg.execution.jito.block_engine_url}/api/v1/transactions"
        payload = {"jsonrpc": "2.0", "id": 1, "method": "sendTransaction",
                   "params": [signed_b64, {"encoding": "base64"}]}
        try:
            async with aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=5)) as session:
                async with session.post(url, json=payload) as resp:
                    body = await resp.json(content_type=None)
            return body.get("result")
        except Exception as exc:  # noqa: BLE001 - jito is optional best-effort
            log.debug("jito send error: %s", exc)
            return None

    async def _confirm(self, signature: str, timeout_s: float) -> Optional[dict[str, Any]]:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            statuses = await self.rpc.get_signature_statuses([signature])
            st = statuses[0] if statuses else None
            if st and st.get("confirmationStatus") in ("confirmed", "finalized"):
                return st
            await asyncio.sleep(1.0)
        return None

    async def _fill_from_chain(self, signature: str, side: str,
                               fees: FeeBreakdown,
                               quote: dict[str, Any]) -> FillResult:
        """Parse the ACTUAL fill (not the quote) from the landed transaction."""
        tx = None
        for _ in range(5):
            tx = await self.rpc.get_transaction(signature)
            if tx:
                break
            await asyncio.sleep(1.0)
        if not tx:
            return FillResult(filled=True, side=side, fees=fees, signature=signature,
                              reason="landed but tx fetch failed; amounts from quote",
                              sol_delta=-int(quote.get("inAmount", 0)) - fees.total
                              if side == "buy" else int(quote.get("outAmount", 0)),
                              tokens_delta=int(quote.get("outAmount", 0))
                              if side == "buy" else -int(quote.get("inAmount", 0)))
        meta = tx.get("meta") or {}
        keys = ((tx.get("transaction") or {}).get("message") or {}).get("accountKeys") or []
        idx = next((i for i, k in enumerate(keys)
                    if (k.get("pubkey") if isinstance(k, dict) else k) == self.wallet.pubkey), 0)
        pre_sol = (meta.get("preBalances") or [0])[idx]
        post_sol = (meta.get("postBalances") or [0])[idx]
        sol_delta = post_sol - pre_sol

        token_delta = 0
        for post in meta.get("postTokenBalances") or []:
            if post.get("owner") != self.wallet.pubkey or post.get("mint") == C.WSOL_MINT:
                continue
            pre_amt = 0
            for pre in meta.get("preTokenBalances") or []:
                if pre.get("accountIndex") == post.get("accountIndex"):
                    pre_amt = int((pre.get("uiTokenAmount") or {}).get("amount", 0))
            token_delta += int((post.get("uiTokenAmount") or {}).get("amount", 0)) - pre_amt

        fees.base_fee = int(meta.get("fee", fees.base_fee))
        effective = (abs(sol_delta) / abs(token_delta)) if token_delta else 0.0
        return FillResult(filled=True, side=side, sol_delta=sol_delta,
                          tokens_delta=token_delta, effective_price=effective,
                          fees=fees, signature=signature, reason="live_fill",
                          slippage_bps=0.0)
