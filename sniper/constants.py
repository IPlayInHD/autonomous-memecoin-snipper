"""Well-known Solana program IDs and network constants.

All values are public mainnet program addresses. Overridable via config so a
program migration doesn't require a code change.
"""

LAMPORTS_PER_SOL = 1_000_000_000

# SPL token programs
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
ASSOCIATED_TOKEN_PROGRAM = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
SYSTEM_PROGRAM = "11111111111111111111111111111111"

# Wrapped SOL
WSOL_MINT = "So11111111111111111111111111111111111111112"

# pump.fun bonding curve program
PUMPFUN_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
# PumpSwap AMM (pump.fun graduation target since 2025)
PUMPSWAP_PROGRAM = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
# pump.fun migration authority wallet (signs graduations)
PUMPFUN_MIGRATION_AUTHORITY = "39azUYFWPz3VHgKCf3VChUwbpURdCHRxjWVowf5jUJjg"

# Raydium
RAYDIUM_AMM_V4 = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"
RAYDIUM_CPMM = "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C"
RAYDIUM_AMM_AUTHORITY = "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1"

# LP-burn destination
INCINERATOR = "1nc1nerator11111111111111111111111111111111"

# Known LP locker programs (extend via config `filters.locker_programs`).
# "Locked" is only recognized for these specific programs; an unknown holder
# of the LP supply is treated as UNLOCKED (i.e. rug-capable).
KNOWN_LOCKER_PROGRAMS = {
    "strmRqUCoQUgGUan5YhzUZa6KqdzwX5L6FpUxfmKg5m": "streamflow",
    "LocpQgucEQHbqNABEYvBvwoxCPsSbG91A1QaQhQQqjn": "raydium_lp_locker",
    "DCA265Vj8a9CEuX1eb1LWRnDT7uK6q1xMipnNyatn23M": "jupiter_lock",
    "GDDMwNyyx8uB6zrqwBFHjLLG3TBYk2F8Az4yrQC5RzMp": "goki_smart_wallet",
}

# Token-2022 extension type codes (spl-token-2022 ExtensionType enum)
EXT_TRANSFER_FEE_CONFIG = 1
EXT_MINT_CLOSE_AUTHORITY = 3
EXT_DEFAULT_ACCOUNT_STATE = 6
EXT_NON_TRANSFERABLE = 9
EXT_PERMANENT_DELEGATE = 12
EXT_TRANSFER_HOOK = 14
EXT_CONFIDENTIAL_TRANSFER_MINT = 4
# benign metadata extensions (name/symbol/URI on-chain). pump.fun mints all
# Token-2022 tokens with these two; they carry no transfer-control power.
EXT_METADATA_POINTER = 18
EXT_TOKEN_METADATA = 19

# Base transaction fee per signature
BASE_FEE_LAMPORTS = 5_000
# Rent-exempt minimum for a token account (recoverable when the ATA is closed)
ATA_RENT_LAMPORTS = 2_039_280

# SPL mint account layout (classic): 82 bytes
MINT_ACCOUNT_BASE_LEN = 82
# Token-2022 account-type discriminator offset (after base + padding to 165)
T22_ACCOUNT_TYPE_OFFSET = 165
