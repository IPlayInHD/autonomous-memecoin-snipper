"""Typed chain data access + raw account parsers.

The parsers are pure functions over bytes so the filter unit tests can feed
them crafted fixtures (including hostile Token-2022 mints) without a network.
"""

from __future__ import annotations

import base64
import logging
import struct
from typing import Any, Optional, Protocol

import base58

from .. import constants as C
from ..models import (
    LaunchEvent, LaunchSource, MintInfo, PoolState, SimulateResult,
    Token2022Extensions, TokenHolding,
)
from .rpc import RpcClient, RpcError

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pure parsers
# ---------------------------------------------------------------------------

def _pubkey(data: bytes) -> Optional[str]:
    """32 bytes -> base58, or None for the all-zero key."""
    if len(data) != 32 or data == b"\x00" * 32:
        return None
    return base58.b58encode(data).decode()


def parse_mint(data: bytes, owner_program: str, address: str = "") -> MintInfo:
    """Parse a classic SPL or Token-2022 mint account."""
    if len(data) < C.MINT_ACCOUNT_BASE_LEN:
        raise ValueError(f"mint account too short: {len(data)} bytes")
    has_mint_auth = struct.unpack_from("<I", data, 0)[0] == 1
    mint_auth = _pubkey(data[4:36]) if has_mint_auth else None
    supply = struct.unpack_from("<Q", data, 36)[0]
    decimals = data[44]
    has_freeze = struct.unpack_from("<I", data, 46)[0] == 1
    freeze_auth = _pubkey(data[50:82]) if has_freeze else None

    ext = Token2022Extensions(is_token_2022=(owner_program == C.TOKEN_2022_PROGRAM))
    if ext.is_token_2022 and len(data) > C.T22_ACCOUNT_TYPE_OFFSET:
        ext = parse_token2022_extensions(data)

    return MintInfo(address=address, owner_program=owner_program, decimals=decimals,
                    supply=supply, mint_authority=mint_auth, freeze_authority=freeze_auth,
                    extensions=ext)


def parse_token2022_extensions(data: bytes) -> Token2022Extensions:
    """Walk the TLV region of a Token-2022 mint.

    Layout: 82-byte base mint, zero padding to byte 165, one account-type byte
    (1 = Mint), then repeated [type: u16 LE][len: u16 LE][payload] entries.
    Anything we do not recognize is recorded in `unknown_extensions` — an
    unknown extension on a memecoin is itself a red flag.
    """
    ext = Token2022Extensions(is_token_2022=True)
    pos = C.T22_ACCOUNT_TYPE_OFFSET + 1
    while pos + 4 <= len(data):
        etype, elen = struct.unpack_from("<HH", data, pos)
        pos += 4
        payload = data[pos:pos + elen]
        pos += elen
        if etype == 0:  # Uninitialized padding — end of TLV
            break
        if etype == C.EXT_TRANSFER_FEE_CONFIG and len(payload) >= 108:
            # newer_transfer_fee lives at payload[90:108]: epoch u64, max u64, bps u16
            ext.max_transfer_fee = struct.unpack_from("<Q", payload, 98)[0]
            ext.transfer_fee_bps = struct.unpack_from("<H", payload, 106)[0]
            older_bps = struct.unpack_from("<H", payload, 88)[0]
            ext.transfer_fee_bps = max(ext.transfer_fee_bps, older_bps)
        elif etype == C.EXT_TRANSFER_HOOK and len(payload) >= 64:
            hook = _pubkey(payload[32:64])
            ext.has_transfer_hook = hook is not None
            ext.transfer_hook_program = hook
        elif etype == C.EXT_PERMANENT_DELEGATE and len(payload) >= 32:
            delegate = _pubkey(payload[0:32])
            ext.has_permanent_delegate = delegate is not None
            ext.permanent_delegate = delegate
        elif etype == C.EXT_DEFAULT_ACCOUNT_STATE and len(payload) >= 1:
            ext.default_state_frozen = payload[0] == 2  # AccountState::Frozen
        elif etype == C.EXT_NON_TRANSFERABLE:
            ext.non_transferable = True
        elif etype == C.EXT_CONFIDENTIAL_TRANSFER_MINT:
            ext.has_confidential_transfer = True
        elif etype not in (C.EXT_MINT_CLOSE_AUTHORITY, C.EXT_METADATA_POINTER,
                           C.EXT_TOKEN_METADATA):
            ext.unknown_extensions.append(etype)
    return ext


