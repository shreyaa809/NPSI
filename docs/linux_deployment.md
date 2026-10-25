# Real Deployment: eBPF + Mininet + Open vSwitch + os-ken

Everything in this doc requires a real Linux kernel (WSL2 is fine, and is
what this was actually validated against). None of it is needed to
reproduce `results/RESULTS.md` -- that comes entirely from
`npsi/sim/sim_e2e.py`, which runs on Windows. This is the path to validate
the same system against a real kernel and real OpenFlow switches.

**Validation status**: the control plane (real eBPF via bcc, real OpenFlow
via a real SDN controller, real Mininet+OVS dataplane, the full project
topology with 20 incast hosts) was brought up and validated end-to-end under
WSL2 Ubuntu 22.04 as part of building this project -- see "What was actually
validated" below for exactly what was checked and what's still open. Three
real, substantive issues were found and fixed in the process; they're
documented here rather than smoothed over, because the fixes are things
anyone deploying this on a different Linux host should know about.

## 0. A load-bearing correction: this runs on os-ken, not Ryu

The original plan (and the project report) names Ryu as the SDN controller.
While setting up this environment, installing upstream `ryu` (last released
2019) failed outright: its `setup.py` and `ryu.app.wsgi` module both depend
on OpenStack `oslo.config` internals (a legacy namespace-package layout)
that no longer exist in any `oslo.config` release installable today -- the
whole OpenStack ecosystem dropped that packaging style. This isn't a local
misconfiguration; it's upstream Ryu being unmaintained since 2019.

The fix used throughout this codebase: **os-ken**, the actively maintained
OpenStack-Neutron fork of the exact same framework (same event/handler/
app_manager model, same OpenFlow protocol library). `npsi/control/ryu_app.py`
is built on `os_ken.*` imports. Two things os-ken's PyPI package drops that
Ryu had (OpenStack's Neutron agent never needed them, since it embeds
os-ken as a library, not a standalone controller):
  - `ryu.app.wsgi` (the REST framework) -- replaced with a small
    `eventlet.wsgi.server`-based endpoint in `ryu_app.py` presenting the
    identical `/npsi/prediction` / `/npsi/health` JSON contract, so
    `prediction_service.py` needed no changes.
  - the `ryu-manager` console script -- replaced with
    `npsi/control/os_ken_launcher.py`, a ~40-line equivalent using
    `os_ken.base.app_manager.AppManager.run_apps(...)`, which is what
    `ryu-manager` does internally anyway.

Run the controller with:
```bash
python3 -m npsi.control.os_ken_launcher --topology-file topology.json
```
not `ryu-manager npsi/control/ryu_app.py` (that command will not work in
this environment for the reason above).

## 1. Environment setup (WSL2 Ubuntu 22.04+, or a native Linux VM)

```bash
sudo apt update
sudo apt install -y mininet openvswitch-switch python3-bpfcc bpfcc-tools \
    linux-headers-$(uname -r) iperf3

python3 -m venv --system-site-packages .venv-linux   # --system-site-packages: bcc's
source .venv-linux/bin/activate                       # Python bindings come from the
pip install -r requirements-linux.txt                 # apt package, not pip
```

`--system-site-packages` matters: `python3-bpfcc` (real eBPF bindings)
installs into the system Python, not into an isolated venv. Without this
flag, `from bcc import BPF` will fail inside the venv even though it works
system-wide.

Verify eBPF works at all before trusting anything downstream:

```bash
sudo python3 -c "from bcc import BPF; print('bcc OK')"
```
This was confirmed working under WSL2's kernel (5.15.167.4-microsoft-standard-WSL2,
`CONFIG_BPF_SYSCALL=y`, `CONFIG_KPROBES=y`, `CONFIG_BPF_EVENTS=y` all present).

**No systemd in WSL2**: `apt install openvswitch-switch` will report a
post-install failure (`Could not execute systemctl`) because WSL2 doesn't
run systemd by default. The binaries install fine regardless; start OVS
manually instead of via the (nonfunctional) systemd unit:
```bash
mkdir -p /var/run/openvswitch /etc/openvswitch
/usr/share/openvswitch/scripts/ovs-ctl --system-id=random start --no-ovs-vswitchd
ovs-vsctl --no-wait init
ovs-vswitchd --pidfile --detach --log-file
```

