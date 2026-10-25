"""Supporting features that ride alongside the core N-PSI some/full score."""
from __future__ import annotations


def queue_depth_trend(backlog_ratios: list[float]) -> float:
    """
    Simple least-squares slope of backlog/capacity over the window (units:
    ratio change per sample). Positive => building up, negative => draining.
    """
    n = len(backlog_ratios)
    if n < 2:
        return 0.0
    xs = list(range(n))
    mean_x = sum(xs) / n
    mean_y = sum(backlog_ratios) / n
    num = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, backlog_ratios))
    den = sum((x - mean_x) ** 2 for x in xs)
    return num / den if den else 0.0


def softirq_latency_avg(latencies_ns: list[float]) -> float:
    if not latencies_ns:
        return 0.0
    return sum(latencies_ns) / len(latencies_ns)


def burst_arrival_rate(stall_tick_count: int, window_s: float) -> float:
    """Stalled-ticks-per-second within the window -- a proxy for how sharply
    arrivals are spiking versus a link that is merely mildly, steadily busy."""
    if window_s <= 0:
        return 0.0
    return stall_tick_count / window_s
