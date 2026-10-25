"""
Orchestrates raw telemetry events -> windowed N-PSI feature vectors, per
link. This is the only place that knows how to turn a batch of
StallEvent/QueueSample objects into the feature representation the ML layer
and the labeling logic consume.

Windowing is tick-count based (not wall-clock-delta based) for determinism:
the caller tells us how many ticks make up one window given its known
sampling interval (`dt_s`).
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from npsi.features.npsi_score import compute_npsi_score
from npsi.features.supporting import burst_arrival_rate, queue_depth_trend, softirq_latency_avg
from npsi.telemetry.events import ProbePoint, QueueSample, StallEvent, TelemetryEvent

FEATURE_NAMES = ["npsi_some", "npsi_full", "queue_trend", "softirq_latency_avg_ns", "burst_arrival_rate"]


@dataclass(frozen=True, slots=True)
class FeatureVector:
    ts_ns: int
    link_id: str
    npsi_some: float
    npsi_full: float
    queue_trend: float
    softirq_latency_avg_ns: float
    burst_arrival_rate: float
    backlog_ratio_last: float  # ground-truth occupancy at window end; used ONLY for labeling, never fed to the model

    def to_array(self) -> list[float]:
        return [self.npsi_some, self.npsi_full, self.queue_trend, self.softirq_latency_avg_ns, self.burst_arrival_rate]


@dataclass
class _TickRecord:
    ts_ns: int
    has_stall: bool = False
    has_full_stall: bool = False
    backlog_ratio: float = 0.0
    softirq_latency_ns: float = 0.0


class NPsiFeaturePipeline:
    def __init__(self, dt_s: float, window_s: float = 0.05):
        self.dt_s = dt_s
        self.window_s = window_s
        self.window_ticks = max(1, round(window_s / dt_s))
        self._buffers: dict[str, list[_TickRecord]] = defaultdict(list)

    def process_batch(self, events: list[TelemetryEvent]) -> list[FeatureVector]:
        by_link_ts: dict[tuple[str, int], _TickRecord] = {}
        for ev in events:
            key = (ev.link_id, ev.ts_ns)
            rec = by_link_ts.setdefault(key, _TickRecord(ts_ns=ev.ts_ns))
            if isinstance(ev, QueueSample):
                rec.backlog_ratio = ev.backlog / ev.capacity if ev.capacity else 0.0
                rec.softirq_latency_ns = ev.softirq_latency_ns
            elif isinstance(ev, StallEvent):
                rec.has_stall = True
                if ev.probe == ProbePoint.NIC_RING:
                    rec.has_full_stall = True

        out: list[FeatureVector] = []
        for (link_id, _ts_ns), rec in by_link_ts.items():
            buf = self._buffers[link_id]
            buf.append(rec)
            if len(buf) >= self.window_ticks:
                out.append(self._flush(link_id, buf))
                self._buffers[link_id] = []
        return out

    def _flush(self, link_id: str, buf: list[_TickRecord]) -> FeatureVector:
        some_flags = [r.has_stall for r in buf]
        full_flags = [r.has_full_stall for r in buf]
        score = compute_npsi_score(some_flags, full_flags)
        ratios = [r.backlog_ratio for r in buf]
        latencies = [r.softirq_latency_ns for r in buf]
        return FeatureVector(
            ts_ns=buf[-1].ts_ns,
            link_id=link_id,
            npsi_some=score.some,
            npsi_full=score.full,
            queue_trend=queue_depth_trend(ratios),
            softirq_latency_avg_ns=softirq_latency_avg(latencies),
            burst_arrival_rate=burst_arrival_rate(sum(some_flags), self.window_s),
            backlog_ratio_last=ratios[-1] if ratios else 0.0,
        )
