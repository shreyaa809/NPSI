"""
Per-variant orchestration for the real Mininet+OVS+Ryu testbed (Linux only).
Mirrors `npsi.sim.sim_e2e`'s three variants, but against real switches:

  - reactive : Mininet + Ryu learning switch only. No prediction_service is
               started, so the NPsiRyuApp's DecisionEngine never receives a
               score and never reroutes/throttles -- traffic relies purely
               on whatever the transport itself does under loss, matching
               the report's baseline (a), "reactive TCP with no prediction."
  - flow_stat: same topology, but prediction_service.py runs with
               --pipeline flow_stat against the coarse baseline model,
               feeding the same NPsiRyuApp/DecisionEngine over REST.
  - npsi     : same topology, prediction_service.py runs with the
               fine-grained N-PSI pipeline and model.

Requires a Ryu instance already running the app for the target variant:
    ryu-manager npsi/control/ryu_app.py --topology-file topology.json
"""
from __future__ import annotations

import subprocess
import sys
import time
from dataclasses import dataclass, field

from npsi.testbed import traffic_gen

VARIANTS = ["reactive", "flow_stat", "npsi"]


@dataclass
class VariantHandles:
    """Live process handles for a running variant, so the caller can stop
    everything cleanly once the experiment window elapses."""

    prediction_service: subprocess.Popen | None = None
    victim_iface: str = ""
    port_stats_log: list[dict] = field(default_factory=list)


def start_variant(variant: str, net, victim_iface: str, ryu_url: str = "http://127.0.0.1:8080") -> VariantHandles:
    if variant not in VARIANTS:
        raise ValueError(f"unknown variant {variant!r}, expected one of {VARIANTS}")

    handles = VariantHandles(victim_iface=victim_iface)
    if variant == "reactive":
        return handles  # no prediction service -- pure learning-switch forwarding

    pipeline = "flow_stat" if variant == "flow_stat" else "npsi"
    cmd = [
        sys.executable, "-m", "npsi.control.prediction_service",
        "--backend", "ebpf",
        "--interfaces", victim_iface,
        "--pipeline", pipeline,
        "--ryu-url", ryu_url,
    ]
    handles.prediction_service = subprocess.Popen(cmd)
    time.sleep(1.0)  # give the eBPF probes a moment to attach before traffic starts
    return handles


def stop_variant(handles: VariantHandles) -> None:
    if handles.prediction_service is not None:
        handles.prediction_service.terminate()
        try:
            handles.prediction_service.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            handles.prediction_service.kill()


def read_interface_counters(iface: str) -> dict:
    """rx/tx bytes and drops for `iface`, via sysfs -- portable across
    whatever qdisc is configured, unlike parsing `tc -s qdisc` output."""
    base = f"/sys/class/net/{iface}/statistics"
    counters = {}
    for name in ("rx_bytes", "tx_bytes", "rx_dropped", "tx_dropped", "rx_packets", "tx_packets"):
        try:
            with open(f"{base}/{name}") as f:
                counters[name] = int(f.read().strip())
        except OSError:
            counters[name] = 0
    return counters


def sample_port_stats(handles: VariantHandles) -> None:
    handles.port_stats_log.append({"t": time.monotonic(), **read_interface_counters(handles.victim_iface)})


def summarize_counters(handles: VariantHandles) -> dict:
    """First/last counter snapshot -> deltas, in the same spirit as
    `npsi.eval.metrics.summarize_run` for the simulated runs (loss, rough
    throughput), so the two result sets can eventually sit side by side."""
    if len(handles.port_stats_log) < 2:
        return {}
    first, last = handles.port_stats_log[0], handles.port_stats_log[-1]
    duration_s = last["t"] - first["t"]
    tx_bytes_delta = last["tx_bytes"] - first["tx_bytes"]
    tx_dropped_delta = last["tx_dropped"] - first["tx_dropped"]
    return {
        "duration_s": duration_s,
        "tx_bytes": tx_bytes_delta,
        "tx_dropped_pkts": tx_dropped_delta,
        "tx_throughput_bps": tx_bytes_delta * 8 / duration_s if duration_s > 0 else 0.0,
    }
