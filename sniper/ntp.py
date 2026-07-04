"""SNTP clock-offset check (stdlib only).

Latency numbers are meaningless if the host clock is wrong: t0 comes from the
chain (block time), every other stage from the local clock. We measure the
offset against an NTP server at startup and refuse to label latency data as
trustworthy when the offset exceeds the configured bound.
"""

from __future__ import annotations

import logging
import socket
import struct
import time
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger(__name__)

_NTP_EPOCH_DELTA = 2208988800  # seconds between 1900-01-01 and 1970-01-01


@dataclass
class ClockCheck:
    ok: bool
    offset_ms: Optional[float]
    detail: str


def query_ntp_offset(server: str, timeout: float = 3.0) -> float:
    """Return estimated local-clock offset in seconds (positive = local fast)."""
    packet = b"\x1b" + 47 * b"\0"
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(timeout)
        t_send = time.time()
        sock.sendto(packet, (server, 123))
        data, _ = sock.recvfrom(512)
        t_recv = time.time()
    if len(data) < 48:
        raise ValueError("short NTP response")
    # Server receive (offset 32) and transmit (offset 40) timestamps
    rx_int, rx_frac = struct.unpack("!II", data[32:40])
    tx_int, tx_frac = struct.unpack("!II", data[40:48])
    t_rx = rx_int - _NTP_EPOCH_DELTA + rx_frac / 2**32
    t_tx = tx_int - _NTP_EPOCH_DELTA + tx_frac / 2**32
    # standard NTP offset: ((rx - send) + (tx - recv)) / 2
    return ((t_rx - t_send) + (t_tx - t_recv)) / 2.0 * -1.0


def check_clock(server: str, max_offset_ms: float, attempts: int = 3) -> ClockCheck:
    last_err: Optional[Exception] = None
    for _ in range(attempts):
        try:
            offset_s = query_ntp_offset(server)
            offset_ms = offset_s * 1000.0
            ok = abs(offset_ms) <= max_offset_ms
            detail = (
                f"clock offset {offset_ms:+.1f} ms vs {server} "
                f"(bound ±{max_offset_ms:.0f} ms)"
            )
            if not ok:
                detail += " — LATENCY MEASUREMENTS WILL BE NOISE until the host is NTP-synced"
            return ClockCheck(ok=ok, offset_ms=offset_ms, detail=detail)
        except Exception as exc:  # noqa: BLE001 - network probe, report and retry
            last_err = exc
    return ClockCheck(
        ok=False, offset_ms=None,
        detail=f"NTP query to {server} failed ({last_err}); cannot vouch for latency data",
    )
