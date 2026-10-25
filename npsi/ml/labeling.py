"""
Forward-looking threshold labeling: window i is a positive ("imminent
congestion") example if the link's true backlog occupancy exceeds
`threshold` at any point within the next `horizon_s` seconds. Ground truth
comes from `backlog_ratio_last` on each feature vector, which every feature
pipeline (N-PSI and flow-baseline) carries specifically so labeling logic
does not have to know which pipeline produced it.
"""
from __future__ import annotations

from typing import Protocol


class HasBacklogRatio(Protocol):
    backlog_ratio_last: float


def label_sequence(feature_vectors: list[HasBacklogRatio], window_s: float, horizon_s: float = 0.2,
                    threshold: float = 0.8) -> list[int]:
    horizon_windows = max(1, round(horizon_s / window_s))
    n = len(feature_vectors)
    labels = [0] * n
    for i in range(n):
        end = min(n, i + 1 + horizon_windows)
        future = feature_vectors[i + 1:end]
        if any(fv.backlog_ratio_last >= threshold for fv in future):
            labels[i] = 1
    return labels
