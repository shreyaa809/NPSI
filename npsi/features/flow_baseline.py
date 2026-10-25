"""
Flow-statistic baseline feature extractor -- deliberately modeled after
NetFlow/sFlow: it only ever sees backlog/utilization (never stall time or
softirq latency), it subsamples ticks (1-in-`sample_stride`, like sFlow's
1-in-N packet sampling), it aggregates over a coarser window than N-PSI, and
it reports with one full window of extra lag (periodic batch export, not a
continuous stream). This is the "fair baseline" from the evaluation plan:
same downstream classifier and controller logic, weaker telemetry.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from npsi.features.supporting import queue_depth_trend
from npsi.telemetry.events import QueueSample, TelemetryEvent

FLOW_FEATURE_NAMES = ["avg_utilization", "utilization_trend"]


@dataclass(frozen=True, slots=True)
class FlowBaselineFeatureVector:
    ts_ns: int
    link_id: str
    avg_utilization: float
    utilization_trend: float
    backlog_ratio_last: float  # ground truth, for labeling only

    def to_array(self) -> list[float]:
        return [self.avg_utilization, self.utilization_trend]


class FlowStatFeaturePipeline:
    def __init__(self, dt_s: float, window_s: float = 0.25, sample_stride: int = 10, report_delay_windows: int = 1):
        self.dt_s = dt_s
        self.window_s = window_s
        self.window_ticks = max(1, round(window_s / dt_s))
        self.sample_stride = sample_stride
        self.report_delay_windows = report_delay_windows
        self._tick_index: dict[str, int] = defaultdict(int)
        self._sampled_ratios: dict[str, list[float]] = defaultdict(list)
        self._pending: dict[str, list[FlowBaselineFeatureVector]] = defaultdict(list)

    def process_batch(self, events: list[TelemetryEvent]) -> list[FlowBaselineFeatureVector]:
        by_link: dict[str, QueueSample] = {}
        for ev in events:
            if isinstance(ev, QueueSample):
                by_link[ev.link_id] = ev  # last sample for this link this tick

        out: list[FlowBaselineFeatureVector] = []
        for link_id, ev in by_link.items():
            idx = self._tick_index[link_id]
            self._tick_index[link_id] = idx + 1
            if idx % self.sample_stride == 0:
                ratio = ev.backlog / ev.capacity if ev.capacity else 0.0
                self._sampled_ratios[link_id].append(ratio)

            if (idx + 1) % self.window_ticks == 0:
                ratios = self._sampled_ratios[link_id]
                self._sampled_ratios[link_id] = []
                if ratios:
                    fv = FlowBaselineFeatureVector(
                        ts_ns=ev.ts_ns,
                        link_id=link_id,
                        avg_utilization=sum(ratios) / len(ratios),
                        utilization_trend=queue_depth_trend(ratios),
                        backlog_ratio_last=ratios[-1],
                    )
                    self._pending[link_id].append(fv)
                # only release once we have `report_delay_windows` extra windows queued,
                # emulating periodic export lag
                pending = self._pending[link_id]
                if len(pending) > self.report_delay_windows:
                    out.append(pending.pop(0))
        return out
