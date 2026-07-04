"""Account parsers: classic mint, Token-2022 TLV, bonding curve, token account."""

import struct

import pytest

from conftest import (
    make_bonding_curve_bytes, make_mint_bytes, make_t22_mint_bytes, pk, pk_bytes,
    transfer_fee_payload, transfer_hook_payload,
)
from sniper import constants as C
from sniper.chain.provider import (
    parse_bonding_curve, parse_mint, parse_token2022_extensions, parse_token_account,
)


class TestClassicMint:
    def test_revoked_authorities(self):
        info = parse_mint(make_mint_bytes(None, None), C.TOKEN_PROGRAM, "m")
        assert info.mint_authority is None
        assert info.freeze_authority is None
        assert not info.extensions.is_token_2022

    def test_live_authorities(self):
        info = parse_mint(make_mint_bytes(pk_bytes(7), pk_bytes(8)),
                          C.TOKEN_PROGRAM, "m")
        assert info.mint_authority == pk(7)
        assert info.freeze_authority == pk(8)

    def test_supply_and_decimals(self):
        info = parse_mint(make_mint_bytes(supply=123456, decimals=9),
                          C.TOKEN_PROGRAM, "m")
        assert info.supply == 123456
        assert info.decimals == 9

    def test_short_account_rejected(self):
        with pytest.raises(ValueError):
            parse_mint(b"\x00" * 10, C.TOKEN_PROGRAM)


class TestToken2022:
    def test_transfer_fee_extension(self):
        data = make_t22_mint_bytes(
            [(C.EXT_TRANSFER_FEE_CONFIG, transfer_fee_payload(bps=500))])
        ext = parse_token2022_extensions(data)
        assert ext.transfer_fee_bps == 500

    def test_transfer_hook_detected(self):
        data = make_t22_mint_bytes(
            [(C.EXT_TRANSFER_HOOK, transfer_hook_payload(pk_bytes(11)))])
        ext = parse_token2022_extensions(data)
        assert ext.has_transfer_hook
        assert ext.transfer_hook_program == pk(11)

    def test_transfer_hook_with_zero_program_is_inert(self):
        data = make_t22_mint_bytes([(C.EXT_TRANSFER_HOOK, transfer_hook_payload(None))])
        assert not parse_token2022_extensions(data).has_transfer_hook

    def test_permanent_delegate(self):
        data = make_t22_mint_bytes([(C.EXT_PERMANENT_DELEGATE, pk_bytes(12))])
        ext = parse_token2022_extensions(data)
        assert ext.has_permanent_delegate
        assert ext.permanent_delegate == pk(12)

    def test_default_frozen_state(self):
        data = make_t22_mint_bytes([(C.EXT_DEFAULT_ACCOUNT_STATE, bytes([2]))])
        assert parse_token2022_extensions(data).default_state_frozen
        data = make_t22_mint_bytes([(C.EXT_DEFAULT_ACCOUNT_STATE, bytes([1]))])
        assert not parse_token2022_extensions(data).default_state_frozen

    def test_non_transferable(self):
        data = make_t22_mint_bytes([(C.EXT_NON_TRANSFERABLE, b"")])
        assert parse_token2022_extensions(data).non_transferable

    def test_unknown_extension_recorded(self):
        data = make_t22_mint_bytes([(77, b"\x01\x02\x03")])
        assert 77 in parse_token2022_extensions(data).unknown_extensions

    def test_multiple_extensions(self):
        data = make_t22_mint_bytes([
            (C.EXT_TRANSFER_FEE_CONFIG, transfer_fee_payload(bps=100)),
            (C.EXT_PERMANENT_DELEGATE, pk_bytes(13)),
        ])
        ext = parse_token2022_extensions(data)
        assert ext.transfer_fee_bps == 100 and ext.has_permanent_delegate

    def test_parse_mint_wires_extensions(self):
        data = make_t22_mint_bytes(
            [(C.EXT_TRANSFER_HOOK, transfer_hook_payload(pk_bytes(11)))])
        info = parse_mint(data, C.TOKEN_2022_PROGRAM, "m")
        assert info.extensions.is_token_2022 and info.extensions.has_transfer_hook

    def test_classic_owner_means_no_extension_parse(self):
        info = parse_mint(make_mint_bytes(), C.TOKEN_PROGRAM, "m")
        assert not info.extensions.is_token_2022


class TestOtherAccounts:
    def test_bonding_curve(self):
        data = make_bonding_curve_bytes(30 * 10 ** 9, 10 ** 15, complete=False)
        curve = parse_bonding_curve(data)
        assert curve["virtual_sol_reserves"] == 30 * 10 ** 9
        assert curve["virtual_token_reserves"] == 10 ** 15
        assert curve["complete"] is False
        data = make_bonding_curve_bytes(1, 1, complete=True)
        assert parse_bonding_curve(data)["complete"] is True

    def test_token_account(self):
        data = pk_bytes(1) + pk_bytes(2) + struct.pack("<Q", 42) + b"\x00" * 100
        mint, owner, amount = parse_token_account(data)
        assert (mint, owner, amount) == (pk(1), pk(2), 42)
