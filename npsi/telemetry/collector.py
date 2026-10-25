"""
Backend-agnostic collector facade: everything downstream (feature pipelines,
prediction service) is written against `TelemetryCollector` and never
imports `SimulatorCollector` or `EbpfCollector` directly. This is what makes
"simulation mode" and "real deployment mode" a one-line switch.
"""
from __future__ import annotations

from collections.abc import Iterator
from typing import Protocol

from npsi.telemetry.events import TelemetryEvent


class TelemetryCollector(Protocol):
    @property
    def link_ids(self) -> list[str]: ...

    def run(self, duration_s: float | None) -> Iterator[list[TelemetryEvent]]: ...


def make_collector(backend: str, **kwargs) -> TelemetryCollector:
    """
    backend="simulator": kwargs may include `model` (a pre-built NetworkModel)
        or `seed`/`duration_s` to build the reference incast scenario, plus `dt_s`.
    backend="ebpf": kwargs must include `interfaces: list[str]`, may include
        `queue_sample_interval_s`. Linux + bcc only.
    """
    if backend == "simulator":
        from npsi.sim.network_model import make_incast_scenario
        from npsi.telemetry.simulator import SimulatorCollector

        model = kwargs.get("model")
        if model is None:
            model = make_incast_scenario(seed=kwargs.get("seed", 7), duration_s=kwargs.get("duration_s", 60.0))
        return SimulatorCollector(model, dt_s=kwargs.get("dt_s", 0.005))

    if backend == "ebpf":
        from npsi.telemetry.ebpf_probes import EbpfCollector

        if "interfaces" not in kwargs:
            raise ValueError("backend='ebpf' requires interfaces=[...]")
        return EbpfCollector(
            interfaces=kwargs["interfaces"],
            queue_sample_interval_s=kwargs.get("queue_sample_interval_s", 0.02),
        )

    raise ValueError(f"unknown telemetry backend: {backend!r} (expected 'simulator' or 'ebpf')")
