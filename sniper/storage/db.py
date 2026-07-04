"""SQLite persistence.

Design notes:
- WAL mode so the read-only dashboard can read while the bot writes.
- Trades carry the full fee breakdown, signatures and timestamps so the log
  can double as a tax record (swaps are taxable events in most jurisdictions).
- The chain is the source of truth for crash recovery; this DB is the journal.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Optional

from ..models import (
    FillResult, FilterResult, LaunchEvent, PipelineOutcome, Position, RunMode, TriggerMode,
)

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS launches (
    id INTEGER PRIMARY KEY,
    mint TEXT NOT NULL,
    source TEXT NOT NULL,
    signature TEXT,
    slot INTEGER,
    block_time REAL,
    creator TEXT,
    pool TEXT,
    base_vault TEXT,
    quote_vault TEXT,
    lp_mint TEXT,
    detected_wall REAL NOT NULL,
    created_at REAL NOT NULL,
    UNIQUE(mint, source)
);
CREATE INDEX IF NOT EXISTS idx_launches_time ON launches(created_at);

CREATE TABLE IF NOT EXISTS filter_results (
    id INTEGER PRIMARY KEY,
    launch_id INTEGER NOT NULL REFERENCES launches(id),
    name TEXT NOT NULL,
    passed INTEGER NOT NULL,
    hard INTEGER NOT NULL,
    skipped INTEGER NOT NULL DEFAULT 0,
    reason TEXT,
    score REAL,
    elapsed_ms REAL,
    data_json TEXT,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_filter_launch ON filter_results(launch_id);

CREATE TABLE IF NOT EXISTS pipeline_outcomes (
    id INTEGER PRIMARY KEY,
    launch_id INTEGER NOT NULL REFERENCES launches(id),
    trigger_mode TEXT NOT NULL,
    score REAL NOT NULL,
    threshold REAL NOT NULL,
    accepted INTEGER NOT NULL,
    rejected_by TEXT,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY,
    launch_id INTEGER NOT NULL REFERENCES launches(id),
    mint TEXT NOT NULL,
    pool TEXT,
    mode TEXT NOT NULL,
    trigger_mode TEXT NOT NULL,
    tokens_total INTEGER NOT NULL DEFAULT 0,
    tokens_remaining INTEGER NOT NULL DEFAULT 0,
    sol_spent INTEGER NOT NULL DEFAULT 0,
    sol_received INTEGER NOT NULL DEFAULT 0,
    entry_price REAL NOT NULL DEFAULT 0,
    entry_sol_reserve INTEGER NOT NULL DEFAULT 0,
    decimals INTEGER NOT NULL DEFAULT 6,
    tiers_filled INTEGER NOT NULL DEFAULT 0,
    opened_at REAL NOT NULL,
    closed_at REAL,
    state TEXT NOT NULL DEFAULT 'open',
    outcome TEXT
);
CREATE INDEX IF NOT EXISTS idx_positions_state ON positions(state);
-- double-buy guard: at most one open position per mint+mode
CREATE UNIQUE INDEX IF NOT EXISTS idx_positions_open_mint
    ON positions(mint, mode) WHERE state = 'open';

CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY,
    position_id INTEGER REFERENCES positions(id),
    launch_id INTEGER REFERENCES launches(id),
    mode TEXT NOT NULL,               -- paper | shadow | live
    side TEXT NOT NULL,               -- buy | sell
    filled INTEGER NOT NULL,
    reason TEXT,                      -- exit reason / no-fill reason
    sol_delta INTEGER NOT NULL,       -- lamports, negative = out of wallet (fees incl.)
    tokens_delta INTEGER NOT NULL,
    effective_price REAL,
    mid_price REAL,
    slippage_bps REAL,
    fill_probability REAL,
    fee_base INTEGER NOT NULL DEFAULT 0,
    fee_priority INTEGER NOT NULL DEFAULT 0,
    fee_jito INTEGER NOT NULL DEFAULT 0,
    fee_ata_rent INTEGER NOT NULL DEFAULT 0,
    fee_route INTEGER NOT NULL DEFAULT 0,
    signature TEXT,                   -- real chain signature when shadow/live
    slot_sent INTEGER,
    slot_landed INTEGER,
    wall_time REAL NOT NULL           -- execution timestamp (tax record)
);
CREATE INDEX IF NOT EXISTS idx_trades_position ON trades(position_id);
CREATE INDEX IF NOT EXISTS idx_trades_time ON trades(wall_time);

CREATE TABLE IF NOT EXISTS latency_samples (
    id INTEGER PRIMARY KEY,
    launch_id INTEGER REFERENCES launches(id),
    t0_block_time REAL,
    t0_slot INTEGER,
    event_seen_ms REAL,
    filters_done_ms REAL,
    tx_built_ms REAL,
    tx_sent_ms REAL,
    tx_landed_ms REAL,
    slot_delta INTEGER,
    clock_trusted INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS deployer_cache (
    wallet TEXT PRIMARY KEY,
    first_tx_time REAL,
    funding_source TEXT,
    funding_source_flagged INTEGER NOT NULL DEFAULT 0,
    tokens_created INTEGER NOT NULL DEFAULT 0,
    prior_rugs INTEGER NOT NULL DEFAULT 0,
    fetched_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS calibration (
    id INTEGER PRIMARY KEY,
    trade_id INTEGER REFERENCES trades(id),
    predicted_fill_prob REAL,
    actually_filled INTEGER,
    predicted_slippage_bps REAL,
    actual_slippage_bps REAL,
    predicted_tokens INTEGER,
    actual_tokens INTEGER,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS risk_events (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    detail TEXT,
    created_at REAL NOT NULL
);
"""


