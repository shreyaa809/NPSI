"""
CLI: generate simulated telemetry, build N-PSI (and flow-baseline) feature
datasets, train the XGBoost classifier (and the LSTM variant if torch is
installed), evaluate on a held-out chronological test split, and save
artifacts to `models/`.

Usage:
    python -m npsi.ml.train --duration 120 --seed 7 --out-dir models
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

from npsi.eval.metrics import false_positive_rate, prediction_lead_times, summarize_lead_times
from npsi.features.flow_baseline import FlowStatFeaturePipeline
from npsi.features.pipeline import NPsiFeaturePipeline
from npsi.ml.dataset import build_arrays, chronological_split, make_sequences
from npsi.ml.labeling import label_sequence
from npsi.ml.lstm_model import TORCH_AVAILABLE, NPsiLSTMModel
from npsi.ml.xgboost_model import NPsiXGBoostModel
from npsi.config import (
    CONGESTION_THRESHOLD as LABEL_THRESHOLD,
)
from npsi.config import DECISION_THRESHOLD, DT_S, FLOW_WINDOW_S, HORIZON_S, NPSI_WINDOW_S
from npsi.sim.network_model import make_incast_scenario
from npsi.telemetry.simulator import SimulatorCollector


def generate_features(duration_s: float, seed: int):
    model = make_incast_scenario(seed=seed, duration_s=duration_s)
    collector = SimulatorCollector(model, dt_s=DT_S)
    npsi_pipeline = NPsiFeaturePipeline(dt_s=DT_S, window_s=NPSI_WINDOW_S)
    flow_pipeline = FlowStatFeaturePipeline(dt_s=DT_S, window_s=FLOW_WINDOW_S)

    npsi_by_link: dict[str, list] = {}
    flow_by_link: dict[str, list] = {}
    for batch in collector.run(duration_s):
        for fv in npsi_pipeline.process_batch(batch):
            npsi_by_link.setdefault(fv.link_id, []).append(fv)
        for fv in flow_pipeline.process_batch(batch):
            flow_by_link.setdefault(fv.link_id, []).append(fv)
    return npsi_by_link, flow_by_link


def train_and_eval_xgboost(feature_vectors, window_s: float, tag: str) -> tuple[NPsiXGBoostModel, dict]:
    labels = label_sequence(feature_vectors, window_s=window_s, horizon_s=HORIZON_S, threshold=LABEL_THRESHOLD)
    X, y, ts, actual_congestion = build_arrays(feature_vectors, labels, congestion_threshold=LABEL_THRESHOLD)
    split = chronological_split(X, y, ts, actual_congestion)

    model = NPsiXGBoostModel()
    model.fit(split.X_train, split.y_train, split.X_val, split.y_val)

    proba = model.predict_proba(split.X_test)
    y_pred = (proba >= DECISION_THRESHOLD).astype(int)
    fpr = false_positive_rate(split.y_test, y_pred)
    # lead time is measured against the TRUE congestion event (backlog actually
    # crossing threshold), not the horizon-shifted training label -- otherwise
    # the metric would just re-measure the fixed 200ms horizon we trained for.
    detections = prediction_lead_times(split.ts_test, split.actual_congestion_test, proba, DECISION_THRESHOLD)
    lead_summary = summarize_lead_times(detections)

    tp = int(((y_pred == 1) & (split.y_test == 1)).sum())
    fp = int(((y_pred == 1) & (split.y_test == 0)).sum())
    fn = int(((y_pred == 0) & (split.y_test == 1)).sum())
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0

    metrics = {
        "tag": tag,
        "n_test": len(split.y_test),
        "positive_rate_test": float(split.y_test.mean()) if len(split.y_test) else 0.0,
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "false_positive_rate": fpr,
        **lead_summary,
    }
    return model, metrics


def train_lstm(feature_vectors, window_s: float, seq_len: int = 8) -> dict | None:
    if not TORCH_AVAILABLE:
        print("[train] torch not installed -- skipping LSTM variant (this is fine; XGBoost is primary).")
        return None
    labels = label_sequence(feature_vectors, window_s=window_s, horizon_s=HORIZON_S, threshold=LABEL_THRESHOLD)
    X, y, ts, actual_congestion = build_arrays(feature_vectors, labels, congestion_threshold=LABEL_THRESHOLD)
    split = chronological_split(X, y, ts, actual_congestion)
    Xtr_seq, ytr_seq = make_sequences(split.X_train, split.y_train, seq_len)
    Xte_seq, yte_seq = make_sequences(split.X_test, split.y_test, seq_len)
    if len(Xtr_seq) < 20 or len(Xte_seq) < 5:
        print("[train] not enough data for LSTM sequences -- skipping.")
        return None

    lstm = NPsiLSTMModel(n_features=X.shape[1])
    lstm.fit(Xtr_seq, ytr_seq, epochs=15)
    proba = lstm.predict_proba(Xte_seq)
    y_pred = (proba >= DECISION_THRESHOLD).astype(int)
    fpr = false_positive_rate(yte_seq, y_pred)
    return {"tag": "npsi_lstm", "n_test": len(yte_seq), "false_positive_rate": fpr}, lstm


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--duration", type=float, default=120.0)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out-dir", type=str, default="models")
    args = ap.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"[train] simulating {args.duration}s of incast traffic (seed={args.seed}) ...")
    npsi_by_link, flow_by_link = generate_features(args.duration, args.seed)

    all_metrics = {}
    victim_link = "leaf1-spine1"  # the link the incast scenario targets

    print("[train] training N-PSI XGBoost classifier ...")
    npsi_model, npsi_metrics = train_and_eval_xgboost(npsi_by_link[victim_link], NPSI_WINDOW_S, "npsi_xgboost")
    npsi_model.save(out_dir / "npsi_xgboost.json")
    all_metrics["npsi_xgboost"] = npsi_metrics

    print("[train] training flow-baseline XGBoost classifier ...")
    flow_model, flow_metrics = train_and_eval_xgboost(flow_by_link[victim_link], FLOW_WINDOW_S, "flow_baseline_xgboost")
    flow_model.save(out_dir / "flow_baseline_xgboost.json")
    all_metrics["flow_baseline_xgboost"] = flow_metrics

    print("[train] training N-PSI LSTM variant (optional) ...")
    lstm_result = train_lstm(npsi_by_link[victim_link], NPSI_WINDOW_S)
    if lstm_result is not None:
        lstm_metrics, lstm_model = lstm_result
        lstm_model.save(out_dir / "npsi_lstm.pt")
        all_metrics["npsi_lstm"] = lstm_metrics

    metrics_path = out_dir / "training_metrics.json"
    metrics_path.write_text(json.dumps(all_metrics, indent=2))
    print(f"[train] done. Metrics written to {metrics_path}")
    for tag, m in all_metrics.items():
        print(f"  {tag}: {m}")


if __name__ == "__main__":
    main()
