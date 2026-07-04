"""Configuration: YAML file + environment overrides + pre-registration loading.

Secrets (RPC keys, wallet material) come ONLY from the environment / .env —
the YAML holds no credentials so it can be committed safely.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

from .models import RunMode, TriggerMode


def _load_dotenv(path: str = ".env") -> None:
    """Minimal .env loader (no extra dependency). Existing env vars win."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


@dataclass
class RpcConfig:
    http_url: str = ""
    ws_url: str = ""
    helius_api_key: str = ""
    request_timeout_s: float = 10.0
    commitment: str = "confirmed"
    # rolling p95 request latency above this halts new entries (risk layer)
    max_latency_ms_halt: float = 2000.0


@dataclass
class TriggerConfig:
    mode: str = TriggerMode.FILTER_EDGE.value
    # filter_edge only: deliberate delay between detection and entry decision
    entry_delay_s: float = 3.0
    # sources to listen to (all on by default; observation is cheap)
    listen_pumpfun: bool = True
    listen_pumpswap: bool = True
    listen_raydium: bool = True


@dataclass
class FilterConfig:
    min_initial_liquidity_sol: float = 5.0
    max_top_holder_pct: float = 20.0
    max_deployer_pct: float = 10.0
    max_bundle_supply_pct: float = 25.0
    min_deployer_age_days: float = 1.0
    max_deployer_prior_tokens: int = 3
    max_deployer_prior_rugs: int = 0
    max_transfer_fee_bps: int = 0          # any Token-2022 transfer fee is suspicious
    max_buy_tax_bps: int = 200             # sanity ceiling from route quotes
    max_sell_tax_bps: int = 200
    holder_top_n: int = 10
    # extra locker program IDs beyond sniper.constants.KNOWN_LOCKER_PROGRAMS
    locker_programs: dict[str, str] = field(default_factory=dict)
    # deployer cache TTL (Helius lookups are slow + rate limited)
    deployer_cache_ttl_s: float = 6 * 3600.0


@dataclass
class ScoreWeights:
    """Relative weights for soft signals when combining into the 0-1 score."""
    liquidity: float = 1.0
    holders: float = 1.5
    deployer: float = 1.5
    bundle: float = 1.5
    lp_status: float = 2.0
    honeypot: float = 2.0


@dataclass
class PaperModelConfig:
    """Honest-cost paper fill model. Every knob defaults pessimistic-realistic."""
    route_fee_bps: int = 100          # ~1% swap/route fee (pump.fun 1%, aggregators similar)
    amm_fee_bps: int = 25             # pool LP fee (Raydium 25, PumpSwap ~30 incl protocol)
    adverse_selection_bps: int = 75   # price drift between quote and land on shared RPC
    fill_probability: dict[str, float] = field(default_factory=lambda: {
        "block_0": 0.30,      # you will usually lose the race on shared infra
        "migration": 0.70,
        "filter_edge": 0.85,
    })
    base_fee_lamports: int = 5_000
    ata_rent_lamports: int = 2_039_280
    # deterministic seed for the fill RNG so runs are reproducible; null = random
    rng_seed: Optional[int] = None


@dataclass
class JitoConfig:
    enabled: bool = False
    block_engine_url: str = "https://mainnet.block-engine.jito.wtf"
    tip_lamports: int = 100_000


@dataclass
class ExecutionConfig:
    run_mode: str = RunMode.PAPER.value
    entry_size_sol: float = 0.02
    shadow_size_sol: float = 0.01      # canary trade size (0.01–0.05 SOL)
    slippage_bps: int = 500
    priority_fee_microlamports: int = 100_000   # per CU price for entries
    compute_units: int = 120_000
    # Jupiter's free-tier swap API (the old quote-api.jup.ag host is retired)
    jupiter_base_url: str = "https://lite-api.jup.ag/swap/v1"
    jito: JitoConfig = field(default_factory=JitoConfig)
    paper: PaperModelConfig = field(default_factory=PaperModelConfig)
    # live arming: BOTH this flag and the env ack must be set
    live_enabled: bool = False
    live_ack_env: str = "SNIPER_LIVE_ACK"
    live_ack_value: str = "I_UNDERSTAND_THIS_WILL_PROBABLY_LOSE_MONEY"


@dataclass
class ExitTier:
    multiple: float
    sell_pct: float


@dataclass
class ExitConfig:
    tiers: list[ExitTier] = field(default_factory=lambda: [
        ExitTier(multiple=2.0, sell_pct=50.0),
        ExitTier(multiple=5.0, sell_pct=25.0),
    ])
    stop_loss_pct: float = 60.0        # exit all if value drops 60% from entry
    max_hold_s: float = 600.0          # 10 min force-exit
    poll_interval_s: float = 2.0
    lp_drop_emergency_pct: float = 40.0  # pool SOL reserve drop => emergency exit
    # priority-fee escalation ladder for sells (multiplier on entry fee), one per retry
    fee_escalation: list[float] = field(default_factory=lambda: [1.0, 2.0, 5.0, 10.0, 20.0])
    sell_retry_delay_s: float = 1.5
    blockhash_max_age_s: float = 45.0  # rebuild tx if blockhash older than this


