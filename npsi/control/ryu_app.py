"""
Real OpenFlow 1.3 SDN controller app (Linux only, runs against Mininet+OVS
in the real deployment path -- see docs/linux_deployment.md).

Built on **os-ken**, not upstream Ryu. This is a deliberate substitution,
not a downgrade: upstream `ryu` (last released 2019) is unmaintained and its
`setup.py` and `ryu.app.wsgi` module both depend on OpenStack `oslo.config`
internals that no longer exist in any oslo.config release installable today
(namespace-package layout was dropped ecosystem-wide). `os-ken` is the
actively maintained OpenStack-Neutron fork of the exact same framework --
same event/handler/app_manager model, same OpenFlow protocol library, API
compatible for everything used here. Verified installable and importable
under WSL2 Ubuntu 22.04 (Python 3.10) as part of this project.

One consequence of that swap: the PyPI `os-ken` package ships as a library
only -- it drops Ryu's `ryu.app.wsgi` REST framework and its `ryu-manager`
console script (OpenStack's Neutron agent embeds os-ken directly and never
needed either). Both are replaced here with equivalents that present the
exact same external contract:
  - REST: `eventlet.wsgi.server` (the same mechanism Ryu's own REST
    framework uses internally) instead of `ryu.app.wsgi`'s ControllerBase/
    route decorators -- same two endpoints, same JSON in/out --
    `prediction_service.py` needed no changes. (An earlier version of this
    used stdlib `http.server` on a `threading.Thread`; that silently died
    under eventlet's monkey-patched runtime -- see git history / the
    validation notes in docs/linux_deployment.md.)
  - Launcher: `npsi/control/os_ken_launcher.py` instead of `ryu-manager`,
    using `os_ken.base.app_manager.AppManager.run_apps(...)`, which is
    literally what `ryu-manager`/would-be `os-ken-manager` does internally.

Three responsibilities:
  1. Proactive static routing for the known topology's host<->receiver
     traffic (`topology.json`'s `default_routes`, installed at switch-connect
     time). This is NOT the same as the learning-switch fallback below --
     it exists because this testbed's leaf-spine topology has two physical
     paths to the receiver (the primary uplink and the alternate one N-PSI
     reroutes onto), which is a genuine L2 loop from a naive
     flood-on-unknown-destination switch's point of view. Real SDN
     controllers on multi-path fabrics install explicit routes rather than
     flood for exactly this reason (found by running this against real
     Mininet+OVS under WSL2 -- pingall degraded to ~10% delivery from
     broadcast-storm-style loss until this was added).
  2. A minimal L2 learning switch (MAC-learning, flood-on-unknown) for
     traffic default_routes doesn't cover -- safe here because hosts are
     given static ARP entries (see `fattree_topo.py`), so in practice this
     path only ever forwards already-learned unicast, never floods.
  3. A REST endpoint (`POST /npsi/prediction`) that `prediction_service.py`
     calls with `{"link_id": ..., "score": ...}`. Each call runs through the
     SAME `DecisionEngine` used by the simulation controller (this is the
     whole point -- the control logic is identical, only the actuation
     mechanism differs), and an activate/release transition installs or
     removes:
       - reroute: a higher-priority flow entry sending the victim link's
         traffic out the alternate path's port instead
       - throttle: an OpenFlow meter band capping that traffic's rate

Run with:  python -m npsi.control.os_ken_launcher --topology-file topology.json
"""
from __future__ import annotations

import json
from pathlib import Path

import eventlet
from eventlet import wsgi as eventlet_wsgi
from os_ken.base import app_manager
from os_ken.controller import ofp_event
from os_ken.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER, set_ev_cls
from os_ken.lib.packet import ethernet, packet
from os_ken.ofproto import ofproto_v1_3

from npsi.control.decision import DecisionConfig, DecisionEngine

DEFAULT_TOPOLOGY_FILE = "topology.json"
DEFAULT_REST_PORT = 8080
DEFAULT_ROUTE_PRIORITY = 100  # static topology routes: above learning-switch (1), below reroute override (200)
REROUTE_FLOW_PRIORITY = 200  # above the learning-switch's default (1) and table-miss (0)
THROTTLE_METER_RATE_KBPS = 5000  # applied while a link's action is active


