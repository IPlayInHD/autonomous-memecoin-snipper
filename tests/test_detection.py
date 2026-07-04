"""Transaction extraction + log-marker matching for the detection layer."""

from conftest import pk
from sniper import constants as C
from sniper.detection.listeners import (
    extract_pumpfun_create, extract_pumpswap_create_pool,
    extract_raydium_initialize2, match_log_marker,
)


def tx_with(program: str, accounts: list[str], post_mints: list[str] = ()):
    return {
        "slot": 100,
        "blockTime": 1_700_000_000,
        "transaction": {"message": {
            "accountKeys": [{"pubkey": accounts[0] if accounts else pk(0),
                             "signer": True}],
            "instructions": [{"programId": program, "accounts": accounts}],
        }},
        "meta": {"innerInstructions": [],
                 "postTokenBalances": [{"mint": m, "owner": pk(99),
                                        "accountIndex": i,
                                        "uiTokenAmount": {"amount": "10"}}
                                       for i, m in enumerate(post_mints)]},
    }


class TestPumpfunCreate:
    def test_extracts_mint_curve_creator(self):
        accounts = [pk(1), pk(2), pk(3), pk(4), pk(5), pk(6), pk(7), pk(8), pk(9)]
        tx = tx_with(C.PUMPFUN_PROGRAM, accounts, post_mints=[pk(1)])
        out = extract_pumpfun_create(tx)
        assert out == {"mint": pk(1), "pool": pk(3), "creator": pk(8)}

    def test_balance_fallback_when_no_instruction_match(self):
        tx = tx_with("SomeOtherProgram", [pk(1)] * 9, post_mints=[pk(42)])
        out = extract_pumpfun_create(tx)
        assert out["mint"] == pk(42)

    def test_none_when_nothing_extractable(self):
        tx = tx_with("SomeOtherProgram", [], post_mints=[])
        assert extract_pumpfun_create(tx) is None


class TestPumpswapCreatePool:
    def accounts(self):
        a = [pk(i) for i in range(12)]
        a[3] = pk(20)            # base mint = token
        a[4] = C.WSOL_MINT       # quote = WSOL
        return a

    def test_extracts_pool_fields(self):
        tx = tx_with(C.PUMPSWAP_PROGRAM, self.accounts(), post_mints=[pk(20)])
        out = extract_pumpswap_create_pool(tx)
        assert out["mint"] == pk(20)
        assert out["pool"] == pk(0)
        assert out["creator"] == pk(2)
        assert out["lp_mint"] == pk(5)
        assert out["base_vault"] == pk(9)     # token-side vault
        assert out["quote_vault"] == pk(10)   # WSOL-side vault

    def test_wsol_as_base_swaps_vaults(self):
        a = self.accounts()
        a[3], a[4] = C.WSOL_MINT, pk(20)
        tx = tx_with(C.PUMPSWAP_PROGRAM, a, post_mints=[pk(20)])
        out = extract_pumpswap_create_pool(tx)
        assert out["mint"] == pk(20)
        assert out["base_vault"] == pk(10)
        assert out["quote_vault"] == pk(9)


class TestRaydiumInitialize2:
    def accounts(self):
        a = [pk(i) for i in range(21)]
        a[8] = pk(20)            # coin mint = token
        a[9] = C.WSOL_MINT       # pc mint = WSOL
        return a

    def test_extracts_amm_fields(self):
        tx = tx_with(C.RAYDIUM_AMM_V4, self.accounts(), post_mints=[pk(20)])
        out = extract_raydium_initialize2(tx)
        assert out["mint"] == pk(20)
        assert out["pool"] == pk(4)
        assert out["lp_mint"] == pk(7)
        assert out["base_vault"] == pk(10)
        assert out["quote_vault"] == pk(11)
        assert out["creator"] == pk(17)

    def test_non_sol_pair_skipped(self):
        a = self.accounts()
        a[9] = pk(30)            # USDC pair, not SOL
        tx = tx_with(C.RAYDIUM_AMM_V4, a, post_mints=[pk(20)])
        assert extract_raydium_initialize2(tx) is None


class TestLogMarkers:
    def test_create_marker(self):
        logs = ["Program log: Instruction: Create", "Program log: ok"]
        assert match_log_marker(logs, ("Instruction: Create",))

    def test_no_match(self):
        assert not match_log_marker(["Program log: Instruction: Sell"],
                                    ("Instruction: Create",))
