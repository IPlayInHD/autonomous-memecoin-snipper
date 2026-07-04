"""Shared fixtures: crafted account bytes + a fake chain provider."""

from __future__ import annotations

import struct
import sys
from pathlib import Path
from typing import Optional

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sniper import constants as C
from sniper.config import Config
from sniper.models import (
    DeployerInfo, LaunchEvent, LaunchSource, MintInfo, PoolState, SimulateResult,
    TokenHolding,
)
from sniper.chain.provider import parse_mint

import base58


def pk(seed: int) -> str:
    """Deterministic fake pubkey (base58 of 32 bytes)."""
    return base58.b58encode(bytes([seed % 256]) * 32).decode()


def pk_bytes(seed: int) -> bytes:
    return bytes([seed % 256]) * 32


# --------------------------------------------------------------------------
# Account-byte builders
# --------------------------------------------------------------------------

def make_mint_bytes(mint_authority: Optional[bytes] = None,
                    freeze_authority: Optional[bytes] = None,
                    supply: int = 10 ** 15, decimals: int = 6) -> bytes:
    buf = bytearray(82)
    struct.pack_into("<I", buf, 0, 1 if mint_authority else 0)
    buf[4:36] = mint_authority or b"\x00" * 32
    struct.pack_into("<Q", buf, 36, supply)
    buf[44] = decimals
    buf[45] = 1
    struct.pack_into("<I", buf, 46, 1 if freeze_authority else 0)
    buf[50:82] = freeze_authority or b"\x00" * 32
    return bytes(buf)


def make_t22_mint_bytes(extensions: list[tuple[int, bytes]], **mint_kwargs) -> bytes:
    """Token-2022 mint: base 82 + pad to 165 + type byte + TLV entries."""
    base = make_mint_bytes(**mint_kwargs)
    buf = bytearray(base) + bytearray(165 - 82) + bytes([1])  # AccountType::Mint
    for etype, payload in extensions:
        buf += struct.pack("<HH", etype, len(payload)) + payload
    return bytes(buf)


def transfer_fee_payload(bps: int, max_fee: int = 10 ** 9) -> bytes:
    buf = bytearray(108)
    struct.pack_into("<QQH", buf, 72, 0, max_fee, bps)   # older
    struct.pack_into("<QQH", buf, 90, 1, max_fee, bps)   # newer
    return bytes(buf)


def transfer_hook_payload(program: Optional[bytes]) -> bytes:
    return pk_bytes(9) + (program or b"\x00" * 32)


def make_bonding_curve_bytes(virtual_sol: int, virtual_tokens: int,
                             complete: bool = False) -> bytes:
    buf = bytearray(8)  # discriminator
    buf += struct.pack("<QQQQQ", virtual_tokens, virtual_sol,
                       virtual_tokens // 2, virtual_sol // 2, 10 ** 15)
    buf += bytes([1 if complete else 0])
    return bytes(buf)


# --------------------------------------------------------------------------
# Fake provider
# --------------------------------------------------------------------------

class FakeProvider:
    def __init__(self):
        self.mints: dict[str, MintInfo] = {}
        self.pool: Optional[PoolState] = None
        self.holders: dict[str, list[TokenHolding]] = {}
        self.supplies: dict[str, int] = {}
        self.owner_programs: dict[str, str] = {}
        self.launch_buys: Optional[dict[str, int]] = {}
        self.sim_result = SimulateResult(success=True)

    def add_clean_mint(self, mint: str, decimals: int = 6,
                       supply: int = 10 ** 15) -> None:
        data = make_mint_bytes(None, None, supply=supply, decimals=decimals)
        self.mints[mint] = parse_mint(data, C.TOKEN_PROGRAM, mint)

    async def get_mint_info(self, mint: str):
        return self.mints.get(mint)

    async def get_pool_state(self, event: LaunchEvent):
        return self.pool

    async def get_largest_holders(self, mint: str):
        return self.holders.get(mint, [])

    async def get_token_supply(self, mint: str):
        return self.supplies.get(mint)

    async def get_account_owner_program(self, pubkey: str):
        return self.owner_programs.get(pubkey)

    async def get_lp_holders(self, lp_mint: str):
        return self.holders.get(lp_mint, [])

    async def get_launch_block_buys(self, event: LaunchEvent):
        return self.launch_buys

    async def simulate_sell(self, event: LaunchEvent, tokens: int):
        return self.sim_result


# --------------------------------------------------------------------------
# Common fixtures
# --------------------------------------------------------------------------

@pytest.fixture
def cfg() -> Config:
    c = Config()
    c.execution.paper.rng_seed = 7
    return c


@pytest.fixture
def provider() -> FakeProvider:
    return FakeProvider()


@pytest.fixture
def event() -> LaunchEvent:
    return LaunchEvent(
        mint=pk(1), source=LaunchSource.RAYDIUM_POOL, signature="sig111",
        slot=1000, creator=pk(2), pool=pk(3), base_vault=pk(4),
        quote_vault=pk(5), lp_mint=pk(6), block_time=1_700_000_000.0)


@pytest.fixture
def pool(event) -> PoolState:
    return PoolState(pool=event.pool, token_mint=event.mint,
                     sol_reserve=50 * C.LAMPORTS_PER_SOL,
                     token_reserve=10 ** 12, lp_mint=event.lp_mint,
                     lp_supply=10 ** 9)
