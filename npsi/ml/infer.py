"""Streaming inference wrapper: loads a trained model and exposes a single
`predict_proba(feature_vectors: list[np.ndarray]) -> np.ndarray` entry point,
used identically by the simulation controller and the real prediction_service."""
from __future__ import annotations

from pathlib import Path

import numpy as np

from npsi.ml.xgboost_model import NPsiXGBoostModel


class NPsiPredictor:
    def __init__(self, model_path: str | Path):
        self.model = NPsiXGBoostModel()
        self.model.load(model_path)

    def predict_proba(self, X: list[list[float]] | np.ndarray) -> np.ndarray:
        return self.model.predict_proba(np.asarray(X, dtype=np.float64))

    def predict_one(self, feature_vector) -> float:
        return float(self.predict_proba([feature_vector.to_array()])[0])
