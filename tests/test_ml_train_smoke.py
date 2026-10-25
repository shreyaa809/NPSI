"""Fast smoke tests for the training pipeline -- short duration, just checking
the whole path (simulate -> features -> label -> train -> evaluate -> save/load)
runs without error and produces sane shapes/ranges. Statistical quality on
this short a run is not asserted here; see docs/results.md for the real
(longer-duration) numbers used in the report."""
import numpy as np

from npsi.config import VICTIM_LINK
from npsi.ml.train import generate_features, train_and_eval_xgboost


def test_generate_features_smoke():
    npsi_by_link, flow_by_link = generate_features(duration_s=20.0, seed=1)
    assert VICTIM_LINK in npsi_by_link
    assert VICTIM_LINK in flow_by_link
    assert len(npsi_by_link[VICTIM_LINK]) > 0
    assert len(flow_by_link[VICTIM_LINK]) > 0


def test_train_and_eval_xgboost_smoke(tmp_path):
    npsi_by_link, _ = generate_features(duration_s=30.0, seed=2)
    model, metrics = train_and_eval_xgboost(npsi_by_link[VICTIM_LINK], window_s=0.02, tag="smoke")

    assert metrics["tag"] == "smoke"
    assert 0.0 <= metrics["false_positive_rate"] <= 1.0
    assert 0.0 <= metrics["precision"] <= 1.0
    assert 0.0 <= metrics["recall"] <= 1.0

    save_path = tmp_path / "model.json"
    model.save(save_path)
    assert save_path.exists()

    from npsi.ml.xgboost_model import NPsiXGBoostModel
    reloaded = NPsiXGBoostModel()
    reloaded.load(save_path)
    X_dummy = np.array([[0.5, 0.2, 0.0, 5000.0, 3.0]])
    proba = reloaded.predict_proba(X_dummy)
    assert proba.shape == (1,)
    assert 0.0 <= proba[0] <= 1.0
