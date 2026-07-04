"""Official Jupiter swap API client (quote + swap-transaction build).

Also provides the honeypot sell-simulation builder: it constructs a sell
transaction on behalf of an EXISTING top holder (simulateTransaction with
sigVerify=false doesn't need their signature) so sellability can be verified
before we own a single token.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import aiohttp

from .. import constants as C
from ..models import LaunchEvent

log = logging.getLogger(__name__)


class JupiterClient:
    def __init__(self, base_url: str, timeout_s: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self._session: Optional[aiohttp.ClientSession] = None

    async def _ensure(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout_s))
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def quote(self, input_mint: str, output_mint: str, amount: int,
                    slippage_bps: int) -> Optional[dict[str, Any]]:
        session = await self._ensure()
        params = {"inputMint": input_mint, "outputMint": output_mint,
                  "amount": str(amount), "slippageBps": str(slippage_bps),
                  "swapMode": "ExactIn"}
        try:
            async with session.get(f"{self.base_url}/quote", params=params) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    log.debug("jupiter quote %s: %s", resp.status, body[:200])
                    return None
                data = await resp.json()
        except aiohttp.ClientError as exc:
            log.warning("jupiter quote failed: %s", exc)
            return None
        return None if data.get("error") else data

    async def swap_transaction(self, quote: dict[str, Any], user_pubkey: str,
                               priority_fee_lamports: int = 0,
                               jito_tip_lamports: int = 0) -> Optional[str]:
        """Build the swap tx (base64 VersionedTransaction, unsigned)."""
        session = await self._ensure()
        payload: dict[str, Any] = {
            "quoteResponse": quote,
            "userPublicKey": user_pubkey,
            "wrapAndUnwrapSol": True,
            "dynamicComputeUnitLimit": True,
        }
        if jito_tip_lamports > 0:
            payload["prioritizationFeeLamports"] = {"jitoTipLamports": jito_tip_lamports}
        elif priority_fee_lamports > 0:
            payload["prioritizationFeeLamports"] = priority_fee_lamports
        try:
            async with session.post(f"{self.base_url}/swap", json=payload) as resp:
                if resp.status != 200:
                    log.debug("jupiter swap build %s: %s", resp.status,
                              (await resp.text())[:200])
                    return None
                data = await resp.json()
        except aiohttp.ClientError as exc:
            log.warning("jupiter swap build failed: %s", exc)
            return None
        return data.get("swapTransaction")

    @staticmethod
    def buy_tax_bps(quote: dict[str, Any]) -> Optional[int]:
        """Rough buy-tax sanity check from a quote: how far the quoted output
        falls short of the price-impact-adjusted expectation."""
        try:
            impact_pct = float(quote.get("priceImpactPct", 0.0))
            # Jupiter folds token taxes into otherAmountThreshold vs outAmount
            out_amt = int(quote["outAmount"])
            other = int(quote.get("otherAmountThreshold", out_amt))
            if out_amt <= 0:
                return None
            shortfall_bps = max(0, (out_amt - other) * 10_000 // out_amt)
            return shortfall_bps + int(abs(impact_pct) * 100)
        except (KeyError, ValueError, TypeError):
            return None


def make_sell_sim_builder(jupiter: JupiterClient, provider_getter,
                          slippage_bps: int = 1000):
    """Returns an async (event, tokens) -> Optional[base64 tx] callable for the
    honeypot filter. Uses the current largest non-pool holder as the simulated
    seller. `provider_getter` defers the provider reference (circular wiring)."""

    async def build(event: LaunchEvent, tokens: int) -> Optional[str]:
        provider = provider_getter()
        holders = await provider.get_largest_holders(event.mint)
        pool_accounts = {event.pool, event.base_vault}
        seller = None
        sell_amount = tokens
        for h in holders:
            if h.owner and h.owner not in pool_accounts \
                    and h.address not in pool_accounts and h.amount > 0:
                seller = h.owner
                sell_amount = max(1, min(tokens, h.amount // 2))
                break
        if seller is None:
            return None
        quote = await jupiter.quote(event.mint, C.WSOL_MINT, sell_amount, slippage_bps)
        if quote is None:
            return None
        return await jupiter.swap_transaction(quote, seller)

    return build
