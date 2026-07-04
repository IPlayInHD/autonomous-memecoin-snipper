"""Core data models. Plain dataclasses + str pubkeys so the logic layer has no
hard dependency on solders — chain adapters convert at the boundary."""

from __future__ import annotations

import enum
import time
from dataclasses import dataclass, field
from typing import Any, Optional


class LaunchSource(str, enum.Enum):
    PUMPFUN_LAUNCH = "pumpfun_launch"          # new bonding-curve token
    PUMPFUN_MIGRATION = "pumpfun_migration"    # graduation to PumpSwap
    PUMPSWAP_POOL = "pumpswap_pool"            # new PumpSwap AMM pool
    RAYDIUM_POOL = "raydium_pool"              # new Raydium AMM v4 pool


class TriggerMode(str, enum.Enum):
    BLOCK_0 = "block_0"          # first slot after pool creation (latency control arm)
    MIGRATION = "migration"      # buy on pump.fun graduation
    FILTER_EDGE = "filter_edge"  # delayed entry gated on filter quality


class RunMode(str, enum.Enum):
    OBSERVE = "observe"  # detection + filters only, no execution at all
    PAPER = "paper"      # simulated fills from real pool state (DEFAULT)
    SHADOW = "shadow"    # tiny real trades purely to calibrate the paper model
    LIVE = "live"        # double-gated real trading


class ExitOutcome(str, enum.Enum):
    TAKE_PROFIT = "take_profit"
    STOP_LOSS = "stop_loss"
    MAX_HOLD = "max_hold"
    EMERGENCY_LP_PULL = "emergency_lp_pull"
    COULD_NOT_SELL = "could_not_sell"   # rug: sell route dead. NOT a normal loss.
    MANUAL_HALT = "manual_halt"
    RECONCILED_GONE = "reconciled_gone"  # crash recovery found no tokens on chain


@dataclass
class LatencyBudget:
    """Full pipeline latency budget, all wall-clock seconds (host must be NTP-synced,
    see sniper.ntp). t0 = block time of the pool-creation transaction."""
    t0_block_time: Optional[float] = None
    t0_slot: Optional[int] = None
    event_seen: Optional[float] = None
    filters_done: Optional[float] = None
    tx_built: Optional[float] = None
    tx_sent: Optional[float] = None
    tx_landed: Optional[float] = None
    landed_slot: Optional[int] = None

    def offsets_ms(self) -> dict[str, Optional[float]]:
        """Each stage as milliseconds after t0 (None where unknown)."""
        out: dict[str, Optional[float]] = {}
        for name in ("event_seen", "filters_done", "tx_built", "tx_sent", "tx_landed"):
            v = getattr(self, name)
            out[name] = None if (v is None or self.t0_block_time is None) \
                else (v - self.t0_block_time) * 1000.0
        out["slot_delta"] = (
            None if (self.landed_slot is None or self.t0_slot is None)
            else self.landed_slot - self.t0_slot
        )
        return out


@dataclass
class LaunchEvent:
    mint: str
    source: LaunchSource
    signature: str
    slot: int
    creator: Optional[str] = None
    pool: Optional[str] = None            # AMM pool / bonding curve address
    base_vault: Optional[str] = None      # token-side vault (AMM pools)
    quote_vault: Optional[str] = None     # SOL/WSOL-side vault
    lp_mint: Optional[str] = None
    block_time: Optional[float] = None
    detected_wall: float = field(default_factory=time.time)
    detected_mono: float = field(default_factory=time.monotonic)
    latency: LatencyBudget = field(default_factory=LatencyBudget)
    raw: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.latency.t0_block_time = self.block_time
        self.latency.t0_slot = self.slot
        self.latency.event_seen = self.detected_wall


@dataclass
class Token2022Extensions:
    is_token_2022: bool = False
    transfer_fee_bps: int = 0
    max_transfer_fee: int = 0
    has_transfer_hook: bool = False
    transfer_hook_program: Optional[str] = None
    has_permanent_delegate: bool = False
    permanent_delegate: Optional[str] = None
    default_state_frozen: bool = False
    non_transferable: bool = False
    has_confidential_transfer: bool = False
    unknown_extensions: list[int] = field(default_factory=list)


@dataclass
class MintInfo:
    address: str
    owner_program: str
    decimals: int
    supply: int
    mint_authority: Optional[str]     # None = revoked
    freeze_authority: Optional[str]   # None = revoked
    extensions: Token2022Extensions = field(default_factory=Token2022Extensions)


