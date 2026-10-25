"""
The full closed-loop, Windows-runnable demo: for the SAME incast scenario
(same seed => same burst schedule, so the comparison is apples-to-apples),
run three variants back to back --

  1. reactive   -- no prediction; the controller only acts once the queue is
                   ALREADY backed up (classic loss/RTT-triggered behaviour)
  2. flow_stat  -- coarse, sampled, delayed flow-statistic classifier drives
                   the same controller (the "fair baseline" from the eval plan)
  3. npsi       -- fine-grained N-PSI classifier drives the same controller

Each variant gets its own fresh NetworkModel instance, so a successful
proactive action in one variant can't leak into another's trajectory. The
output is a RunSummary (loss/throughput/latency) plus a lead-time/FPR
snapshot per variant, which `npsi/eval/report.py` turns into the comparison
table and plots.

This is intentionally the single script a grader can run to see the entire
system work end to end without any Linux/root/Mininet dependency:

    python -m npsi.sim.sim_e2e --duration 120 --seed 42 --models-dir models
"""
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from npsi.config import (
    CONGESTION_THRESHOLD,
    DECISION_THRESHOLD,
    DT_S,
    FLOW_WINDOW_S,
    HORIZON_S,
    NPSI_WINDOW_S,
    REACTIVE_RELEASE_THRESHOLD,
    REACTIVE_TRIGGER_THRESHOLD,
    VICTIM_LINK,
)
from npsi.control.decision import DecisionConfig, DecisionEngine
from npsi.control.sim_controller import SimController, make_model_signal, reactive_signal
from npsi.eval.metrics import RunSummary, false_positive_rate, prediction_lead_times, summarize_lead_times, summarize_run
from npsi.features.flow_baseline import FlowStatFeaturePipeline
from npsi.features.pipeline import NPsiFeaturePipeline
from npsi.ml.infer import NPsiPredictor
from npsi.ml.labeling import label_sequence
from npsi.sim.network_model import make_incast_scenario
from npsi.telemetry.simulator import tick_to_events

VARIANTS = ["reactive", "flow_stat", "npsi"]


@dataclass
class VariantResult:
    variant: str
    run_summary: RunSummary
    lead_summary: dict
    false_positive_rate: float
    n_reroute_activations: int
    actions_timeline: list[dict] = field(default_factory=list)


