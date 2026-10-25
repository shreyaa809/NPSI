"""
Discrete-time network data-plane model used by the Windows-runnable
simulation mode. This stands in for Mininet+OVS+real-kernel-queues: it
models per-link backlog build-up from bursty (incast) arrivals, packet loss
on buffer overflow, and lets an external controller act on it (reroute /
throttle), so the full closed loop can be exercised without root or Linux.

This is deliberately NOT a packet-level simulator -- it is a fluid
(rate-based) queue model, which is standard for congestion-control studies
where the object of interest is queue occupancy over time, not per-packet
timing. Units: rates in packets/sec, backlog/capacity in packets, time in
seconds unless suffixed `_ns`.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

FULL_STALL_FRACTION = 0.90  # backlog/max_backlog above which we call a link "fully" stalled
BASE_SOFTIRQ_NS = 2_000.0  # idle-link interrupt handling latency
MAX_SOFTIRQ_NS = 40_000.0  # interrupt handling latency once a link is saturated


@dataclass
class LinkConfig:
    link_id: str
    capacity_pps: float
    max_backlog_pkts: float
    base_load_pps: float = 0.0
    alt_link_id: str | None = None  # alternate path this link can reroute traffic onto


@dataclass
class BurstEvent:
    """A synthetic incast burst: `num_sources` senders all target `link_id`
    simultaneously for `duration_s`, each contributing `per_source_pps`."""

    start_s: float
    duration_s: float
    link_id: str
    num_sources: int
    per_source_pps: float

    def extra_pps(self, t: float) -> float:
        if self.start_s <= t < self.start_s + self.duration_s:
            return self.num_sources * self.per_source_pps
        return 0.0


@dataclass
class LinkRuntimeState:
    backlog: float = 0.0
    throttle_factor: float = 1.0  # 1.0 = no throttling, controller can lower this
    reroute_fraction: float = 0.0  # fraction of arrivals diverted to alt_link_id
    cum_dropped_pkts: float = 0.0
    cum_arrived_pkts: float = 0.0
    last_arrival_pps: float = 0.0


@dataclass
class TickResult:
    t_s: float
    link_id: str
    arrival_pps: float
    capacity_pps: float
    backlog: float
    max_backlog: float
    dropped_pkts: float
    softirq_latency_ns: float
    some_stalled: bool
    full_stalled: bool


class NetworkModel:
    """Multi-link fluid queue model with controller hooks."""

    def __init__(self, links: list[LinkConfig], bursts: list[BurstEvent], seed: int = 7):
        self.links: dict[str, LinkConfig] = {l.link_id: l for l in links}
        self.state: dict[str, LinkRuntimeState] = {l.link_id: LinkRuntimeState() for l in links}
        self.bursts = bursts
        self.t_s = 0.0
        self._rng = random.Random(seed)

    # --- controller-facing actions -------------------------------------------------
    def set_throttle(self, link_id: str, factor: float) -> None:
        """factor in [0,1]; 1.0 = full rate, 0.0 = fully blocked."""
        self.state[link_id].throttle_factor = max(0.0, min(1.0, factor))

    def set_reroute(self, link_id: str, fraction: float) -> None:
        """Divert `fraction` of this link's arrivals onto its configured alt_link_id."""
        if self.links[link_id].alt_link_id is None:
            return
        self.state[link_id].reroute_fraction = max(0.0, min(1.0, fraction))

    def reset_actions(self, link_id: str) -> None:
        self.state[link_id].throttle_factor = 1.0
        self.state[link_id].reroute_fraction = 0.0

    # --- simulation step -------------------------------------------------------------
    def _raw_arrival_pps(self, link_id: str) -> float:
        cfg = self.links[link_id]
        extra = sum(b.extra_pps(self.t_s) for b in self.bursts if b.link_id == link_id)
        return cfg.base_load_pps + extra

    def step(self, dt_s: float) -> list[TickResult]:
        results: list[TickResult] = []
        diverted_in: dict[str, float] = {lid: 0.0 for lid in self.links}

        # first pass: compute each link's own raw arrival post-throttle, and how much
        # it diverts onto its alt link.
        post_throttle: dict[str, float] = {}
        for link_id, cfg in self.links.items():
            st = self.state[link_id]
            raw = self._raw_arrival_pps(link_id) * st.throttle_factor
            post_throttle[link_id] = raw
            if st.reroute_fraction > 0 and cfg.alt_link_id:
                moved = raw * st.reroute_fraction
                post_throttle[link_id] -= moved
                diverted_in[cfg.alt_link_id] += moved

        for link_id, cfg in self.links.items():
            st = self.state[link_id]
            effective_arrival = max(0.0, post_throttle[link_id] + diverted_in[link_id])
            st.last_arrival_pps = effective_arrival

            # net backlog change this tick: arrivals add, service capacity drains
            delta = (effective_arrival - cfg.capacity_pps) * dt_s
            new_backlog = st.backlog + delta
            dropped = 0.0
            if new_backlog > cfg.max_backlog_pkts:
                dropped = new_backlog - cfg.max_backlog_pkts
                new_backlog = cfg.max_backlog_pkts
            if new_backlog < 0:
                new_backlog = 0.0
            st.backlog = new_backlog
            st.cum_dropped_pkts += dropped
            st.cum_arrived_pkts += effective_arrival * dt_s

            occ = st.backlog / cfg.max_backlog_pkts if cfg.max_backlog_pkts > 0 else 0.0
            softirq_ns = BASE_SOFTIRQ_NS + occ * (MAX_SOFTIRQ_NS - BASE_SOFTIRQ_NS)
            softirq_ns *= 1.0 + self._rng.uniform(-0.05, 0.05)  # measurement noise

            results.append(
                TickResult(
                    t_s=self.t_s,
                    link_id=link_id,
                    arrival_pps=effective_arrival,
                    capacity_pps=cfg.capacity_pps,
                    backlog=st.backlog,
                    max_backlog=cfg.max_backlog_pkts,
                    dropped_pkts=dropped,
                    softirq_latency_ns=softirq_ns,
                    some_stalled=st.backlog > 0,
                    full_stalled=occ >= FULL_STALL_FRACTION,
                )
            )

        self.t_s += dt_s
        return results

    def run(self, duration_s: float, dt_s: float):
        """Generator yielding list[TickResult] per tick for `duration_s` seconds."""
        n_ticks = int(math.ceil(duration_s / dt_s))
        for _ in range(n_ticks):
            yield self.step(dt_s)


