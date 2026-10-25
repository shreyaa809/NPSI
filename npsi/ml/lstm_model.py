"""
Small LSTM variant to capture temporal burst dependencies (a burst building
over several consecutive windows looks different from an isolated spike,
even at the same instantaneous N-PSI score). Optional: torch is a heavy
dependency, so callers should catch `TorchNotAvailable` and fall back to
XGBoost-only, which is what `npsi/ml/train.py` does.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

try:
    import torch
    from torch import nn

    TORCH_AVAILABLE = True
except ImportError:  # pragma: no cover
    TORCH_AVAILABLE = False


class TorchNotAvailable(RuntimeError):
    pass


if TORCH_AVAILABLE:

    class _LSTMNet(nn.Module):
        def __init__(self, n_features: int, hidden: int = 16):
            super().__init__()
            self.lstm = nn.LSTM(input_size=n_features, hidden_size=hidden, num_layers=1, batch_first=True)
            self.head = nn.Sequential(nn.Linear(hidden, 8), nn.ReLU(), nn.Linear(8, 1))

        def forward(self, x):
            out, _ = self.lstm(x)
            last = out[:, -1, :]
            return self.head(last).squeeze(-1)


class NPsiLSTMModel:
    def __init__(self, n_features: int, hidden: int = 16, lr: float = 1e-3):
        if not TORCH_AVAILABLE:
            raise TorchNotAvailable("torch is not installed; run `pip install torch` to enable the LSTM variant")
        self.net = _LSTMNet(n_features, hidden)
        self.optimizer = torch.optim.Adam(self.net.parameters(), lr=lr)

    def fit(self, X_seq: np.ndarray, y: np.ndarray, epochs: int = 15, batch_size: int = 64,
            pos_weight: float | None = None) -> list[float]:
        X_t = torch.tensor(X_seq, dtype=torch.float32)
        y_t = torch.tensor(y, dtype=torch.float32)
        if pos_weight is None:
            pos = max(1.0, float(y.sum()))
            neg = max(1.0, len(y) - pos)
            pos_weight = neg / pos
        criterion = nn.BCEWithLogitsLoss(pos_weight=torch.tensor(pos_weight))
        n = len(X_t)
        losses = []
        for _ in range(epochs):
            perm = torch.randperm(n)
            epoch_loss = 0.0
            for i in range(0, n, batch_size):
                idx = perm[i:i + batch_size]
                xb, yb = X_t[idx], y_t[idx]
                self.optimizer.zero_grad()
                logits = self.net(xb)
                loss = criterion(logits, yb)
                loss.backward()
                self.optimizer.step()
                epoch_loss += loss.item() * len(idx)
            losses.append(epoch_loss / n)
        return losses

    def predict_proba(self, X_seq: np.ndarray) -> np.ndarray:
        self.net.eval()
        with torch.no_grad():
            logits = self.net(torch.tensor(X_seq, dtype=torch.float32))
            return torch.sigmoid(logits).numpy()

    def save(self, path: str | Path) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        torch.save(self.net.state_dict(), str(path))

    def load(self, path: str | Path) -> None:
        self.net.load_state_dict(torch.load(str(path)))
