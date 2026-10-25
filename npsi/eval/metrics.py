"""
The four headline metrics from the evaluation plan: prediction lead time,
false-positive rate, packet loss reduction, and throughput/latency deltas.
Used both as a training-time sanity check (ml/train.py) and as the final
cross-variant comparison (sim/sim_e2e.py, eval/report.py).
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np


def find_positive_blocks(y_true: np.ndarray) -> list[tuple[int, int]]:
    """Contiguous runs of ground-truth positives, as [start, end) index pairs."""
    blocks = []
    n = len(y_true)
    i = 0
    while i < n:
        if y_true[i] == 1:
            j = i
            while j < n and y_true[j] == 1:
                j += 1
            blocks.append((i, j))
            i = j
        else:
            i += 1
    return blocks


@dataclass
class BlockDetection:
    start: int
    end: int
    detected: bool
    lead_time_s: float | None  # None if never detected; 0.0 if only detected after onset


def prediction_lead_times(ts_ns: np.ndarray, y_true: np.ndarray, y_score: np.ndarray,
                           threshold: float) -> list[BlockDetection]:
    blocks = find_positive_blocks(y_true)
    results: list[BlockDetection] = []
    prev_end = 0
    for start, end in blocks:
        k = start - 1
        while k >= prev_end and y_score[k] >= threshold:
            k -= 1
        first_cross = k + 1
        if first_cross <= start - 1:
            lead_time_s = (ts_ns[start] - ts_ns[first_cross]) / 1e9
            results.append(BlockDetection(start, end, True, lead_time_s))
        else:
            detected_in_block = any(y_score[i] >= threshold for i in range(start, end))
            results.append(BlockDetection(start, end, detected_in_block, 0.0 if detected_in_block else None))
        prev_end = end
    return results


def summarize_lead_times(detections: list[BlockDetection]) -> dict:
    """NOTE: `event_detection_recall` here is "fraction of distinct congestion
    EVENTS (contiguous blocks) that were caught at all" -- a different
    quantity from window-level classification recall used for F1. Keep the
    key names distinct; merging both into one flat metrics dict under the
    same name ("recall") previously caused one to silently clobber the other."""
    if not detections:
        return {"n_events": 0, "event_detection_recall": 0.0, "mean_lead_time_s": None, "median_lead_time_s": None}
    detected = [d for d in detections if d.detected]
    lead_times = [d.lead_time_s for d in detected if d.lead_time_s is not None and d.lead_time_s > 0]
    return {
        "n_events": len(detections),
        "event_detection_recall": len(detected) / len(detections),
        "mean_lead_time_s": float(np.mean(lead_times)) if lead_times else 0.0,
        "median_lead_time_s": float(np.median(lead_times)) if lead_times else 0.0,
    }


def false_positive_rate(y_true: np.ndarray, y_pred: np.ndarray) -> float:
    negatives = y_true == 0
    n_neg = int(negatives.sum())
    if n_neg == 0:
        return 0.0
    fp = int(((y_pred == 1) & negatives).sum())
    return fp / n_neg


def packet_loss_reduction(baseline_dropped: float, treatment_dropped: float) -> float:
    if baseline_dropped <= 0:
        return 0.0
    return (baseline_dropped - treatment_dropped) / baseline_dropped


@dataclass
class RunSummary:
    """Aggregate stats over a full simulation run, per variant, used for the
    packet-loss / throughput / latency comparison table."""

    variant: str
    total_arrived_pkts: float
    total_dropped_pkts: float
    mean_backlog_ratio: float
    effective_throughput_pps: float
    mean_queueing_delay_s: float

    @property
    def loss_rate(self) -> float:
        return self.total_dropped_pkts / self.total_arrived_pkts if self.total_arrived_pkts else 0.0


def summarize_run(variant: str, tick_log: list[dict], duration_s: float) -> RunSummary:
    """`tick_log` entries: {arrival_pps, capacity_pps, backlog, max_backlog, dropped_pkts} per tick,
    for the link(s) of interest."""
    arrived = sum(t["arrival_pps"] for t in tick_log) * (duration_s / len(tick_log)) if tick_log else 0.0
    dropped = sum(t["dropped_pkts"] for t in tick_log)
    ratios = [t["backlog"] / t["max_backlog"] for t in tick_log if t["max_backlog"] > 0]
    mean_ratio = float(np.mean(ratios)) if ratios else 0.0
    delays = [t["backlog"] / t["capacity_pps"] for t in tick_log if t["capacity_pps"] > 0]
    mean_delay = float(np.mean(delays)) if delays else 0.0
    effective_throughput = (arrived - dropped) / duration_s if duration_s else 0.0
    return RunSummary(
        variant=variant,
        total_arrived_pkts=arrived,
        total_dropped_pkts=dropped,
        mean_backlog_ratio=mean_ratio,
        effective_throughput_pps=effective_throughput,
        mean_queueing_delay_s=mean_delay,
    )
