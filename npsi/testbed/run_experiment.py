"""
CLI: orchestrates ONE full run of the real Mininet+OVS+Ryu testbed for a
single variant (the real-deployment counterpart of `npsi.sim.sim_e2e`).
Linux only -- see docs/linux_deployment.md.

Single command, self-contained:

    sudo python3 -m npsi.testbed.run_experiment --variant npsi --duration 120

This launches its OWN controller subprocess, deliberately AFTER the
topology is built and `topology.json` is written -- not before. The
controller loads `topology.json` exactly once, at its own startup, and real
dpid/port numbers are only known once Mininet has actually built the
topology; starting the controller first would make it load an empty
topology and silently install none of the default routes (found by running
this against real Mininet+OVS under WSL2 -- every host<->receiver ping
failed with the routes correctly computed but never installed, because the
controller that was already running had loaded topology.json before it had
real content). OVS's automatic controller-reconnect means starting switches
before their controller exists is fine; it's the reverse -- controller
before topology.json is accurate -- that silently breaks routing.

Each run launches a FRESH controller instance, so each variant's
DecisionEngine starts from a clean state, exactly like sim_e2e.py building a
fresh NetworkModel per variant.

Writes `results/real_<variant>_summary.json`, in the same spirit as
`results/comparison_metrics.json` from the simulation, so the two can be
compared once real hardware/testbed numbers are available.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from mininet.link import TCLink
from mininet.log import setLogLevel
from mininet.net import Mininet
from mininet.node import RemoteController

from npsi.testbed import baselines, traffic_gen
from npsi.testbed.fattree_topo import LeafSpineIncastTopo, UserspaceOVSSwitch, setup_static_arp, write_topology_map

PORT_SAMPLE_INTERVAL_S = 0.5


def start_controller(topology_out: str, ofp_port: int, rest_port: int, ready_timeout_s: float = 15.0) -> subprocess.Popen:
    """Launches os_ken_launcher.py as a subprocess and blocks until its
    /npsi/health REST endpoint responds, so callers know it has actually
    loaded topology.json (with real values, since this is called after
    write_topology_map) before any switch traffic is generated."""
    proc = subprocess.Popen([
        sys.executable, "-m", "npsi.control.os_ken_launcher",
        "--topology-file", topology_out,
        "--ofp-tcp-listen-port", str(ofp_port),
        "--rest-port", str(rest_port),
    ])
    deadline = time.monotonic() + ready_timeout_s
    url = f"http://127.0.0.1:{rest_port}/npsi/health"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.0) as resp:
                if resp.status == 200:
                    return proc
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            pass
        time.sleep(0.5)
    proc.terminate()
    raise RuntimeError(f"controller did not become ready within {ready_timeout_s}s")


def find_victim_interface(net) -> str:
    """The interface name on leaf1 (s1) facing spine1 (s2) -- the link
    eBPF/prediction_service watches and OVS counters are read from."""
    leaf1 = net.get("s1")
    for intf in leaf1.intfList():
        link = intf.link
        if link is None:
            continue
        peer = link.intf1 if link.intf2 == intf else link.intf2
        if peer.node.name == "s2":
            return intf.name
    raise RuntimeError("could not find leaf1<->spine1 interface")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--variant", choices=baselines.VARIANTS, required=True)
    ap.add_argument("--duration", type=float, default=120.0)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--controller-ip", default="127.0.0.1")
    ap.add_argument("--controller-port", type=int, default=6653)
    ap.add_argument("--ryu-url", default="http://127.0.0.1:8080")
    ap.add_argument("--topology-out", default="topology.json")
    ap.add_argument("--out-dir", default="results")
    ap.add_argument("--kernel-datapath", action="store_true",
                     help="use OVS's kernel datapath instead of userspace (requires the openvswitch kernel module)")
    args = ap.parse_args()

    setLogLevel("info")
    topo = LeafSpineIncastTopo()
    if args.kernel_datapath:
        from mininet.node import OVSSwitch
        switch_cls = OVSSwitch
    else:
        switch_cls = UserspaceOVSSwitch
    net = Mininet(topo=topo, switch=switch_cls, link=TCLink,
                  controller=lambda name: RemoteController(name, ip=args.controller_ip, port=args.controller_port))
    net.start()
    write_topology_map(net, args.topology_out)
    setup_static_arp(net)

    rest_port = int(args.ryu_url.rsplit(":", 1)[-1])
    print("[run_experiment] starting controller (fresh instance, post-topology)...")
    controller_proc = start_controller(args.topology_out, args.controller_port, rest_port)
    print("[run_experiment] controller ready")

    victim_iface = find_victim_interface(net)
    print(f"[run_experiment] victim interface: {victim_iface}")

    handles = baselines.start_variant(args.variant, net, victim_iface, ryu_url=args.ryu_url)

    receiver = net.get("hr")
    incast_hosts = [net.get(f"h{i}") for i in range(20)]
    host_by_name = {h.name: h for h in incast_hosts}
    traffic_gen.start_receiver(receiver)
    schedule = traffic_gen.generate_incast_schedule([h.name for h in incast_hosts], args.duration, seed=args.seed)

    stop_event = threading.Event()

    def sampler_loop():
        while not stop_event.is_set():
            baselines.sample_port_stats(handles)
            time.sleep(PORT_SAMPLE_INTERVAL_S)

    sampler_thread = threading.Thread(target=sampler_loop, daemon=True)
    sampler_thread.start()

    print(f"[run_experiment] running variant '{args.variant}' for {args.duration}s ...")
    traffic_gen.run_schedule(schedule, host_by_name, receiver.IP())
    time.sleep(2.0)  # drain trailing traffic before the final sample

    stop_event.set()
    sampler_thread.join(timeout=2.0)

    summary = baselines.summarize_counters(handles)
    summary["variant"] = args.variant

    baselines.stop_variant(handles)
    traffic_gen.stop_receiver(receiver)
    controller_proc.terminate()
    try:
        controller_proc.wait(timeout=5.0)
    except subprocess.TimeoutExpired:
        controller_proc.kill()
    net.stop()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"real_{args.variant}_summary.json"
    out_path.write_text(json.dumps(summary, indent=2))
    print(f"[run_experiment] wrote {out_path}: {summary}")


if __name__ == "__main__":
    main()
