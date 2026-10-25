"""
Mininet topology for the real deployment (Linux only -- see
docs/linux_deployment.md). Deliberately mirrors
`npsi.sim.network_model.make_incast_scenario` structurally: one leaf switch
with many hosts, an uplink to spine1 (the victim link that suffers incast)
and an uplink to spine2 (the alternate path N-PSI can reroute onto), plus a
receiver host. This keeps the simulation-mode demo and the real deployment
telling the same story on different substrates, rather than being two
unrelated setups.

A general k-ary fat tree builder (`FatTreeTopo`, using the same graph as
`npsi.control.topology_paths.build_fat_tree`) is also provided for anyone
wanting to scale the experiment up -- see the "stretch" section of
FEATURES.md -- but `LeafSpineIncastTopo` is what `run_experiment.py` uses
for the actual evaluation, since it matches the metrics already reported in
docs/results.md.

Also writes `topology.json`, the static topology map `ryu_app.py` reads to
know which (dpid, port) pairs correspond to the victim link and its
alternate, since this project intentionally skips LLDP-based discovery for
a controlled, statically-known testbed.
"""
from __future__ import annotations

import argparse
import json

from mininet.link import TCLink
from mininet.log import setLogLevel
from mininet.net import Mininet
from mininet.node import OVSSwitch, RemoteController
from mininet.topo import Topo

from npsi.control.topology_paths import build_fat_tree

N_INCAST_HOSTS = 20
VICTIM_LINK_ID = "leaf1-spine1"
ALT_LINK_ID = "leaf1-spine2"

# fluid-model capacity in the simulation was 1000 pps on a shallow, ~150-packet
# buffer; translated to a real link, that's a deliberately narrow/shallow
# uplink so the same incast burst actually overflows it, giving N-PSI
# something real to prevent (see network_model.make_incast_scenario's own
# comment on this same design choice).
VICTIM_LINK_BW_MBPS = 20
VICTIM_LINK_MAX_QUEUE_PKTS = 150


class LeafSpineIncastTopo(Topo):
    def build(self):
        leaf1 = self.addSwitch("s1", dpid="0000000000000001")
        spine1 = self.addSwitch("s2", dpid="0000000000000002")
        spine2 = self.addSwitch("s3", dpid="0000000000000003")
        receiver_switch = self.addSwitch("s4", dpid="0000000000000004")

        # the incast victim link: narrow, shallow-buffered, and the one N-PSI watches
        self.addLink(leaf1, spine1, bw=VICTIM_LINK_BW_MBPS, max_queue_size=VICTIM_LINK_MAX_QUEUE_PKTS)
        # the alternate path, ample capacity, unused unless the controller reroutes onto it
        self.addLink(leaf1, spine2, bw=VICTIM_LINK_BW_MBPS * 4, max_queue_size=VICTIM_LINK_MAX_QUEUE_PKTS * 4)

        self.addLink(spine1, receiver_switch, bw=1000)
        self.addLink(spine2, receiver_switch, bw=1000)

        for i in range(N_INCAST_HOSTS):
            h = self.addHost(f"h{i}", ip=f"10.0.0.{i + 1}/24")
            self.addLink(h, leaf1)

        receiver = self.addHost("hr", ip="10.0.0.254/24")
        self.addLink(receiver, receiver_switch)


class FatTreeTopo(Topo):
    """General k-ary fat tree, built from the same graph
    `npsi.control.topology_paths.build_fat_tree` produces. Not used by the
    default evaluation run -- see module docstring."""

    def build(self, k: int = 4):
        G = build_fat_tree(k)
        node_map = {}
        for name, data in G.nodes(data=True):
            if data.get("kind") == "host":
                node_map[name] = self.addHost(name.replace("-", "_"))
            else:
                node_map[name] = self.addSwitch(name.replace("-", "_"))
        for u, v in G.edges():
            self.addLink(node_map[u], node_map[v])


def _port_of(node, other):
    """This node's own port number toward `other`, via the link interfaces
    Mininet assigned when the topology was built."""
    for intf in node.intfList():
        link = intf.link
        if link is None:
            continue
        peer = link.intf1 if link.intf2 == intf else link.intf2
        if peer.node.name == other.name:
            return node.ports[intf]
    raise RuntimeError(f"no link between {node.name} and {other.name}")


