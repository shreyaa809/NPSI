"""Primary predictor: a lightweight gradient-boosted classifier on N-PSI features."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import xgboost as xgb


class NPsiXGBoostModel:
    def __init__(self, **params):
        default = dict(
            n_estimators=150,
            max_depth=4,
            learning_rate=0.1,
            subsample=0.9,
            colsample_bytree=0.9,
            eval_metric="aucpr",
            n_jobs=-1,
        )
        default.update(params)
        self.model = xgb.XGBClassifier(**default)

    def fit(self, X_train: np.ndarray, y_train: np.ndarray, X_val: np.ndarray | None = None,
            y_val: np.ndarray | None = None) -> None:
        # class imbalance is expected (congestion windows are rarer than calm ones)
        pos = max(1, int(y_train.sum()))
        neg = max(1, len(y_train) - pos)
        self.model.set_params(scale_pos_weight=neg / pos)
        eval_set = [(X_val, y_val)] if X_val is not None and len(X_val) else None
        self.model.fit(X_train, y_train, eval_set=eval_set, verbose=False)

    def predict_proba(self, X: np.ndarray) -> np.ndarray:
        return self.model.predict_proba(X)[:, 1]

    def save(self, path: str | Path) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.model.save_model(str(path))

    def load(self, path: str | Path) -> None:
        self.model.load_model(str(path))
