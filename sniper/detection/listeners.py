"""Launch detection: pump.fun creates & migrations, PumpSwap pools, Raydium pools.

Flow: logsSubscribe notification (cheap, gives signature+slot+logs) -> match
instruction markers in logs -> fetch the parsed transaction -> extract mint /
pool / vaults with the pure `extract_*` helpers -> emit a LaunchEvent.

t0 for the latency budget is the transaction's blockTime; `event_seen` is the
wall-clock moment the *websocket notification* arrived (before the tx fetch,
which costs an extra round trip and is part of filters, not detection).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

from .. import constants as C
from ..config import Config
from ..models import LaunchEvent, LaunchSource
from ..chain.rpc import RpcClient, RpcError
from .ws import ResilientWebSocket

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pure extraction helpers over jsonParsed getTransaction results (testable)
# ---------------------------------------------------------------------------

def _instructions(tx: dict[str, Any]) -> list[dict[str, Any]]:
    msg = ((tx.get("transaction") or {}).get("message") or {})
    instrs = list(msg.get("instructions") or [])
    for inner in ((tx.get("meta") or {}).get("innerInstructions") or []):
        instrs.extend(inner.get("instructions") or [])
    return instrs


def _first_signer(tx: dict[str, Any]) -> Optional[str]:
    msg = ((tx.get("transaction") or {}).get("message") or {})
    for key in msg.get("accountKeys") or []:
        if isinstance(key, dict) and key.get("signer"):
            return key.get("pubkey")
    return None


def _new_token_mint(tx: dict[str, Any]) -> Optional[str]:
    """Fallback mint discovery: the non-WSOL mint present in postTokenBalances."""
    for bal in ((tx.get("meta") or {}).get("postTokenBalances") or []):
        mint = bal.get("mint")
        if mint and mint != C.WSOL_MINT:
            return mint
    return None


def extract_pumpfun_create(tx: dict[str, Any]) -> Optional[dict[str, str]]:
    """pump.fun `create` accounts: [0]=mint, [2]=bonding_curve, [7]=creator."""
    for ix in _instructions(tx):
        if ix.get("programId") != C.PUMPFUN_PROGRAM:
            continue
        accounts = ix.get("accounts") or []
        if len(accounts) < 8:
            continue
        mint, curve, creator = accounts[0], accounts[2], accounts[7]
        if mint != _new_token_mint(tx) and _new_token_mint(tx) is not None:
            mint = _new_token_mint(tx)  # trust balances over index guessing
        return {"mint": mint, "pool": curve, "creator": creator}
    mint = _new_token_mint(tx)
    if mint:
        return {"mint": mint, "pool": "", "creator": _first_signer(tx) or ""}
    return None


def extract_pumpswap_create_pool(tx: dict[str, Any]) -> Optional[dict[str, str]]:
    """PumpSwap `create_pool` accounts:
    [0]=pool, [2]=creator, [3]=base_mint, [4]=quote_mint, [5]=lp_mint,
    [9]=pool_base_vault, [10]=pool_quote_vault (validated, with fallbacks)."""
    for ix in _instructions(tx):
        if ix.get("programId") != C.PUMPSWAP_PROGRAM:
            continue
        accounts = ix.get("accounts") or []
        if len(accounts) < 11:
            continue
        base_mint, quote_mint = accounts[3], accounts[4]
        token_mint = base_mint if quote_mint == C.WSOL_MINT else (
            quote_mint if base_mint == C.WSOL_MINT else None)
        if token_mint is None:
            token_mint = _new_token_mint(tx)
            if token_mint is None:
                continue
        base_is_token = base_mint == token_mint
        return {
            "mint": token_mint, "pool": accounts[0], "creator": accounts[2],
            "lp_mint": accounts[5],
            "base_vault": accounts[9] if base_is_token else accounts[10],
            "quote_vault": accounts[10] if base_is_token else accounts[9],
        }
    return None


def extract_raydium_initialize2(tx: dict[str, Any]) -> Optional[dict[str, str]]:
    """Raydium AMM v4 `initialize2` accounts:
    [4]=amm, [7]=lp_mint, [8]=coin_mint, [9]=pc_mint, [10]=coin_vault,
    [11]=pc_vault, [17]=creator wallet."""
    for ix in _instructions(tx):
        if ix.get("programId") != C.RAYDIUM_AMM_V4:
            continue
        accounts = ix.get("accounts") or []
        if len(accounts) < 18:
            continue
        coin_mint, pc_mint = accounts[8], accounts[9]
        if pc_mint == C.WSOL_MINT:
            token_mint, base_vault, quote_vault = coin_mint, accounts[10], accounts[11]
        elif coin_mint == C.WSOL_MINT:
            token_mint, base_vault, quote_vault = pc_mint, accounts[11], accounts[10]
        else:
            continue  # not a SOL pair; out of scope
        return {"mint": token_mint, "pool": accounts[4], "creator": accounts[17],
                "lp_mint": accounts[7], "base_vault": base_vault,
                "quote_vault": quote_vault}
    return None


def match_log_marker(logs: list[str], markers: tuple[str, ...]) -> bool:
    return any(m in line for line in logs for m in markers)


# ---------------------------------------------------------------------------
# Live detector
# ---------------------------------------------------------------------------

class LaunchDetector:
    """Subscribes to program logs and emits LaunchEvents onto `queue`."""

    def __init__(self, cfg: Config, rpc: RpcClient, queue: asyncio.Queue):
        self.cfg = cfg
        self.rpc = rpc
        self.queue = queue
        self.ws = ResilientWebSocket(cfg.rpc.ws_url)
        self._seen_signatures: dict[str, float] = {}
        self.stats = {"notifications": 0, "matched": 0, "emitted": 0,
                      "parse_failures": 0}
        self._wire_subscriptions()

    def _wire_subscriptions(self) -> None:
        def logs_params(program: str) -> list[Any]:
            return [{"mentions": [program]},
                    {"commitment": self.cfg.rpc.commitment}]

        t = self.cfg.trigger
        if t.listen_pumpfun:
            self.ws.subscribe("logsSubscribe", logs_params(C.PUMPFUN_PROGRAM),
                              self._on_pumpfun_logs, name="pumpfun")
        if t.listen_pumpswap:
            self.ws.subscribe("logsSubscribe", logs_params(C.PUMPSWAP_PROGRAM),
                              self._on_pumpswap_logs, name="pumpswap")
        if t.listen_raydium:
            self.ws.subscribe("logsSubscribe", logs_params(C.RAYDIUM_AMM_V4),
                              self._on_raydium_logs, name="raydium")

    async def run(self) -> None:
        gc_task = asyncio.create_task(self._gc_seen())
        try:
            await self.ws.run()
        finally:
            gc_task.cancel()

    def stop(self) -> None:
        self.ws.stop()

    async def _gc_seen(self) -> None:
        while True:
            await asyncio.sleep(300)
            cutoff = time.time() - 900
            self._seen_signatures = {s: t for s, t in self._seen_signatures.items()
                                     if t > cutoff}

    # -- log handlers ----------------------------------------------------------
    async def _on_pumpfun_logs(self, result: dict[str, Any]) -> None:
        value = result.get("value") or {}
        logs = value.get("logs") or []
        if value.get("err") is not None:
            return
        self.stats["notifications"] += 1
        if match_log_marker(logs, ("Instruction: Create",)) and \
                match_log_marker(logs, ("Instruction: MintTo", "InitializeMint")):
            await self._handle(value, result, LaunchSource.PUMPFUN_LAUNCH)
        elif match_log_marker(logs, ("Instruction: Migrate",)):
            await self._handle(value, result, LaunchSource.PUMPFUN_MIGRATION)

    async def _on_pumpswap_logs(self, result: dict[str, Any]) -> None:
        value = result.get("value") or {}
        if value.get("err") is not None:
            return
        self.stats["notifications"] += 1
        if match_log_marker(value.get("logs") or [], ("Instruction: CreatePool",)):
            await self._handle(value, result, LaunchSource.PUMPSWAP_POOL)

    async def _on_raydium_logs(self, result: dict[str, Any]) -> None:
        value = result.get("value") or {}
        if value.get("err") is not None:
            return
        self.stats["notifications"] += 1
        if match_log_marker(value.get("logs") or [],
                            ("initialize2", "Instruction: Initialize2")):
            await self._handle(value, result, LaunchSource.RAYDIUM_POOL)

    # -- event assembly ----------------------------------------------------------
    async def _handle(self, value: dict[str, Any], result: dict[str, Any],
                      source: LaunchSource) -> None:
        signature = value.get("signature", "")
        detected_wall = time.time()  # before the tx fetch: true event_seen
        detected_mono = time.monotonic()
        if not signature or signature in self._seen_signatures:
            return  # duplicate notification guard
        self._seen_signatures[signature] = detected_wall
        self.stats["matched"] += 1
        slot = int((result.get("context") or {}).get("slot") or 0)

        try:
            tx = await self.rpc.get_transaction(signature)
        except RpcError as exc:
            log.warning("tx fetch failed for %s (%s): %s", source.value, signature, exc)
            self.stats["parse_failures"] += 1
            return
        if tx is None:
            self.stats["parse_failures"] += 1
            return

        extracted = self._extract(tx, source)
        if extracted is None:
            log.info("could not extract %s details from %s", source.value, signature)
            self.stats["parse_failures"] += 1
            return

        block_time = tx.get("blockTime")
        event = LaunchEvent(
            mint=extracted["mint"], source=source, signature=signature,
            slot=tx.get("slot") or slot, creator=extracted.get("creator") or None,
            pool=extracted.get("pool") or None,
            base_vault=extracted.get("base_vault"),
            quote_vault=extracted.get("quote_vault"),
            lp_mint=extracted.get("lp_mint"),
            block_time=float(block_time) if block_time else None,
            detected_wall=detected_wall, detected_mono=detected_mono,
        )
        self.stats["emitted"] += 1
        await self.queue.put(event)

    def _extract(self, tx: dict[str, Any],
                 source: LaunchSource) -> Optional[dict[str, str]]:
        if source == LaunchSource.PUMPFUN_LAUNCH:
            return extract_pumpfun_create(tx)
        if source in (LaunchSource.PUMPSWAP_POOL, LaunchSource.PUMPFUN_MIGRATION):
            ext = extract_pumpswap_create_pool(tx)
            if ext is None and source == LaunchSource.PUMPFUN_MIGRATION:
                # migration tx itself may not contain the pool-create ix;
                # fall back to mint-only, pool resolved later by the provider
                mint = _new_token_mint(tx)
                if mint:
                    return {"mint": mint, "creator": _first_signer(tx) or ""}
            return ext
        if source == LaunchSource.RAYDIUM_POOL:
            return extract_raydium_initialize2(tx)
        return None
