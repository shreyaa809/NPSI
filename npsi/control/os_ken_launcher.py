"""
Minimal replacement for the `ryu-manager` CLI, which this project cannot
use directly -- see the module docstring in `npsi/control/ryu_app.py` for
why (upstream Ryu's dependency chain is broken; we run on os-ken instead,
and the PyPI `os-ken` package ships as a library without its own manager
console script). This does exactly what `ryu-manager`/`os-ken-manager`
would: eventlet monkey-patch, load the built-in OpenFlow protocol handler
app plus our app, and block until they stop.

Run:
    python -m npsi.control.os_ken_launcher --topology-file topology.json --rest-port 8080
"""
from __future__ import annotations

import argparse

import eventlet

eventlet.monkey_patch()

from os_ken.base.app_manager import AppManager  # noqa: E402  (must follow monkey_patch)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--topology-file", default="topology.json")
    ap.add_argument("--rest-port", type=int, default=8080)
    ap.add_argument("--ofp-tcp-listen-port", type=int, default=6653)
    args = ap.parse_args()

    import os_ken.controller.controller as controller_mod
    controller_mod.OFP_TCP_LISTEN_PORT = args.ofp_tcp_listen_port

    app_mgr = AppManager.get_instance()
    # load_apps (plural) is the method that actually registers classes into
    # applications_cls -- load_app (singular) only locates/returns a class
    # without registering it, which silently produces zero instantiated
    # services if called directly (caught while validating this launcher;
    # see scripts/wsl_debug_launcher.py).
    app_mgr.load_apps(["os_ken.controller.ofp_handler", "npsi.control.ryu_app"])
    if not app_mgr.applications_cls:
        raise RuntimeError("no apps registered -- load_apps found nothing")

    contexts = app_mgr.create_contexts()
    services = app_mgr.instantiate_apps(
        topology_file=args.topology_file, rest_port=args.rest_port, **contexts
    )
    print(f"[os_ken_launcher] running on OpenFlow port {args.ofp_tcp_listen_port}, "
          f"REST port {args.rest_port}, topology={args.topology_file}")
    from os_ken.lib import hub
    hub.joinall(services)


if __name__ == "__main__":
    main()