def write_topology_map(net: Mininet, out_path: str) -> None:
    """Writes the static topology map `ryu_app.py` reads: which (dpid, port)
    pairs correspond to the victim/alt link (for reroute/throttle), plus a
    full set of proactive default routes and per-host static MAC routes so
    the fabric never needs to flood (see the long comment in ryu_app.py's
    module docstring on why this topology is a physical L2 loop and what
    breaks without this)."""
    leaf1 = net.get("s1")
    spine1 = net.get("s2")
    spine2 = net.get("s3")
    recv_sw = net.get("s4")
    receiver = net.get("hr")

    recv_ip, recv_mask = "10.0.0.254", "255.255.255.255"
    host_subnet, host_mask = "10.0.0.0", "255.255.255.0"

    default_routes = [
        # forward direction: any host -> receiver
        {"dpid": int(leaf1.dpid, 16), "dst_subnet": recv_ip, "dst_mask": recv_mask,
         "out_port": _port_of(leaf1, spine1)},
        {"dpid": int(spine1.dpid, 16), "dst_subnet": recv_ip, "dst_mask": recv_mask,
         "out_port": _port_of(spine1, recv_sw)},
        {"dpid": int(spine2.dpid, 16), "dst_subnet": recv_ip, "dst_mask": recv_mask,
         "out_port": _port_of(spine2, recv_sw)},
        {"dpid": int(recv_sw.dpid, 16), "dst_subnet": recv_ip, "dst_mask": recv_mask,
         "out_port": _port_of(recv_sw, receiver)},
        # return direction: receiver -> host subnet, default via the primary uplink
        {"dpid": int(recv_sw.dpid, 16), "dst_subnet": host_subnet, "dst_mask": host_mask,
         "out_port": _port_of(recv_sw, spine1)},
        {"dpid": int(spine1.dpid, 16), "dst_subnet": host_subnet, "dst_mask": host_mask,
         "out_port": _port_of(spine1, leaf1)},
        {"dpid": int(spine2.dpid, 16), "dst_subnet": host_subnet, "dst_mask": host_mask,
         "out_port": _port_of(spine2, leaf1)},
    ]
    # leaf1 -> each host: static per-MAC route, so return traffic never
    # depends on dynamic MAC learning (which would otherwise still risk one
    # flood on the very first unseen destination)
    for i in range(N_INCAST_HOSTS):
        h = net.get(f"h{i}")
        default_routes.append({"dpid": int(leaf1.dpid, 16), "eth_dst": h.MAC(), "out_port": _port_of(leaf1, h)})

    topology = {
        "links": {
            VICTIM_LINK_ID: {
                "dpid": int(leaf1.dpid, 16),
                "primary_out_port": _port_of(leaf1, spine1),
                "alt_out_port": _port_of(leaf1, spine2),
                "alt_dpid": int(leaf1.dpid, 16),
                "dst_subnet": recv_ip,
                "dst_mask": recv_mask,
            }
        },
        "default_routes": default_routes,
        # both hub switches (degree > 2) never flood -- see ryu_app.py docstring
        "no_flood_dpids": [int(leaf1.dpid, 16), int(recv_sw.dpid, 16)],
    }
    with open(out_path, "w") as f:
        json.dump(topology, f, indent=2)


def setup_static_arp(net: Mininet) -> None:
    """Pre-populates every host's ARP cache for the incast<->receiver pair,
    eliminating ARP broadcast traffic entirely -- belt-and-suspenders on top
    of the no-flood default routes above, and removes ARP round-trip latency
    from the very start of each experiment run."""
    receiver = net.get("hr")
    incast_hosts = [net.get(f"h{i}") for i in range(N_INCAST_HOSTS)]
    for h in incast_hosts:
        h.cmd(f"arp -s {receiver.IP()} {receiver.MAC()}")
        receiver.cmd(f"arp -s {h.IP()} {h.MAC()}")


class UserspaceOVSSwitch(OVSSwitch):
    """OVS in userspace ('netdev') datapath mode -- required on hosts
    without the openvswitch kernel module (e.g. WSL2's custom kernel, many
    containers/cloud VMs). Verified working under WSL2 Ubuntu 22.04 as part
    of validating this project's real deployment path. On a host that DOES
    have the kernel module, the default 'system' datapath is marginally
    faster; switch `datapath` back to 'system' there if desired."""

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("datapath", "user")
        super().__init__(*args, **kwargs)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--controller-ip", default="127.0.0.1")
    ap.add_argument("--controller-port", type=int, default=6653)
    ap.add_argument("--topology-out", default="topology.json")
    ap.add_argument("--kernel-datapath", action="store_true",
                     help="use OVS's kernel datapath instead of userspace (requires the openvswitch kernel module)")
    args = ap.parse_args()

    setLogLevel("info")
    topo = LeafSpineIncastTopo()
    switch_cls = OVSSwitch if args.kernel_datapath else UserspaceOVSSwitch
    net = Mininet(topo=topo, switch=switch_cls, link=TCLink,
                  controller=lambda name: RemoteController(name, ip=args.controller_ip, port=args.controller_port))
    net.start()
    write_topology_map(net, args.topology_out)
    setup_static_arp(net)
    print(f"topology written to {args.topology_out}; CLI ready.")
    from mininet.cli import CLI
    CLI(net)
    net.stop()


if __name__ == "__main__":
    main()
