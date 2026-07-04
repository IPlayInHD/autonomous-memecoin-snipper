"""Minimal async Solana JSON-RPC client with per-request latency tracking.

The rolling latency window feeds the risk layer's RPC-degradation halt: if the
shared endpoint slows past the configured bound, new entries stop — a slow RPC
turns every exit into a loss.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import time
from collections import deque
from typing import Any, Optional

import aiohttp

log = logging.getLogger(__name__)


class RpcError(Exception):
    def __init__(self, method: str, code: Any, message: str):
        super().__init__(f"{method}: [{code}] {message}")
        self.method = method
        self.code = code
        self.message = message


class RpcClient:
    def __init__(self, url: str, timeout_s: float = 10.0, commitment: str = "confirmed",
                 latency_window: int = 200):
        self.url = url
        self.timeout_s = timeout_s
        self.commitment = commitment
        self._ids = itertools.count(1)
        self._session: Optional[aiohttp.ClientSession] = None
        self._latencies_ms: deque[float] = deque(maxlen=latency_window)
        self._errors = 0
        self._requests = 0

    async def __aenter__(self) -> "RpcClient":
        await self._ensure_session()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout_s))
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    # -- latency stats (risk layer input) ------------------------------------
    def latency_percentile(self, pct: float) -> Optional[float]:
        if not self._latencies_ms:
            return None
        data = sorted(self._latencies_ms)
        idx = min(len(data) - 1, max(0, int(round(pct / 100.0 * (len(data) - 1)))))
        return data[idx]

    @property
    def error_rate(self) -> float:
        return self._errors / self._requests if self._requests else 0.0

    # -- core -----------------------------------------------------------------
    async def call(self, method: str, params: list[Any] | None = None,
                   retries: int = 2) -> Any:
        session = await self._ensure_session()
        payload = {"jsonrpc": "2.0", "id": next(self._ids),
                   "method": method, "params": params or []}
        last_exc: Optional[Exception] = None
        for attempt in range(retries + 1):
            self._requests += 1
            start = time.monotonic()
            try:
                async with session.post(self.url, json=payload) as resp:
                    body = await resp.json(content_type=None)
                self._latencies_ms.append((time.monotonic() - start) * 1000.0)
                if "error" in body:
                    err = body["error"]
                    raise RpcError(method, err.get("code"), err.get("message", ""))
                return body.get("result")
            except RpcError:
                self._errors += 1
                raise  # structured RPC errors are not transient; surface them
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
                self._errors += 1
                last_exc = exc
                if attempt < retries:
                    await asyncio.sleep(0.25 * 2 ** attempt)
        raise RpcError(method, "network", str(last_exc))

    # -- convenience wrappers ----------------------------------------------------
    def _c(self, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        base = {"commitment": self.commitment}
        if extra:
            base.update(extra)
        return base

    async def get_account_info(self, pubkey: str) -> Optional[dict[str, Any]]:
        res = await self.call("getAccountInfo",
                              [pubkey, self._c({"encoding": "base64"})])
        return (res or {}).get("value")

    async def get_multiple_accounts(self, pubkeys: list[str]) -> list[Optional[dict]]:
        res = await self.call("getMultipleAccounts",
                              [pubkeys, self._c({"encoding": "base64"})])
        return (res or {}).get("value", [None] * len(pubkeys))

    async def get_token_largest_accounts(self, mint: str) -> list[dict[str, Any]]:
        res = await self.call("getTokenLargestAccounts", [mint, self._c()])
        return (res or {}).get("value", [])

    async def get_token_supply(self, mint: str) -> Optional[dict[str, Any]]:
        res = await self.call("getTokenSupply", [mint, self._c()])
        return (res or {}).get("value")

    async def get_token_accounts_by_owner(self, owner: str,
                                          mint: str | None = None) -> list[dict]:
        flt = {"mint": mint} if mint else {"programId":
              "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"}
        res = await self.call(
            "getTokenAccountsByOwner",
            [owner, flt, self._c({"encoding": "jsonParsed"})])
        return (res or {}).get("value", [])

    async def get_transaction(self, signature: str) -> Optional[dict[str, Any]]:
        return await self.call("getTransaction", [signature, {
            "encoding": "jsonParsed", "commitment": self.commitment,
            "maxSupportedTransactionVersion": 0}])

    async def get_block_time(self, slot: int) -> Optional[int]:
        try:
            return await self.call("getBlockTime", [slot])
        except RpcError:
            return None

    async def get_balance(self, pubkey: str) -> int:
        res = await self.call("getBalance", [pubkey, self._c()])
        return int((res or {}).get("value", 0))

    async def get_latest_blockhash(self) -> tuple[str, int]:
        res = await self.call("getLatestBlockhash", [self._c()])
        v = (res or {}).get("value", {})
        return v.get("blockhash", ""), int(v.get("lastValidBlockHeight", 0))

    async def simulate_transaction(self, tx_b64: str,
                                   sig_verify: bool = False) -> dict[str, Any]:
        res = await self.call("simulateTransaction", [tx_b64, {
            "encoding": "base64", "commitment": self.commitment,
            "sigVerify": sig_verify, "replaceRecentBlockhash": not sig_verify}])
        return (res or {}).get("value", {})

    async def send_transaction(self, tx_b64: str, max_retries: int = 0) -> str:
        return await self.call("sendTransaction", [tx_b64, {
            "encoding": "base64", "skipPreflight": True, "maxRetries": max_retries}])

    async def get_signature_statuses(self, signatures: list[str]) -> list[Optional[dict]]:
        res = await self.call("getSignatureStatuses",
                              [signatures, {"searchTransactionHistory": False}])
        return (res or {}).get("value", [None] * len(signatures))

    async def get_signatures_for_address(self, address: str, limit: int = 50,
                                         before: str | None = None) -> list[dict]:
        opts: dict[str, Any] = {"limit": limit, "commitment": self.commitment}
        if before:
            opts["before"] = before
        return await self.call("getSignaturesForAddress", [address, opts]) or []