def parse_token_account(data: bytes) -> tuple[Optional[str], Optional[str], int]:
    """SPL token account -> (mint, owner, amount)."""
    if len(data) < 72:
        raise ValueError("token account too short")
    return _pubkey(data[0:32]), _pubkey(data[32:64]), struct.unpack_from("<Q", data, 64)[0]


def parse_bonding_curve(data: bytes) -> dict[str, Any]:
    """pump.fun bonding-curve account.

    Layout after the 8-byte anchor discriminator:
    virtual_token_reserves u64, virtual_sol_reserves u64,
    real_token_reserves u64, real_sol_reserves u64,
    token_total_supply u64, complete u8.
    """
    if len(data) < 8 + 41:
        raise ValueError("bonding curve account too short")
    vt, vs, rt, rs, total = struct.unpack_from("<QQQQQ", data, 8)
    complete = data[48] == 1
    return {"virtual_token_reserves": vt, "virtual_sol_reserves": vs,
            "real_token_reserves": rt, "real_sol_reserves": rs,
            "token_total_supply": total, "complete": complete}


def _decode_account_data(value: dict[str, Any]) -> bytes:
    data = value.get("data")
    if isinstance(data, list) and data and data[1] == "base64":
        return base64.b64decode(data[0])
    if isinstance(data, str):
        return base64.b64decode(data)
    raise ValueError("unsupported account data encoding")


# ---------------------------------------------------------------------------
# Provider protocol — filters depend on this, tests inject fakes
# ---------------------------------------------------------------------------

class ChainDataProvider(Protocol):
    async def get_mint_info(self, mint: str) -> Optional[MintInfo]: ...
    async def get_pool_state(self, event: LaunchEvent) -> Optional[PoolState]: ...
    async def get_largest_holders(self, mint: str) -> list[TokenHolding]: ...
    async def get_token_supply(self, mint: str) -> Optional[int]: ...
    async def get_account_owner_program(self, pubkey: str) -> Optional[str]: ...
    async def get_lp_holders(self, lp_mint: str) -> list[TokenHolding]: ...
    async def get_launch_block_buys(self, event: LaunchEvent) -> Optional[dict[str, int]]: ...
    async def simulate_sell(self, event: LaunchEvent, tokens: int) -> SimulateResult: ...


