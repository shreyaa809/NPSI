from dataclasses import dataclass

from npsi.ml.labeling import label_sequence


@dataclass
class FakeFV:
    backlog_ratio_last: float


def test_no_future_congestion_all_negative():
    fvs = [FakeFV(0.1)] * 10
    labels = label_sequence(fvs, window_s=0.05, horizon_s=0.2, threshold=0.8)
    assert labels == [0] * 10


def test_future_spike_labels_preceding_windows_positive():
    # window_s=0.05, horizon_s=0.2 -> horizon_windows = 4
    ratios = [0.1] * 10
    ratios[7] = 0.9  # spike at index 7
    fvs = [FakeFV(r) for r in ratios]
    labels = label_sequence(fvs, window_s=0.05, horizon_s=0.2, threshold=0.8)
    # indices 3,4,5,6 should be positive (within 4 windows before index 7); index 7 itself looks at 8-10 only
    assert labels[3:7] == [1, 1, 1, 1]
    assert labels[7] == 0  # label looks strictly at FUTURE windows, not itself
    assert labels[0:3] == [0, 0, 0]


def test_last_window_never_positive_no_future_available():
    ratios = [0.1] * 5
    fvs = [FakeFV(r) for r in ratios]
    labels = label_sequence(fvs, window_s=0.05, horizon_s=0.2, threshold=0.8)
    assert labels[-1] == 0