def make_incast_scenario(seed: int = 7, duration_s: float = 60.0) -> NetworkModel:
    """
    Reference scenario: a small leaf-spine style pair of uplinks. `link_victim`
    repeatedly suffers many-to-one incast bursts; `link_alt` is the alternate
    path a controller can reroute onto. Capacities/backlogs are chosen so
    that an unmitigated burst reliably overflows the buffer (i.e. baseline
    reactive TCP *will* see loss), giving N-PSI something real to prevent.

    Bursts are scheduled across the *entire* requested duration (with a small
    margin at each end) so that a chronological train/val/test split always
    has congestion events in every split, regardless of how long a run is.
    """
    links = [
        LinkConfig(link_id="leaf1-spine1", capacity_pps=1000, max_backlog_pkts=150, base_load_pps=300,
                   alt_link_id="leaf1-spine2"),
        LinkConfig(link_id="leaf1-spine2", capacity_pps=1000, max_backlog_pkts=150, base_load_pps=200),
    ]
    rng = random.Random(seed)
    bursts = []
    t = 2.0
    end = max(4.0, duration_s - 2.0)
    while t < end:
        burst = BurstEvent(
            start_s=t,
            duration_s=rng.uniform(0.15, 0.4),
            link_id="leaf1-spine1",
            num_sources=rng.randint(15, 40),
            per_source_pps=rng.uniform(40, 90),
        )
        bursts.append(burst)
        t += rng.uniform(2.5, 5.0)
    return NetworkModel(links=links, bursts=bursts, seed=seed)