class Database:
    """Thread-safe synchronous SQLite wrapper. Writes are short; a lock keeps
    the async code honest without an aiosqlite dependency."""

    def __init__(self, path: str):
        self.path = path
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- low level ----------------------------------------------------------
    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, tuple(params))
            self._conn.commit()
            return cur

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, tuple(params)).fetchall()

    # -- meta / preregistration ---------------------------------------------
    def get_meta(self, key: str) -> Optional[str]:
        rows = self.query("SELECT value FROM meta WHERE key=?", (key,))
        return rows[0]["value"] if rows else None

    def set_meta(self, key: str, value: str) -> None:
        self.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def record_preregistration(self, sha256: str, payload: str) -> bool:
        """Record the preregistration hash once. Returns False if a different
        hash was already recorded (i.e. the file changed mid-sample)."""
        existing = self.get_meta("prereg_sha256")
        if existing is None:
            self.set_meta("prereg_sha256", sha256)
            self.set_meta("prereg_payload", payload)
            self.set_meta("prereg_recorded_at", str(time.time()))
            return True
        return existing == sha256

    # -- launches -------------------------------------------------------------
    def insert_launch(self, ev: LaunchEvent) -> Optional[int]:
        """Insert a launch; returns row id, or None if it was a duplicate
        (mint+source unique constraint — the double-event guard)."""
        try:
            cur = self.execute(
                "INSERT INTO launches(mint, source, signature, slot, block_time, creator,"
                " pool, base_vault, quote_vault, lp_mint, detected_wall, created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (ev.mint, ev.source.value, ev.signature, ev.slot, ev.block_time,
                 ev.creator, ev.pool, ev.base_vault, ev.quote_vault, ev.lp_mint,
                 ev.detected_wall, time.time()),
            )
            return cur.lastrowid
        except sqlite3.IntegrityError:
            return None

    # -- filters / pipeline ---------------------------------------------------
    def insert_filter_results(self, launch_id: int, results: list[FilterResult]) -> None:
        now = time.time()
        with self._lock:
            self._conn.executemany(
                "INSERT INTO filter_results(launch_id, name, passed, hard, skipped,"
                " reason, score, elapsed_ms, data_json, created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                [(launch_id, r.name, int(r.passed), int(r.hard), int(r.skipped),
                  r.reason, r.score, r.elapsed_ms, json.dumps(r.data, default=str), now)
                 for r in results],
            )
            self._conn.commit()

    def insert_pipeline_outcome(self, launch_id: int, out: PipelineOutcome,
                                trigger_mode: TriggerMode) -> None:
        self.execute(
            "INSERT INTO pipeline_outcomes(launch_id, trigger_mode, score, threshold,"
            " accepted, rejected_by, created_at) VALUES(?,?,?,?,?,?,?)",
            (launch_id, trigger_mode.value, out.score, out.threshold,
             int(out.accepted), out.rejected_by, time.time()),
        )

    # -- positions / trades ---------------------------------------------------
    def insert_position(self, pos: Position) -> Optional[int]:
        try:
            cur = self.execute(
                "INSERT INTO positions(launch_id, mint, pool, mode, trigger_mode,"
                " tokens_total, tokens_remaining, sol_spent, sol_received, entry_price,"
                " entry_sol_reserve, decimals, tiers_filled, opened_at, state)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (pos.launch_id, pos.mint, pos.pool, pos.mode.value, pos.trigger_mode.value,
                 pos.tokens_total, pos.tokens_remaining, pos.sol_spent, pos.sol_received,
                 pos.entry_price, pos.entry_sol_reserve, pos.decimals, pos.tiers_filled,
                 pos.opened_at, pos.state),
            )
            return cur.lastrowid
        except sqlite3.IntegrityError:
            return None  # double-buy guard tripped

    def update_position(self, pos: Position) -> None:
        self.execute(
            "UPDATE positions SET tokens_remaining=?, sol_spent=?, sol_received=?,"
            " tiers_filled=?, state=?, outcome=?, closed_at=? WHERE id=?",
            (pos.tokens_remaining, pos.sol_spent, pos.sol_received, pos.tiers_filled,
             pos.state, pos.outcome, pos.closed_at, pos.id),
        )

    def open_positions(self, mode: Optional[RunMode] = None) -> list[Position]:
        sql = "SELECT * FROM positions WHERE state='open'"
        params: tuple = ()
        if mode:
            sql += " AND mode=?"
            params = (mode.value,)
        return [self._row_to_position(r) for r in self.query(sql, params)]

    def has_open_position(self, mint: str, mode: RunMode) -> bool:
        rows = self.query(
            "SELECT 1 FROM positions WHERE mint=? AND mode=? AND state='open' LIMIT 1",
            (mint, mode.value))
        return bool(rows)

    @staticmethod
    def _row_to_position(r: sqlite3.Row) -> Position:
        return Position(
            id=r["id"], launch_id=r["launch_id"], mint=r["mint"], pool=r["pool"],
            mode=RunMode(r["mode"]), trigger_mode=TriggerMode(r["trigger_mode"]),
            tokens_total=r["tokens_total"], tokens_remaining=r["tokens_remaining"],
            sol_spent=r["sol_spent"], sol_received=r["sol_received"],
            entry_price=r["entry_price"], entry_sol_reserve=r["entry_sol_reserve"],
            opened_at=r["opened_at"], closed_at=r["closed_at"], state=r["state"],
            outcome=r["outcome"], tiers_filled=r["tiers_filled"], decimals=r["decimals"],
        )

    def insert_trade(self, fill: FillResult, mode: RunMode,
                     position_id: Optional[int], launch_id: Optional[int],
                     slot_sent: Optional[int] = None,
                     slot_landed: Optional[int] = None) -> int:
        cur = self.execute(
            "INSERT INTO trades(position_id, launch_id, mode, side, filled, reason,"
            " sol_delta, tokens_delta, effective_price, mid_price, slippage_bps,"
            " fill_probability, fee_base, fee_priority, fee_jito, fee_ata_rent,"
            " fee_route, signature, slot_sent, slot_landed, wall_time)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (position_id, launch_id, mode.value, fill.side, int(fill.filled), fill.reason,
             fill.sol_delta, fill.tokens_delta, fill.effective_price, fill.mid_price,
             fill.slippage_bps, fill.fill_probability, fill.fees.base_fee,
             fill.fees.priority_fee, fill.fees.jito_tip, fill.fees.ata_rent,
             fill.fees.route_fee, fill.signature, slot_sent, slot_landed, time.time()),
        )
        return cur.lastrowid or 0

    # -- latency ---------------------------------------------------------------
    def insert_latency(self, launch_id: Optional[int], offsets: dict[str, Optional[float]],
                       t0_block_time: Optional[float], t0_slot: Optional[int],
                       clock_trusted: bool) -> None:
        self.execute(
            "INSERT INTO latency_samples(launch_id, t0_block_time, t0_slot, event_seen_ms,"
            " filters_done_ms, tx_built_ms, tx_sent_ms, tx_landed_ms, slot_delta,"
            " clock_trusted, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (launch_id, t0_block_time, t0_slot, offsets.get("event_seen"),
             offsets.get("filters_done"), offsets.get("tx_built"), offsets.get("tx_sent"),
             offsets.get("tx_landed"), offsets.get("slot_delta"), int(clock_trusted),
             time.time()),
        )

    # -- deployer cache ---------------------------------------------------------
    def get_deployer(self, wallet: str, max_age_s: float) -> Optional[sqlite3.Row]:
        rows = self.query(
            "SELECT * FROM deployer_cache WHERE wallet=? AND fetched_at > ?",
            (wallet, time.time() - max_age_s))
        return rows[0] if rows else None

    def put_deployer(self, wallet: str, first_tx_time: Optional[float],
                     funding_source: Optional[str], funding_source_flagged: bool,
                     tokens_created: int, prior_rugs: int) -> None:
        self.execute(
            "INSERT INTO deployer_cache(wallet, first_tx_time, funding_source,"
            " funding_source_flagged, tokens_created, prior_rugs, fetched_at)"
            " VALUES(?,?,?,?,?,?,?) ON CONFLICT(wallet) DO UPDATE SET"
            " first_tx_time=excluded.first_tx_time, funding_source=excluded.funding_source,"
            " funding_source_flagged=excluded.funding_source_flagged,"
            " tokens_created=excluded.tokens_created, prior_rugs=excluded.prior_rugs,"
            " fetched_at=excluded.fetched_at",
            (wallet, first_tx_time, funding_source, int(funding_source_flagged),
             tokens_created, prior_rugs, time.time()),
        )

    # -- calibration (shadow) -----------------------------------------------------
    def insert_calibration(self, trade_id: int, predicted_fill_prob: float,
                           actually_filled: bool, predicted_slippage_bps: float,
                           actual_slippage_bps: Optional[float],
                           predicted_tokens: int, actual_tokens: Optional[int]) -> None:
        self.execute(
            "INSERT INTO calibration(trade_id, predicted_fill_prob, actually_filled,"
            " predicted_slippage_bps, actual_slippage_bps, predicted_tokens,"
            " actual_tokens, created_at) VALUES(?,?,?,?,?,?,?,?)",
            (trade_id, predicted_fill_prob, int(actually_filled), predicted_slippage_bps,
             actual_slippage_bps, predicted_tokens, actual_tokens, time.time()),
        )

    # -- risk events ---------------------------------------------------------------
    def insert_risk_event(self, kind: str, detail: str) -> None:
        self.execute(
            "INSERT INTO risk_events(kind, detail, created_at) VALUES(?,?,?)",
            (kind, detail, time.time()),
        )

    # -- aggregates for risk layer ---------------------------------------------------
    def fees_spent_since(self, since: float, modes: tuple[str, ...] = ("shadow", "live")) -> int:
        """Unrecoverable lamports spent on fees since `since` (real modes only
        by default — paper fees are simulated)."""
        marks = ",".join("?" * len(modes))
        rows = self.query(
            f"SELECT COALESCE(SUM(fee_base + fee_priority + fee_jito + fee_route),0) t"
            f" FROM trades WHERE wall_time > ? AND mode IN ({marks})",
            (since, *modes))
        return int(rows[0]["t"])

    def realized_pnl_since(self, since: float, mode: RunMode) -> int:
        rows = self.query(
            "SELECT COALESCE(SUM(sol_received - sol_spent),0) t FROM positions"
            " WHERE state='closed' AND closed_at > ? AND mode=?",
            (since, mode.value))
        return int(rows[0]["t"])