**No OVS kernel module in WSL2**: `modprobe openvswitch` fails (`Module
openvswitch not found`) -- WSL2's kernel doesn't ship out-of-tree modules.
Use OVS's userspace ("netdev") datapath instead, which needs no kernel
module. This project's Mininet topology already does this by default --
`npsi/testbed/fattree_topo.py`'s `UserspaceOVSSwitch` sets
`datapath='user'`. Pass `--kernel-datapath` to `fattree_topo.py` or
`run_experiment.py` on a host that *does* have the module (real bare-metal
Linux server, most cloud VMs) for marginally better performance.

## 2. Bring up the testbed

```bash
sudo python3 -m npsi.testbed.fattree_topo --topology-out topology.json
```
This brings up Mininet, writes `topology.json`, and drops into the Mininet
CLI. It does **not** start a controller -- see the ordering note below for
why, and use `run_experiment.py` (next section) for anything beyond manual
CLI exploration.

## 3. Run one variant (self-contained, one command)

```bash
sudo python3 -m npsi.testbed.run_experiment --variant reactive --duration 120
sudo python3 -m npsi.testbed.run_experiment --variant flow_stat --duration 120
sudo python3 -m npsi.testbed.run_experiment --variant npsi --duration 120
```

`run_experiment.py` is self-contained: it builds the topology, writes
`topology.json`, **then launches its own controller subprocess**, waits for
it to be ready, and only then starts traffic. This ordering matters --
see the next section. Each invocation writes
`results/real_<variant>_summary.json`.

### Why the controller is launched *after* the topology, not before

Earlier drafts of this project (and the natural instinct) had the
controller started first, in its own terminal, with `run_experiment.py`
run afterward in a second terminal. **This silently breaks routing.** The
controller loads `topology.json` exactly once, at its own startup. Real
dpid/port numbers are only known once Mininet has actually built the
topology -- so a controller started first loads an *empty* topology, and
every proactive route/reroute rule this project installs (see below) is
silently never installed. Symptom observed while validating this: every
`ping` from every host to the receiver failed 100%, with zero errors
logged anywhere, because nothing was actually wrong with the packets --
the switches just had no rule telling them where to send it.

OVS's own automatic controller-reconnect makes the fix trivial: switches
started *before* their controller exists just keep retrying until it comes
up, so building the topology first and starting the controller second
(which `run_experiment.py` now does automatically) works cleanly.

## 4. Why this topology needs proactive static routes, not just a learning switch

`LeafSpineIncastTopo` has two physical paths to the receiver (the primary
uplink N-PSI watches, and the alternate uplink it can reroute onto) --
which is, from a naive flood-on-unknown-destination L2 switch's
perspective, a genuine loop (leaf1 → spine1 → receiver-switch → spine2 →
leaf1). Real SDN controllers on multi-path fabrics install explicit routes
rather than flood for exactly this reason -- it's not a simplification this
project skipped, it's the actual reason SDN controllers exist instead of
plain switches.

`npsi/control/ryu_app.py` handles this with `default_routes`: proactive,
static, priority-100 flows (below the N-PSI reroute override at priority
200, above the learning-switch fallback at priority 1) computed by
`fattree_topo.py::write_topology_map` for every hop of the host↔receiver
path, plus a **per-host static MAC route on the leaf switch** so return
traffic never depends on dynamic MAC learning either. Hub switches (leaf1,
the receiver switch -- both degree > 2) have flooding disabled entirely
(`no_flood_dpids`) as a backstop, and `setup_static_arp()` pre-populates
every host's ARP cache so no ARP broadcast is ever needed in the first
place. With all three in place, host↔receiver connectivity was validated
at 20/20 (100%) under WSL2.

(Symptom before this was in place: `ping` succeeded for a trivial 2-host,
1-switch topology but failed almost completely once the real 4-switch,
21-host topology was used -- the loop only manifests once there's an actual
redundant path for flooded traffic to circulate through.)

## 5. Known environment limitation: no bandwidth/queue shaping under WSL2