class NPsiControllerApp(app_manager.OSKenApp):
    """L2 learning switch + N-PSI proactive control, OpenFlow 1.3."""

    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.mac_to_port: dict[int, dict[str, int]] = {}
        self.datapaths: dict[int, object] = {}
        self.decision_engine = DecisionEngine(DecisionConfig())
        self.active_meters: dict[str, int] = {}  # link_id -> meter_id currently installed
        self._next_meter_id = 1

        topo_path = kwargs.get("topology_file", DEFAULT_TOPOLOGY_FILE)
        self.topology = self._load_topology(topo_path)

        rest_port = kwargs.get("rest_port", DEFAULT_REST_PORT)
        self._start_rest_server(rest_port)

    @staticmethod
    def _load_topology(path: str) -> dict:
        """Expected shape (written by npsi/testbed/fattree_topo.py):
        {
          "links": {
            "leaf1-spine1": {"dpid": 1, "out_port": 2, "alt_dpid": 1, "alt_out_port": 3}
          }
        }
        Falls back to an empty topology (learning-switch-only mode) if the
        file isn't there yet, so the controller still comes up cleanly during
        development."""
        p = Path(path)
        if not p.exists():
            return {"links": {}}
        return json.loads(p.read_text())

    def _start_rest_server(self, port: int) -> None:
        """Plain WSGI callable served by eventlet's own `wsgi.server` (the
        same mechanism Ryu's own REST framework uses internally), run in a
        separate greenthread via `eventlet.spawn` so it's scheduled
        cooperatively alongside the OpenFlow event loop -- no interaction
        with stdlib `threading`/`http.server` under eventlet's monkey-patch,
        which is fragile (see the note in os_ken_launcher.py / ryu_app.py
        module docstring history: an earlier stdlib-threading version of
        this server silently died under monkey-patching)."""
        app = self

        def wsgi_app(environ, start_response):
            method = environ.get("REQUEST_METHOD", "GET")
            path = environ.get("PATH_INFO", "")

            def respond(status_line: str, payload: dict):
                body = json.dumps(payload).encode("utf-8")
                start_response(status_line, [("Content-Type", "application/json"), ("Content-Length", str(len(body)))])
                return [body]

            if method == "GET" and path == "/npsi/health":
                return respond("200 OK", {"status": "ok"})

            if method == "POST" and path == "/npsi/prediction":
                try:
                    length = int(environ.get("CONTENT_LENGTH") or 0)
                    raw = environ["wsgi.input"].read(length)
                    body = json.loads(raw)
                    link_id = str(body["link_id"])
                    score = float(body["score"])
                except (KeyError, ValueError, TypeError, json.JSONDecodeError) as exc:
                    return respond("400 Bad Request", {"error": str(exc)})
                result = app.handle_prediction(link_id, score)
                return respond("200 OK", result)

            return respond("404 Not Found", {"error": "not found"})

        sock = eventlet.listen(("0.0.0.0", port))
        eventlet.spawn(eventlet_wsgi.server, sock, wsgi_app, log_output=False)
        self.logger.info("N-PSI REST endpoint listening on :%d (/npsi/prediction, /npsi/health)", port)

    # --- standard L2 learning switch --------------------------------------------------
    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        datapath = ev.msg.datapath
        self.datapaths[datapath.id] = datapath
        ofproto, parser = datapath.ofproto, datapath.ofproto_parser
        match = parser.OFPMatch()
        actions = [parser.OFPActionOutput(ofproto.OFPP_CONTROLLER, ofproto.OFPCML_NO_BUFFER)]
        self._add_flow(datapath, priority=0, match=match, actions=actions)
        self._install_default_routes(datapath)

    def _install_default_routes(self, datapath) -> None:
        """Static routes for this dpid from topology.json's `default_routes`
        (see module docstring, responsibility 1). Installed once per switch
        connect, unconditionally -- these are the "always forward known
        destinations this way" rules that make the topology loop-safe
        without relying on flooding. Each entry matches either an IP
        subnet (`dst_subnet`/`dst_mask`) or a specific host MAC (`eth_dst`,
        used for leaf-switch return traffic so it never depends on dynamic
        MAC learning, which would otherwise still risk a flood on the first
        unseen destination)."""
        parser = datapath.ofproto_parser
        for route in self.topology.get("default_routes", []):
            if route["dpid"] != datapath.id:
                continue
            if "eth_dst" in route:
                match = parser.OFPMatch(eth_dst=route["eth_dst"])
                desc = f"eth_dst={route['eth_dst']}"
            else:
                match = parser.OFPMatch(eth_type=0x0800,
                                         ipv4_dst=(route["dst_subnet"], route.get("dst_mask", "255.255.255.255")))
                desc = f"{route['dst_subnet']}/{route.get('dst_mask', '255.255.255.255')}"
            actions = [parser.OFPActionOutput(route["out_port"])]
            self._add_flow(datapath, priority=DEFAULT_ROUTE_PRIORITY, match=match, actions=actions)
            self.logger.info("installed default route on dpid=%s: %s -> port %s", datapath.id, desc, route["out_port"])

    def _add_flow(self, datapath, priority, match, actions, meter_id=None, idle_timeout=0):
        ofproto, parser = datapath.ofproto, datapath.ofproto_parser
        inst = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, actions)]
        if meter_id is not None:
            inst.insert(0, parser.OFPInstructionMeter(meter_id, ofproto.OFPIT_METER))
        mod = parser.OFPFlowMod(datapath=datapath, priority=priority, match=match,
                                 instructions=inst, idle_timeout=idle_timeout)
        datapath.send_msg(mod)

    def _del_flow(self, datapath, priority, match):
        ofproto, parser = datapath.ofproto, datapath.ofproto_parser
        mod = parser.OFPFlowMod(datapath=datapath, command=ofproto.OFPFC_DELETE,
                                 out_port=ofproto.OFPP_ANY, out_group=ofproto.OFPG_ANY,
                                 priority=priority, match=match)
        datapath.send_msg(mod)

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def packet_in_handler(self, ev):
        msg = ev.msg
        datapath = msg.datapath
        ofproto, parser = datapath.ofproto, datapath.ofproto_parser
        in_port = msg.match["in_port"]

        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocol(ethernet.ethernet)
        dpid = datapath.id
        self.mac_to_port.setdefault(dpid, {})
        self.mac_to_port[dpid][eth.src] = in_port

        out_port = self.mac_to_port[dpid].get(eth.dst)
        if out_port is None:
            if dpid in self.topology.get("no_flood_dpids", []):
                # this dpid has >2 ports and default_routes/static ARP should
                # already cover every real destination; flooding here would
                # risk re-entering the topology's physical loop (see module
                # docstring) so an unknown destination is dropped instead.
                return
            out_port = ofproto.OFPP_FLOOD

        actions = [parser.OFPActionOutput(out_port)]
        if out_port != ofproto.OFPP_FLOOD:
            match = parser.OFPMatch(in_port=in_port, eth_dst=eth.dst)
            self._add_flow(datapath, priority=1, match=match, actions=actions, idle_timeout=60)

        data = msg.data if msg.buffer_id == ofproto.OFP_NO_BUFFER else None
        out = parser.OFPPacketOut(datapath=datapath, buffer_id=msg.buffer_id,
                                   in_port=in_port, actions=actions, data=data)
        datapath.send_msg(out)

    # --- N-PSI proactive control --------------------------------------------------
    def handle_prediction(self, link_id: str, score: float) -> dict:
        """Called by the REST endpoint above. Runs `score` through the shared
        DecisionEngine and, on a state transition, installs/removes the
        corresponding OpenFlow rules. Returns a small status dict for the
        REST response / logging."""
        action = self.decision_engine.decide(link_id, score)
        link_info = self.topology.get("links", {}).get(link_id)
        if link_info is None:
            self.logger.warning("no topology entry for link_id=%s -- decision computed but not enforced", link_id)
            return {"link_id": link_id, "active": action.active, "enforced": False}

        datapath = self.datapaths.get(link_info["dpid"])
        if datapath is None:
            self.logger.warning("switch dpid=%s not connected yet -- decision computed but not enforced", link_info["dpid"])
            return {"link_id": link_id, "active": action.active, "enforced": False}

        if action.active:
            self._install_reroute(datapath, link_id, link_info, action.reroute_fraction)
            self._install_throttle(datapath, link_id, link_info, action.throttle_factor)
        else:
            self._remove_reroute(datapath, link_id, link_info)
            self._remove_throttle(datapath, link_id)

        return {"link_id": link_id, "active": action.active, "enforced": True,
                "reroute_fraction": action.reroute_fraction, "throttle_factor": action.throttle_factor}

    def _victim_match(self, datapath, link_info: dict):
        """Matches purely on destination IP (the incast receiver), not
        in_port: an incast burst arrives from many different host ports on
        the leaf switch simultaneously, so pinning the match to one in_port
        would only ever catch one sender. Priority 200 (REROUTE_FLOW_PRIORITY)
        is higher than the learning switch's per-eth_dst rules (priority 1),
        so this correctly overrides the default forwarding decision for
        every sender converging on that destination, regardless of in_port."""
        parser = datapath.ofproto_parser
        if "dst_subnet" not in link_info:
            raise ValueError(f"topology entry for a controlled link must define dst_subnet: {link_info}")
        return parser.OFPMatch(eth_type=0x0800,
                                ipv4_dst=(link_info["dst_subnet"], link_info.get("dst_mask", "255.255.255.255")))

    def _install_reroute(self, datapath, link_id: str, link_info: dict, reroute_fraction: float) -> None:
        parser = datapath.ofproto_parser
        match = self._victim_match(datapath, link_info)
        actions = [parser.OFPActionOutput(link_info["alt_out_port"])]
        self._add_flow(datapath, priority=REROUTE_FLOW_PRIORITY, match=match, actions=actions)
        self.logger.info("N-PSI: rerouting %s -> alt path (fraction=%.2f)", link_id, reroute_fraction)

    def _remove_reroute(self, datapath, link_id: str, link_info: dict) -> None:
        match = self._victim_match(datapath, link_info)
        self._del_flow(datapath, priority=REROUTE_FLOW_PRIORITY, match=match)
        self.logger.info("N-PSI: released reroute on %s", link_id)

    def _install_throttle(self, datapath, link_id: str, link_info: dict, throttle_factor: float) -> None:
        ofproto, parser = datapath.ofproto, datapath.ofproto_parser
        meter_id = self.active_meters.get(link_id)
        if meter_id is None:
            meter_id = self._next_meter_id
            self._next_meter_id += 1
            self.active_meters[link_id] = meter_id

        rate_kbps = max(1, int(THROTTLE_METER_RATE_KBPS * throttle_factor))
        band = parser.OFPMeterBandDrop(rate=rate_kbps, burst_size=rate_kbps // 10 or 1)
        mod = parser.OFPMeterMod(datapath=datapath, command=ofproto.OFPMC_ADD,
                                  flags=ofproto.OFPMF_KBPS, meter_id=meter_id, bands=[band])
        datapath.send_msg(mod)

        match = self._victim_match(datapath, link_info)
        actions = [parser.OFPActionOutput(link_info["primary_out_port"])]
        self._add_flow(datapath, priority=REROUTE_FLOW_PRIORITY - 1, match=match,
                        actions=actions, meter_id=meter_id)
        self.logger.info("N-PSI: throttling %s to %d kbps", link_id, rate_kbps)

    def _remove_throttle(self, datapath, link_id: str) -> None:
        ofproto, parser = datapath.ofproto, datapath.ofproto_parser
        meter_id = self.active_meters.pop(link_id, None)
        if meter_id is None:
            return
        mod = parser.OFPMeterMod(datapath=datapath, command=ofproto.OFPMC_DELETE, meter_id=meter_id)
        datapath.send_msg(mod)
        self.logger.info("N-PSI: removed throttle meter on %s", link_id)
