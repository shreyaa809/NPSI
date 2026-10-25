"""
Synthetic telemetry backend: converts the fluid queue model in
`npsi.sim.network_model` into the same StallEvent/QueueSample event stream
that the real eBPF backend (`ebpf_probes.py`) would produce from an actual
kernel. Downstream code (feature engineering, ML, control) is written
against this schema only, so swapping this module for `ebpf_probes.py` is
the entire difference between "simulation mode" and "real deployment mode".

Multiple probe points are synthesized from the single ground-truth backlog
number because that's genuinely what the four eBPF hooks would observe
simultaneously on a real congested link: a queueing-discipline backlog stall
(qdisc), a socket-buffer contention stall once arrivals exceed capacity
(sk_buffer / tcp_sendmsg), and a NIC ring effect once the link is close to
full. This mirrors section 4.1 of the project report.
"""
from __future__ import annotations

from collections.abc import Iterator

from npsi.sim.network_model import NetworkModel, TickResult
from npsi.telemetry.events import ProbePoint, QueueSample, StallEvent, TelemetryEvent

NS_PER_S = 1_000_000_000


class SimulatorCollector:
    """Backend-agnostic collector interface, backed by the fluid queue model."""

    def __init__(self, model: NetworkModel, dt_s: float = 0.005):
        self.model = model
        self.dt_s = dt_s

    @property
    def link_ids(self) -> list[str]:
        return list(self.model.links.keys())

    def run(self, duration_s: float) -> Iterator[list[TelemetryEvent]]:
        """Yields the batch of telemetry events produced during each tick."""
        for tick_results in self.model.run(duration_s, self.dt_s):
            yield tick_to_events(tick_results, self.dt_s)


def tick_to_events(tick_results: list[TickResult], dt_s: float) -> list[TelemetryEvent]:
    events: list[TelemetryEvent] = []
    for r in tick_results:
        ts_ns = int(r.t_s * NS_PER_S)
        events.append(
            QueueSample(
                ts_ns=ts_ns,
                link_id=r.link_id,
                backlog=r.backlog,
                capacity=r.max_backlog,
                softirq_latency_ns=r.softirq_latency_ns,
            )
        )
        if r.backlog <= 0:
            continue  # no stall this tick

        # qdisc: the packet(s) queued this tick waited roughly backlog/capacity_pps seconds
        queueing_delay_s = r.backlog / r.capacity_pps if r.capacity_pps > 0 else dt_s
        events.append(
            StallEvent(
                ts_ns=ts_ns,
                link_id=r.link_id,
                probe=ProbePoint.QDISC,
                stall_ns=int(min(queueing_delay_s, dt_s * 50) * NS_PER_S),
            )
        )
        if r.arrival_pps > r.capacity_pps:
            # sender-side: socket buffer can't drain as fast as the app is writing
            events.append(
                StallEvent(
                    ts_ns=ts_ns,
                    link_id=r.link_id,
                    probe=ProbePoint.SK_BUFFER,
                    stall_ns=int(dt_s * 0.6 * NS_PER_S),
                )
            )
        if r.full_stalled:
            # NIC ring effectively full: everyone trying to send is blocked
            events.append(
                StallEvent(
                    ts_ns=ts_ns,
                    link_id=r.link_id,
                    probe=ProbePoint.NIC_RING,
                    stall_ns=int(dt_s * 0.9 * NS_PER_S),
                )
            )
    return events