Mininet's `TCLink` (used for the narrow, shallow-buffered victim uplink
that makes the incast scenario actually overflow) configures bandwidth and
queue limits via Linux `tc` using the `htb` queueing discipline. WSL2's
kernel does not compile in `htb`, `tbf`, or `netem`
(`zcat /proc/config.gz | grep CONFIG_NET_SCH_` shows only `fq_codel`,
`multiq`, `ingress`, and plain `fifo` -- confirmed while validating this
project). `tc qdisc add ... htb ...` fails with "Specified qdisc kind is
unknown" on every link. This is a WSL2-kernel-specific gap, not a code bug
-- a standard Ubuntu server or cloud VM kernel has all three compiled in,
and `TCLink` will work there unmodified.

Practical effect: under WSL2 specifically, link connectivity is fully
correct (see above) but the artificial capacity/queue constraint that
makes the incast burst actually overflow the buffer is not enforced, so a
full `run_experiment.py` traffic run under WSL2 won't reproduce the loss
numbers in `docs/results.md` (those come from the simulation, which has no
such constraint). Two ways to get real capacity-constrained numbers:
  1. Run on a Linux host/VM with a standard kernel (has `htb`).
  2. Use OpenFlow meters for the constraint instead of `tc` -- the
     N-PSI *throttle* action already uses `OFPMeterMod`, which OVS
     implements itself rather than via Linux `tc`, so it does not depend
     on this kernel gap. A baseline (always-on, not N-PSI-triggered) meter
     on the victim link's default flow would approximate the same shallow-
     capacity scenario without touching `tc` at all; not yet wired into
     `fattree_topo.py` -- tracked in FEATURES.md.

## 6. What was actually validated (and what wasn't)

Validated, under WSL2 Ubuntu 22.04, real kernel, real OVS, real switches:
- `from bcc import BPF` -- real eBPF bindings import and initialize.
- `os_ken_launcher.py` starts a real OpenFlow 1.3 controller, REST endpoint
  responds (`/npsi/health`, `/npsi/prediction`), and a prediction correctly
  flows through `DecisionEngine` end to end.
- A real 2-host/1-switch Mininet topology connects to the controller and
  forwards traffic (`pingall`: 0% loss).
- The actual `LeafSpineIncastTopo` (21 hosts, 4 switches, redundant paths)
  connects and forwards: 20/20 host→receiver pings succeed once the
  ordering and routing fixes above are in place.

Not yet validated (needs either a non-WSL2 Linux host or further work):
- A full bursty-traffic experiment run with real packet loss numbers
  (blocked on the `tc`/`htb` gap above).
- Real eBPF kprobes (`tcp_sendmsg`, `dev_queue_xmit`) firing under actual
  incast load and producing sane stall-time values -- the kprobes attach
  successfully (bcc loads), but weren't exercised under real traffic in
  this session.
- The N-PSI reroute/throttle flow-mods actually taking effect under a real
  overflowing queue (mechanically exercised via a direct REST POST with a
  synthetic score during validation; not yet exercised via a real N-PSI
  prediction from real telemetry end to end).

## 7. What to check first if something doesn't work

- **`bcc` import fails**: kernel headers usually the culprit --
  `apt install linux-headers-$(uname -r)`. Kernel version skew between
  build and running kernel is the most common bcc failure mode; `uname -r`
  must match an installed headers package.
- **Switches never connect / controller never sees `EventOFPSwitchFeatures`**:
  check `ovs-vsctl show` for the switches' configured controller IP/port,
  and confirm nothing else is bound to the OpenFlow port (default 6653).
- **REST POSTs succeed (200 OK) but no flow-mods appear**: check
  `topology.json` actually has an entry for the link you're posting about,
  and that the controller was started *after* the topology was written
  (section 3). `ryu_app.py` logs a warning (`no topology entry for
  link_id=...`) rather than crashing, specifically so this fails loud in
  the log instead of silently.
- **No stall events from `ebpf_probes.py`**: the kprobes are attached to
  `tcp_sendmsg`/`dev_queue_xmit`, which only fire if those exact kernel
  symbols exist -- `sudo bpftrace -l 'kprobe:tcp_sendmsg*'` confirms the
  symbol is present before debugging further upstream.
