"""
Turns per-link feature-vector sequences + labels into numpy arrays ready for
training, with chronological (never shuffled) splits -- shuffling a
congestion time series before splitting would leak future-window
information into training and make the reported lead time meaningless.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass
class Split:
    X_train: np.ndarray
    y_train: np.ndarray
    X_val: np.ndarray
    y_val: np.ndarray
    X_test: np.ndarray
    y_test: np.ndarray
    ts_test: np.ndarray  # timestamps for test rows
    actual_congestion_test: np.ndarray  # TRUE (unshifted) congestion flag, for lead-time evaluation


def build_arrays(feature_vectors: list, labels: list[int],
                  congestion_threshold: float = 0.8) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    X = np.array([fv.to_array() for fv in feature_vectors], dtype=np.float64)
    y = np.array(labels, dtype=np.int64)
    ts = np.array([fv.ts_ns for fv in feature_vectors], dtype=np.int64)
    actual_congestion = np.array(
        [1 if fv.backlog_ratio_last >= congestion_threshold else 0 for fv in feature_vectors], dtype=np.int64
    )
    return X, y, ts, actual_congestion


def chronological_split(X: np.ndarray, y: np.ndarray, ts: np.ndarray, actual_congestion: np.ndarray,
                         train_frac: float = 0.6, val_frac: float = 0.2) -> Split:
    n = len(X)
    n_train = int(n * train_frac)
    n_val = int(n * val_frac)
    return Split(
        X_train=X[:n_train], y_train=y[:n_train],
        X_val=X[n_train:n_train + n_val], y_val=y[n_train:n_train + n_val],
        X_test=X[n_train + n_val:], y_test=y[n_train + n_val:],
        ts_test=ts[n_train + n_val:],
        actual_congestion_test=actual_congestion[n_train + n_val:],
    )


def make_sequences(X: np.ndarray, y: np.ndarray, seq_len: int = 8) -> tuple[np.ndarray, np.ndarray]:
    """Sliding windows of `seq_len` consecutive feature vectors for the LSTM
    variant -- label is that of the LAST step in each window."""
    if len(X) < seq_len:
        return np.empty((0, seq_len, X.shape[1])), np.empty((0,))
    Xs = np.stack([X[i:i + seq_len] for i in range(len(X) - seq_len + 1)])
    ys = y[seq_len - 1:]
    return Xs, ys