@dataclass
class RiskConfig:
    per_trade_cap_usd: float = 2.0
    sol_price_usd_fallback: float = 150.0
    max_concurrent_positions: int = 3
    daily_loss_cap_usd: float = 10.0
    daily_fee_cap_sol: float = 0.05    # fee bleed cap, independent of P&L
    sol_floor: float = 0.05            # never spend below this reserve (exit gas)
    kill_switch_file: str = "KILL"
    halt_on_unhandled_exception: bool = True
    kelly_discount: float = 0.10       # fraction of full Kelly if ever live-sized


@dataclass
class DashboardConfig:
    enabled: bool = True
    host: str = "127.0.0.1"   # read-only, localhost only; refuse anything else
    port: int = 8787


@dataclass
class StorageConfig:
    db_path: str = "data/sniper.db"


@dataclass
class NtpConfig:
    enabled: bool = True
    server: str = "pool.ntp.org"
    max_offset_ms: float = 250.0
    halt_on_fail: bool = False   # warn by default; latency numbers flagged as noisy


@dataclass
class Config:
    rpc: RpcConfig = field(default_factory=RpcConfig)
    trigger: TriggerConfig = field(default_factory=TriggerConfig)
    filters: FilterConfig = field(default_factory=FilterConfig)
    score_weights: ScoreWeights = field(default_factory=ScoreWeights)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    exits: ExitConfig = field(default_factory=ExitConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    dashboard: DashboardConfig = field(default_factory=DashboardConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    ntp: NtpConfig = field(default_factory=NtpConfig)
    preregistration_file: str = "config/preregistration.json"

    @property
    def trigger_mode(self) -> TriggerMode:
        return TriggerMode(self.trigger.mode)

    @property
    def run_mode(self) -> RunMode:
        return RunMode(self.execution.run_mode)


def _apply_dict(obj: Any, data: dict[str, Any]) -> None:
    """Recursively apply a dict onto a dataclass tree."""
    for key, value in data.items():
        if not hasattr(obj, key):
            raise KeyError(f"Unknown config key: {key!r} on {type(obj).__name__}")
        current = getattr(obj, key)
        if dataclasses.is_dataclass(current) and isinstance(value, dict):
            _apply_dict(current, value)
        elif key == "tiers" and isinstance(value, list):
            obj.tiers = [ExitTier(**t) for t in value]
        else:
            setattr(obj, key, value)


def load_config(path: str | None = None, env: dict[str, str] | None = None) -> Config:
    _load_dotenv()
    env = env if env is not None else dict(os.environ)
    cfg = Config()
    if path:
        raw = yaml.safe_load(Path(path).read_text()) or {}
        _apply_dict(cfg, raw)

    # secrets / env overrides — env always wins for connection details
    cfg.rpc.http_url = env.get("SNIPER_RPC_HTTP_URL", cfg.rpc.http_url)
    cfg.rpc.ws_url = env.get("SNIPER_RPC_WS_URL", cfg.rpc.ws_url)
    cfg.rpc.helius_api_key = env.get("SNIPER_HELIUS_API_KEY", cfg.rpc.helius_api_key)
    if env.get("SNIPER_RUN_MODE"):
        cfg.execution.run_mode = env["SNIPER_RUN_MODE"]
    if env.get("SNIPER_TRIGGER_MODE"):
        cfg.trigger.mode = env["SNIPER_TRIGGER_MODE"]

    validate_config(cfg, env)
    return cfg


def validate_config(cfg: Config, env: dict[str, str]) -> None:
    RunMode(cfg.execution.run_mode)      # raises on typo
    TriggerMode(cfg.trigger.mode)
    if cfg.dashboard.host not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError(
            "Dashboard must bind to localhost only. Refusing host "
            f"{cfg.dashboard.host!r} — this dashboard reads your trade log."
        )
    total = sum(t.sell_pct for t in cfg.exits.tiers)
    if total > 100.0:
        raise ValueError(f"Exit tiers sell {total}% > 100% of the position")
    if cfg.run_mode == RunMode.LIVE:
        ack = env.get(cfg.execution.live_ack_env, "")
        if not cfg.execution.live_enabled:
            raise ValueError("run_mode=live but execution.live_enabled is false in config")
        if ack != cfg.execution.live_ack_value:
            raise ValueError(
                f"run_mode=live requires env {cfg.execution.live_ack_env}="
                f"{cfg.execution.live_ack_value!r}. Refusing to arm."
            )


# ---------------------------------------------------------------------------
# Pre-registration: the score threshold is frozen BEFORE evaluation data is
# examined. The file's hash is recorded in the DB on first run; the evaluation
# protocol refuses a GO verdict if the file changed mid-sample.
# ---------------------------------------------------------------------------

@dataclass
class Preregistration:
    score_threshold: float
    trigger_mode: str
    registered_at: str
    notes: str = ""
    sha256: str = ""

    @staticmethod
    def load(path: str) -> "Preregistration":
        raw_bytes = Path(path).read_bytes()
        raw = json.loads(raw_bytes)
        pre = Preregistration(
            score_threshold=float(raw["score_threshold"]),
            trigger_mode=str(raw["trigger_mode"]),
            registered_at=str(raw["registered_at"]),
            notes=str(raw.get("notes", "")),
        )
        pre.sha256 = hashlib.sha256(raw_bytes).hexdigest()
        TriggerMode(pre.trigger_mode)
        if not (0.0 <= pre.score_threshold <= 1.0):
            raise ValueError("score_threshold must be within [0, 1]")
        return pre
