"""
Bursty incast traffic generator for the real Mininet testbed (Linux only).
Structurally mirrors `npsi.sim.network_model.make_incast_scenario`'s burst
schedule (many-to-one, randomly spaced short bursts) so the real-deployment
experiment exercises the same qualitative scenario as the simulation.

Requires `iperf3` installed in the Mininet host image (`apt install iperf3`).
Uses UDP so we get a genuinely bursty, uncontrolled-rate traffic pattern
instead of TCP's own congestion control smoothing it out -- we want to test
N-PSI's ability to predict congestion FROM bursts, not conflate that with
TCP's own reactive behavior (which is the `reactive` baseline's job).
"""
from __future__ import annotations

import random
import time
from dataclasses import dataclass

IPERF_PORT = 5201


@dataclass
class BurstSpec:
    start_s: float
    duration_s: float
    host_names: list[str]
    bandwidth_mbps: float


def start_receiver(receiver, port: int = IPERF_PORT) -> None:
    receiver.cmd(f"iperf3 -s -p {port} -D")


def stop_receiver(receiver, port: int = IPERF_PORT) -> None:
    receiver.cmd(f"kill $(pgrep -f 'iperf3 -s -p {port}') 2>/dev/null")


def start_base_load(hosts: list, receiver_ip: str, bandwidth_mbps: float = 2.0,
                     duration_s: float = 3600.0, port: int = IPERF_PORT) -> None:
    """Persistent low-rate background traffic, analogous to `base_load_pps`
    in the fluid simulation model."""
    for h in hosts:
        h.cmd(f"iperf3 -c {receiver_ip} -p {port} -u -b {bandwidth_mbps}M -t {int(duration_s)} > /tmp/{h.name}_base.json &")


def fire_burst(burst: BurstSpec, host_by_name: dict, receiver_ip: str, port: int = IPERF_PORT) -> None:
    dur_whole_s = max(1, int(round(burst.duration_s)))  # iperf3 -t is whole seconds; we still fire+forget short bursts
    for name in burst.host_names:
        h = host_by_name[name]
        cmd = (f"timeout {burst.duration_s + 0.5} iperf3 -c {receiver_ip} -p {port} -u "
               f"-b {burst.bandwidth_mbps}M -t {dur_whole_s} > /tmp/{h.name}_burst_{int(burst.start_s * 1000)}.json 2>&1 &")
        h.cmd(cmd)


def generate_incast_schedule(host_names: list[str], duration_s: float, seed: int = 7,
                              min_sources: int = 10, bandwidth_mbps: float = 3.0) -> list[BurstSpec]:
    rng = random.Random(seed)
    schedule: list[BurstSpec] = []
    t = 2.0
    end = max(4.0, duration_s - 2.0)
    while t < end:
        n_sources = rng.randint(min_sources, len(host_names))
        burst_hosts = rng.sample(host_names, n_sources)
        burst_dur = rng.uniform(0.2, 0.5)
        schedule.append(BurstSpec(start_s=t, duration_s=burst_dur, host_names=burst_hosts,
                                   bandwidth_mbps=bandwidth_mbps))
        t += rng.uniform(2.5, 5.0)
    return schedule


def run_schedule(schedule: list[BurstSpec], host_by_name: dict, receiver_ip: str) -> None:
    """Blocking driver: sleeps between bursts and fires each one at its
    scheduled offset. Run this from the Mininet control process (not inside
    a host), after `start_receiver`/`start_base_load`."""
    t0 = time.monotonic()
    for burst in schedule:
        wait = burst.start_s - (time.monotonic() - t0)
        if wait > 0:
            time.sleep(wait)
        fire_burst(burst, host_by_name, receiver_ip)