class RpcChainDataProvider:
    """Live implementation over JSON-RPC (+ optional sell-sim builder hook)."""

    def __init__(self, rpc: RpcClient, sell_sim_builder=None):
        self.rpc = rpc
        # callable (event, tokens) -> base64 tx to simulate; injected by the
        # execution layer so the provider stays dependency-light
        self._sell_sim_builder = sell_sim_builder

    async def get_mint_info(self, mint: str) -> Optional[MintInfo]:
        value = await self.rpc.get_account_info(mint)
        if value is None:
            return None
        data = _decode_account_data(value)
        return parse_mint(data, value.get("owner", ""), address=mint)

    async def get_account_owner_program(self, pubkey: str) -> Optional[str]:
        value = await self.rpc.get_account_info(pubkey)
        return None if value is None else value.get("owner")

    async def get_pool_state(self, event: LaunchEvent) -> Optional[PoolState]:
        if event.source == LaunchSource.PUMPFUN_LAUNCH:
            return await self._bonding_curve_state(event)
        return await self._amm_pool_state(event)

    async def _bonding_curve_state(self, event: LaunchEvent) -> Optional[PoolState]:
        if not event.pool:
            return None
        value = await self.rpc.get_account_info(event.pool)
        if value is None:
            return None
        curve = parse_bonding_curve(_decode_account_data(value))
        return PoolState(
            pool=event.pool, token_mint=event.mint,
            sol_reserve=curve["virtual_sol_reserves"],
            token_reserve=curve["virtual_token_reserves"],
            is_bonding_curve=True, curve_complete=curve["complete"],
        )

    async def _amm_pool_state(self, event: LaunchEvent) -> Optional[PoolState]:
        vaults = [v for v in (event.base_vault, event.quote_vault) if v]
        if len(vaults) != 2:
            vaults = await self._discover_vaults(event)
            if not vaults:
                return None
            event.base_vault, event.quote_vault = vaults[0], vaults[1]
        accounts = await self.rpc.get_multiple_accounts(
            [event.base_vault, event.quote_vault])
        token_reserve = sol_reserve = None
        for acc in accounts:
            if acc is None:
                continue
            mint, _owner, amount = parse_token_account(_decode_account_data(acc))
            if mint == event.mint:
                token_reserve = amount
            elif mint == C.WSOL_MINT:
                sol_reserve = amount
        if token_reserve is None or sol_reserve is None:
            return None
        lp_supply = None
        if event.lp_mint:
            supply = await self.rpc.get_token_supply(event.lp_mint)
            if supply:
                lp_supply = int(supply.get("amount", 0))
        return PoolState(pool=event.pool or "", token_mint=event.mint,
                         sol_reserve=sol_reserve, token_reserve=token_reserve,
                         lp_mint=event.lp_mint, lp_supply=lp_supply)

    async def _discover_vaults(self, event: LaunchEvent) -> Optional[list[str]]:
        """PumpSwap vaults are ATAs owned by the pool account itself."""
        if not event.pool:
            return None
        try:
            token_acc = await self.rpc.get_token_accounts_by_owner(event.pool, event.mint)
            wsol_acc = await self.rpc.get_token_accounts_by_owner(event.pool, C.WSOL_MINT)
        except RpcError as exc:
            log.warning("vault discovery failed for %s: %s", event.pool, exc)
            return None
        if not token_acc or not wsol_acc:
            return None
        return [token_acc[0]["pubkey"], wsol_acc[0]["pubkey"]]

    async def get_largest_holders(self, mint: str) -> list[TokenHolding]:
        raw = await self.rpc.get_token_largest_accounts(mint)
        holdings = [TokenHolding(address=r["address"], owner=None,
                                 amount=int(r.get("amount", 0))) for r in raw]
        # resolve owners in one batch so filters can attribute supply to wallets
        if holdings:
            accounts = await self.rpc.get_multiple_accounts([h.address for h in holdings])
            for holding, acc in zip(holdings, accounts):
                if acc is not None:
                    try:
                        _mint, owner, _amt = parse_token_account(_decode_account_data(acc))
                        holding.owner = owner
                    except ValueError:
                        pass
        return holdings

    async def get_token_supply(self, mint: str) -> Optional[int]:
        supply = await self.rpc.get_token_supply(mint)
        return None if supply is None else int(supply.get("amount", 0))

    async def get_lp_holders(self, lp_mint: str) -> list[TokenHolding]:
        return await self.get_largest_holders(lp_mint)

    async def get_launch_block_buys(self, event: LaunchEvent) -> Optional[dict[str, int]]:
        """Best-effort bundle detection input: token amounts acquired by each
        wallet in the launch transaction itself (bundled buys ride the same tx
        or the same slot). Returns {owner: raw_token_amount}."""
        try:
            tx = await self.rpc.get_transaction(event.signature)
        except RpcError as exc:
            log.warning("launch tx fetch failed %s: %s", event.signature, exc)
            return None
        if not tx:
            return None
        meta = tx.get("meta") or {}
        buys: dict[str, int] = {}
        pool_accounts = {event.pool, event.base_vault}
        for post in meta.get("postTokenBalances", []):
            if post.get("mint") != event.mint:
                continue
            owner = post.get("owner")
            if not owner or owner in pool_accounts:
                continue
            amount = int((post.get("uiTokenAmount") or {}).get("amount", 0))
            pre_amount = 0
            for pre in meta.get("preTokenBalances", []):
                if (pre.get("accountIndex") == post.get("accountIndex")
                        and pre.get("mint") == event.mint):
                    pre_amount = int((pre.get("uiTokenAmount") or {}).get("amount", 0))
            delta = amount - pre_amount
            if delta > 0:
                buys[owner] = buys.get(owner, 0) + delta
        return buys

    async def simulate_sell(self, event: LaunchEvent, tokens: int) -> SimulateResult:
        if self._sell_sim_builder is None:
            return SimulateResult(success=False, error="no sell-sim builder configured")
        try:
            tx_b64 = await self._sell_sim_builder(event, tokens)
        except Exception as exc:  # noqa: BLE001 - builder failure = can't verify sellability
            return SimulateResult(success=False, error=f"build failed: {exc}")
        if tx_b64 is None:
            return SimulateResult(success=False, error="no sell route available")
        try:
            value = await self.rpc.simulate_transaction(tx_b64)
        except RpcError as exc:
            return SimulateResult(success=False, error=str(exc))
        err = value.get("err")
        return SimulateResult(
            success=err is None,
            error=None if err is None else str(err),
            logs=value.get("logs") or [],
            units_consumed=value.get("unitsConsumed"),
        )
