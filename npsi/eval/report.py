"""
Turns `results/comparison_metrics.json` (from sim/sim_e2e.py) and
`models/training_metrics.json` (from ml/train.py) into a human-readable
comparison: a Markdown table plus bar-chart plots, matching the metrics
named in the evaluation plan (prediction lead time, FPR, packet loss
reduction, throughput/latency).
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

VARIANT_LABELS = {"reactive": "Reactive TCP", "flow_stat": "Flow-stat baseline", "npsi": "N-PSI"}
VARIANT_ORDER = ["reactive", "flow_stat", "npsi"]


def load_json(path: Path) -> dict:
    return json.loads(Path(path).read_text())


def make_markdown_table(training_metrics: dict, comparison_metrics: dict) -> str:
    lines = ["# N-PSI Evaluation Results\n"]

    lines.append("## Open-loop prediction quality (no controller action; ml/train.py)\n")
    lines.append("| Model | Precision | Recall | F1 | FPR | Events detected | Mean lead time (s) |")
    lines.append("|---|---|---|---|---|---|---|")
    for tag, label in [("npsi_xgboost", "N-PSI (XGBoost)"), ("flow_baseline_xgboost", "Flow-stat baseline (XGBoost)")]:
        m = training_metrics.get(tag, {})
        lead = m.get("mean_lead_time_s")
        lead_str = f"{lead * 1000:.0f} ms" if lead is not None else "n/a"
        lines.append(
            f"| {label} | {m.get('precision', 0):.3f} | {m.get('recall', 0):.3f} | {m.get('f1', 0):.3f} | "
            f"{m.get('false_positive_rate', 0):.4f} | {m.get('event_detection_recall', 0):.0%} of {m.get('n_events', 0)} | "
            f"{lead_str} |"
        )

    lines.append("\n## Closed-loop system outcome (controller acts on its own signal; sim/sim_e2e.py)\n")
    lines.append("| Variant | Packets dropped | Loss rate | Loss reduction vs reactive | Throughput (pps) | Mean queueing delay (s) | Controller activations |")
    lines.append("|---|---|---|---|---|---|---|")
    for variant in VARIANT_ORDER:
        m = comparison_metrics.get(variant, {})
        lines.append(
            f"| {VARIANT_LABELS[variant]} | {m.get('total_dropped_pkts', 0):.1f} | {m.get('loss_rate', 0):.4%} | "
            f"{m.get('packet_loss_reduction_vs_reactive', 0):+.1%} | {m.get('effective_throughput_pps', 0):.1f} | "
            f"{m.get('mean_queueing_delay_s', 0):.5f} | {m.get('n_reroute_activations', 0)} |"
        )

    lines.append(
        "\n*Interpretation note*: closed-loop lead-time/recall figures are measured on each variant's own "
        "(already-mitigated) trajectory, so a fully successful controller can show zero observed congestion "
        "events simply because it prevented them -- the correct place to read prediction quality in isolation "
        "is the open-loop table above. The closed-loop table's headline evidence is the outcome: packet loss, "
        "throughput, and queueing delay actually experienced by the network under each control strategy.\n"
    )
    return "\n".join(lines)


def make_plots(comparison_metrics: dict, out_dir: Path) -> None:
    variants = [v for v in VARIANT_ORDER if v in comparison_metrics]
    labels = [VARIANT_LABELS[v] for v in variants]
    colors = ["#94a3b8", "#f59e0b", "#10b981"]  # neutral, warning, success

    fig, axes = plt.subplots(1, 3, figsize=(13, 4))

    dropped = [comparison_metrics[v]["total_dropped_pkts"] for v in variants]
    axes[0].bar(labels, dropped, color=colors[: len(variants)])
    axes[0].set_title("Packets dropped")
    axes[0].set_ylabel("packets")

    throughput = [comparison_metrics[v]["effective_throughput_pps"] for v in variants]
    axes[1].bar(labels, throughput, color=colors[: len(variants)])
    axes[1].set_title("Effective throughput")
    axes[1].set_ylabel("packets/sec")

    delay = [comparison_metrics[v]["mean_queueing_delay_s"] * 1000 for v in variants]
    axes[2].bar(labels, delay, color=colors[: len(variants)])
    axes[2].set_title("Mean queueing delay")
    axes[2].set_ylabel("ms")

    for ax in axes:
        ax.tick_params(axis="x", rotation=15)

    fig.tight_layout()
    out_path = out_dir / "comparison_plot.png"
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    print(f"[report] wrote {out_path}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--models-dir", type=str, default="models")
    ap.add_argument("--results-dir", type=str, default="results")
    args = ap.parse_args()

    models_dir = Path(args.models_dir)
    results_dir = Path(args.results_dir)

    training_metrics = load_json(models_dir / "training_metrics.json")
    comparison_metrics = load_json(results_dir / "comparison_metrics.json")

    md = make_markdown_table(training_metrics, comparison_metrics)
    md_path = results_dir / "RESULTS.md"
    md_path.write_text(md)
    print(f"[report] wrote {md_path}")

    make_plots(comparison_metrics, results_dir)


if __name__ == "__main__":
    main()
