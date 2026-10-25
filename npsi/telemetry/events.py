"""
Common telemetry event schema shared by every backend (synthetic simulator,
real BCC/eBPF probes). Nothing downstream (feature engineering, ML, control)
is allowed to know which backend produced an event -- that's what makes the
simulation and real deployment interchangeable.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class ProbePoint(str, Enum):
    """Kernel hook an event originated from (mirrors the 4 eBPF attach points)."""

    TCP_SENDMSG = "tcp_sendmsg"
    SK_BUFFER = "sk_buffer"
    QDISC = "qdisc"
    NIC_RING = "nic_ring"


@dataclass(frozen=True, slots=True)
class StallEvent:
    """
    One kernel-observed stall: a flow/queue was blocked waiting for a
    resource (buffer space, ring slot, socket buffer room) for `stall_ns`
    nanoseconds. This is the primitive PSI-style signal: not "how busy",
    but "how long was something actually stuck".
    """

    ts_ns: int  # monotonic timestamp, nanoseconds since epoch of the run
    link_id: str  # interface / link identifier, e.g. "s1-eth1"
    probe: ProbePoint
    stall_ns: int  # duration of this stall
    flow_id: str | None = None  # best-effort flow identifier, if known


@dataclass(frozen=True, slots=True)
class QueueSample:
    """
    Periodic point-in-time queue/interface sample, taken alongside stall
    events. Backlog is in packets (qdisc) or bytes (NIC ring), producer's
    choice, but must be consistent per link_id across a run.
    """

    ts_ns: int
    link_id: str
    backlog: float
    capacity: float  # backlog and capacity share units
    softirq_latency_ns: float = 0.0  # kernel interrupt-handling delay, overload signal


@dataclass(frozen=True, slots=True)
class FlowCounterSample:
    """
    Coarse, sampled flow-level counter -- deliberately modeled after
    NetFlow/sFlow: periodic, 1-in-N sampled, no stall information. Used only
    to build the flow-statistic baseline predictor for a fair comparison.
    """

    ts_ns: int
    link_id: str
    bytes_sampled: int
    packets_sampled: int
    sample_rate: int = field(default=100)  # 1-in-N


TelemetryEvent = StallEvent | QueueSample | FlowCounterSample