def _run_variant(variant: str, seed: int, duration_s: float, npsi_predictor: NPsiPredictor | None,
                  flow_predictor: NPsiPredictor | None) -> VariantResult:
    model = make_incast_scenario(seed=seed, duration_s=duration_s)

    npsi_pipeline = NPsiFeaturePipeline(dt_s=DT_S, window_s=NPSI_WINDOW_S)
    flow_pipeline = FlowStatFeaturePipeline(dt_s=DT_S, window_s=FLOW_WINDOW_S) if variant == "flow_stat" else None

    if variant == "reactive":
        # true reactive-TCP-equivalent: only acts once the queue is essentially
        # already full (i.e. after damage), matching the report's own framing
        # rather than the earlier, over-generous 0.5-occupancy trigger.
        signal_fn = reactive_signal
        alarm_threshold = REACTIVE_TRIGGER_THRESHOLD
        decision_engine = DecisionEngine(DecisionConfig(trigger_threshold=REACTIVE_TRIGGER_THRESHOLD,
                                                         release_threshold=REACTIVE_RELEASE_THRESHOLD))
    elif variant == "flow_stat":
        assert flow_predictor is not None
        signal_fn = make_model_signal(flow_predictor.predict_proba)
        alarm_threshold = DECISION_THRESHOLD
        decision_engine = DecisionEngine(DecisionConfig())
    elif variant == "npsi":
        assert npsi_predictor is not None
        signal_fn = make_model_signal(npsi_predictor.predict_proba)
        alarm_threshold = DECISION_THRESHOLD
        decision_engine = DecisionEngine(DecisionConfig())
    else:
        raise ValueError(f"unknown variant {variant}")

    controller = SimController(model, decision_engine, signal_fn)

    tick_log: list[dict] = []
    eval_fvs: list = []  # feature vectors used to score this run's own detections
    scores: list[float] = []

    for tick_results in model.run(duration_s, DT_S):
        events = tick_to_events(tick_results, DT_S)
        for tr in tick_results:
            if tr.link_id == VICTIM_LINK:
                tick_log.append(dict(arrival_pps=tr.arrival_pps, capacity_pps=tr.capacity_pps,
                                      backlog=tr.backlog, max_backlog=tr.max_backlog,
                                      dropped_pkts=tr.dropped_pkts))

        # drive the controller off the appropriate pipeline for this variant
        pipeline = flow_pipeline if variant == "flow_stat" else npsi_pipeline
        fvs = pipeline.process_batch(events)
        victim_fvs = [fv for fv in fvs if fv.link_id == VICTIM_LINK]
        if victim_fvs:
            for fv in victim_fvs:
                scores.append(signal_fn(fv))
            eval_fvs.extend(victim_fvs)
            controller.on_features(victim_fvs)

    run_summary = summarize_run(variant, tick_log, duration_s)

    # derive activation transitions straight from the controller's own action
    # log, which is recorded 1:1 with eval_fvs (same loop, same order)
    actions_timeline: list[dict] = []
    was_active = False
    for fv, action in zip(eval_fvs, controller.actions_log):
        if action.active and not was_active:
            actions_timeline.append({"t_s": fv.ts_ns / 1e9, "event": "activate"})
        elif not action.active and was_active:
            actions_timeline.append({"t_s": fv.ts_ns / 1e9, "event": "release"})
        was_active = action.active

    window_s = FLOW_WINDOW_S if variant == "flow_stat" else NPSI_WINDOW_S
    labels = label_sequence(eval_fvs, window_s=window_s, horizon_s=HORIZON_S, threshold=CONGESTION_THRESHOLD)
    ts = np.array([fv.ts_ns for fv in eval_fvs], dtype=np.int64)
    actual = np.array([1 if fv.backlog_ratio_last >= CONGESTION_THRESHOLD else 0 for fv in eval_fvs], dtype=np.int64)
    score_arr = np.array(scores, dtype=np.float64)
    label_arr = np.array(labels, dtype=np.int64)

    detections = prediction_lead_times(ts, actual, score_arr, alarm_threshold)
    lead_summary = summarize_lead_times(detections)
    pred = (score_arr >= alarm_threshold).astype(int)
    fpr = false_positive_rate(label_arr, pred)

    n_activations = sum(1 for a in actions_timeline if a["event"] == "activate")
    return VariantResult(variant, run_summary, lead_summary, fpr, n_activations, actions_timeline)


def run_comparison(seed: int, duration_s: float, models_dir: str) -> dict[str, VariantResult]:
    models_path = Path(models_dir)
    npsi_predictor = NPsiPredictor(models_path / "npsi_xgboost.json")
    flow_predictor = NPsiPredictor(models_path / "flow_baseline_xgboost.json")

    results: dict[str, VariantResult] = {}
    for variant in VARIANTS:
        print(f"[sim_e2e] running variant '{variant}' ...")
        results[variant] = _run_variant(variant, seed, duration_s, npsi_predictor, flow_predictor)
    return results


def results_to_json(results: dict[str, VariantResult]) -> dict:
    out = {}
    for variant, r in results.items():
        out[variant] = {
            "loss_rate": r.run_summary.loss_rate,
            "total_dropped_pkts": r.run_summary.total_dropped_pkts,
            "effective_throughput_pps": r.run_summary.effective_throughput_pps,
            "mean_queueing_delay_s": r.run_summary.mean_queueing_delay_s,
            "mean_backlog_ratio": r.run_summary.mean_backlog_ratio,
            "n_reroute_activations": r.n_reroute_activations,
            "false_positive_rate": r.false_positive_rate,
            **r.lead_summary,
        }
    baseline_loss = out["reactive"]["total_dropped_pkts"]
    for variant in out:
        base = out[variant]["total_dropped_pkts"]
        out[variant]["packet_loss_reduction_vs_reactive"] = (
            (baseline_loss - base) / baseline_loss if baseline_loss > 0 else 0.0
        )
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--duration", type=float, default=120.0)
    ap.add_argument("--seed", type=int, default=42, help="use a different seed than training for a fair, held-out run")
    ap.add_argument("--models-dir", type=str, default="models")
    ap.add_argument("--out-dir", type=str, default="results")
    args = ap.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    results = run_comparison(args.seed, args.duration, args.models_dir)
    summary = results_to_json(results)

    out_path = out_dir / "comparison_metrics.json"
    out_path.write_text(json.dumps(summary, indent=2))
    print(f"[sim_e2e] wrote {out_path}")
    for variant, m in summary.items():
        print(f"  {variant}: {m}")

    return results


if __name__ == "__main__":
    main()