@dataclass
class TokenHolding:
    address: str          # token account
    owner: Optional[str]  # wallet owning the token account
    amount: int           # raw units


@dataclass
class PoolState:
    pool: str
    token_mint: str
    sol_reserve: int        # lamports (or virtual SOL reserve for bonding curves)
    token_reserve: int      # raw token units
    lp_mint: Optional[str] = None
    lp_supply: Optional[int] = None
    is_bonding_curve: bool = False
    curve_complete: bool = False
    fetched_at: float = field(default_factory=time.time)


@dataclass
class SimulateResult:
    success: bool
    error: Optional[str] = None
    logs: list[str] = field(default_factory=list)
    units_consumed: Optional[int] = None


@dataclass
class DeployerInfo:
    wallet: str
    first_tx_time: Optional[float] = None       # unix ts of oldest known tx
    funding_source: Optional[str] = None        # wallet that first funded it
    funding_source_flagged: bool = False        # funder matched a flag list / fresh-wallet chain
    tokens_created: int = 0                     # prior mints created by this wallet
    prior_rugs: int = 0                         # prior mints whose pools drained
    fetched_at: float = field(default_factory=time.time)

    @property
    def age_days(self) -> Optional[float]:
        if self.first_tx_time is None:
            return None
        return max(0.0, (time.time() - self.first_tx_time) / 86400.0)


@dataclass
class FilterResult:
    name: str
    passed: bool
    hard: bool                 # hard fail zeroes the score; soft fail only lowers it
    reason: str = ""
    score: float = 1.0         # 0..1 quality contribution when passed
    elapsed_ms: float = 0.0
    skipped: bool = False      # e.g. slow filter skipped in block_0 mode
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class PipelineOutcome:
    launch: LaunchEvent
    results: list[FilterResult]
    score: float
    accepted: bool
    rejected_by: Optional[str] = None
    threshold: float = 1.0

    def rejection_reasons(self) -> list[str]:
        return [f"{r.name}: {r.reason}" for r in self.results if not r.passed and not r.skipped]


@dataclass
class FeeBreakdown:
    """Every lamport a trade costs beyond the AMM math itself."""
    base_fee: int = 0
    priority_fee: int = 0
    jito_tip: int = 0
    ata_rent: int = 0          # recoverable when ATA closed; tracked separately
    route_fee: int = 0         # aggregator/route fee taken from the swap

    @property
    def total(self) -> int:
        return self.base_fee + self.priority_fee + self.jito_tip + self.ata_rent + self.route_fee

    @property
    def total_unrecoverable(self) -> int:
        return self.total - self.ata_rent

    def as_dict(self) -> dict[str, int]:
        return {
            "base_fee": self.base_fee, "priority_fee": self.priority_fee,
            "jito_tip": self.jito_tip, "ata_rent": self.ata_rent, "route_fee": self.route_fee,
        }


@dataclass
class FillResult:
    filled: bool
    side: str                        # "buy" | "sell"
    sol_delta: int = 0               # lamports out of (-) / into (+) the wallet, incl. fees
    tokens_delta: int = 0            # raw token units received (+) / spent (-)
    effective_price: float = 0.0     # SOL per token actually paid/received
    mid_price: float = 0.0           # pre-trade pool mid
    slippage_bps: float = 0.0        # vs mid, incl. price impact + adverse selection
    fees: FeeBreakdown = field(default_factory=FeeBreakdown)
    fill_probability: float = 1.0    # model prob this fill would land at all
    reason: str = ""                 # why unfilled / context
    signature: Optional[str] = None  # real tx signature (shadow/live)


@dataclass
class Position:
    id: Optional[int]
    launch_id: int
    mint: str
    pool: Optional[str]
    mode: RunMode
    trigger_mode: TriggerMode
    tokens_total: int = 0            # tokens bought at entry
    tokens_remaining: int = 0
    sol_spent: int = 0               # lamports, fees included
    sol_received: int = 0            # lamports, fees deducted
    entry_price: float = 0.0         # SOL/token at entry (net)
    entry_sol_reserve: int = 0       # pool SOL reserve at entry (LP-pull baseline)
    opened_at: float = field(default_factory=time.time)
    closed_at: Optional[float] = None
    state: str = "open"
    outcome: Optional[str] = None
    tiers_filled: int = 0
    decimals: int = 6

    @property
    def net_pnl_lamports(self) -> int:
        return self.sol_received - self.sol_spent

    @property
    def is_open(self) -> bool:
        return self.state == "open"
