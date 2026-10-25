"""
Fat-tree topology construction + path computation, shared by the Mininet
testbed script (which needs to wire up the same topology) and the Ryu app's
static routing table. Topology is treated as statically known here (no LLDP
discovery) since the testbed's topology is fixed and known at build time --
a standard, explicitly-scoped simplification for a controlled experiment
(see docs/architecture.md).
"""
from __future__ import annotations

import itertools

import networkx as nx


def build_fat_tree(k: int = 4) -> nx.Graph:
    """Standard k-ary fat tree (Al-Fares et al., 2008): (k/2)^2 core switches,
    k pods each with k/2 aggregation + k/2 edge switches, each edge switch
    connects to k/2 hosts. k must be even."""
    if k % 2 != 0 or k < 2:
        raise ValueError("k must be a positive even number")
    half = k // 2
    G = nx.Graph()

    core_nodes = [f"core-{i}" for i in range(half * half)]
    G.add_nodes_from(core_nodes, kind="core")

    for pod in range(k):
        agg_nodes = [f"pod{pod}-agg{j}" for j in range(half)]
        edge_nodes = [f"pod{pod}-edge{j}" for j in range(half)]
        G.add_nodes_from(agg_nodes, kind="agg", pod=pod)
        G.add_nodes_from(edge_nodes, kind="edge", pod=pod)

        for e in edge_nodes:
            for a in agg_nodes:
                G.add_edge(e, a)

        for j, a in enumerate(agg_nodes):
            for m in range(half):
                core_idx = j * half + m
                G.add_edge(a, core_nodes[core_idx])

        for j, e in enumerate(edge_nodes):
            for h in range(half):
                host = f"pod{pod}-edge{j}-h{h}"
                G.add_node(host, kind="host", pod=pod)
                G.add_edge(host, e)

    return G


def k_shortest_paths(G: nx.Graph, src: str, dst: str, k: int = 4) -> list[list[str]]:
    return list(itertools.islice(nx.shortest_simple_paths(G, src, dst), k))


def alternate_path(G: nx.Graph, src: str, dst: str, primary_path: list[str], k: int = 6) -> list[str] | None:
    """First shortest path distinct from `primary_path`, or None if the
    fabric has no edge-disjoint alternative (shouldn't happen in a fat tree
    with k >= 4 for any two distinct pods)."""
    for path in k_shortest_paths(G, src, dst, k=k):
        if path != primary_path:
            return path
    return None


def path_to_link_ids(path: list[str]) -> list[str]:
    """Node path -> ordered "a-b" link identifiers, matching the telemetry
    layer's link_id naming convention."""
    return [f"{path[i]}-{path[i + 1]}" for i in range(len(path) - 1)]


def hosts(G: nx.Graph) -> list[str]:
    return [n for n, data in G.nodes(data=True) if data.get("kind") == "host"]
