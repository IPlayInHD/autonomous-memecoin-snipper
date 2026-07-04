"""Deployer-wallet intelligence via the Helius enhanced-transactions API.

This lookup is slow (hundreds of ms to seconds) and rate-limited on the free
tier, so it is async + cached in SQLite and only gates the slower entry modes
(migration / filter_edge) — never block_0.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

import aiohttp

from ..models import DeployerInfo
from ..storage.db import Database

log = logging.getLogger(__name__)

HELIUS_BASE = "https://api.helius.xyz/v0"


class HeliusClient:
    def __init__(self, api_key: str, db: Database, cache_ttl_s: float = 21600.0,
                 timeout_s: float = 10.0):
        self.api_key = api_key
        self.db = db
        self.cache_ttl_s = cache_ttl_s
        self.timeout_s = timeout_s
        self._session: Optional[aiohttp.ClientSession] = None

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def _get(self, path: str, params: dict[str, Any]) -> Any:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout_s))
        params = dict(params, **{"api-key": self.api_key})
        async with self._session.get(f"{HELIUS_BASE}{path}", params=params) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def deployer_info(self, wallet: str) -> Optional[DeployerInfo]:
        """Wallet age, funding source and prior token-creation history.
        Returns None when the API is unavailable — the filter treats that as
        'unknown', which scores low but is distinct from 'known bad'."""
        cached = self.db.get_deployer(wallet, self.cache_ttl_s)
        if cached is not None:
            return DeployerInfo(
                wallet=wallet, first_tx_time=cached["first_tx_time"],
                funding_source=cached["funding_source"],
                funding_source_flagged=bool(cached["funding_source_flagged"]),
                tokens_created=cached["tokens_created"],
                prior_rugs=cached["prior_rugs"], fetched_at=cached["fetched_at"])
        if not self.enabled:
            return None
        try:
            info = await self._fetch(wallet)
        except Exception as exc:  # noqa: BLE001 - external API, degrade to unknown
            log.warning("helius deployer lookup failed for %s: %s", wallet, exc)
            return None
        self.db.put_deployer(wallet, info.first_tx_time, info.funding_source,
                             info.funding_source_flagged, info.tokens_created,
                             info.prior_rugs)
        return info

    async def _fetch(self, wallet: str) -> DeployerInfo:
        # Recent enhanced history: token creations + first funding.
        txs: list[dict] = await self._get(f"/addresses/{wallet}/transactions",
                                          {"limit": 100})
        info = DeployerInfo(wallet=wallet)
        oldest_ts: Optional[float] = None
        for tx in txs:
            ts = tx.get("timestamp")
            if ts and (oldest_ts is None or ts < oldest_ts):
                oldest_ts = float(ts)
            ttype = tx.get("type", "")
            if ttype in ("TOKEN_MINT", "CREATE_POOL") or (
                    ttype == "UNKNOWN" and "initializeMint" in str(tx.get("instructions", ""))):
                info.tokens_created += 1
            # first inbound SOL transfer ≈ funding source
            for nt in tx.get("nativeTransfers") or []:
                if nt.get("toUserAccount") == wallet and info.funding_source is None:
                    info.funding_source = nt.get("fromUserAccount")
        # 100 txs returned and none older than 24h => wallet is churning hard;
        # treat as fresh/disposable even if we can't see its true first tx.
        if len(txs) == 100 and oldest_ts and time.time() - oldest_ts < 86400:
            info.funding_source_flagged = True
        info.first_tx_time = oldest_ts
        # prior_rugs needs cross-referencing created mints against drained
        # pools; approximated here by heavy serial token creation. A stricter
        # implementation can enrich this row asynchronously later.
        if info.tokens_created >= 5:
            info.prior_rugs = info.tokens_created // 5
        return info
