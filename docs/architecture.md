# N-PSI Architecture

## Why a dual data-plane backend

The system this project specifies — eBPF kernel probes, Mininet + Open
vSwitch, a Ryu SDN controller — fundamentally requires a Linux kernel.
Development happens on Windows. Rather than either (a) writing code that
can't be exercised until deployment day, or (b) building a toy demo
disconnected from the real system, this project is structured as **one
pipeline, two data-plane backends**, joined by a single shared contract: the
telemetry event schema in `npsi/telemetry/events.py`
(`StallEvent`/`QueueSample`/`FlowCounterSample`).

```
                 ┌─────────────────────────────────────────────┐
                 │   npsi/features, npsi/ml, npsi/control       │
                 │   (feature engineering, XGBoost/LSTM,        │
                 │    decision engine — 100% backend-agnostic)  │
                 └───────────────▲───────────────────────────---┘
                                  │  StallEvent / QueueSample
                  ┌───────────────┴────────────────┐
                  │                                 │
         npsi/telemetry/simulator.py       npsi/telemetry/ebpf_probes.py
         (fluid queue model,               (real BCC kprobes on
          runs anywhere, Windows            tcp_sendmsg / dev_queue_xmit,
          included)                         Linux + bcc only)
                  │                                 │
         npsi/sim/network_model.py         Mininet + Open vSwitch
         + npsi/control/sim_controller.py   + npsi/control/ryu_app.py
         (closed loop entirely in           (real OpenFlow flow-mods
          Python, no root needed)            + meter tables)
```

Everything above the dashed line — N-PSI score computation, the XGBoost/LSTM
models, the hysteresis/cooldown decision engine — is written once and used
identically by both backends. This is not a simplification for the sake of
running on Windows; it is the actual claim under test: **the same signal and
the same control logic**, fed either synthetic or real kernel telemetry,
should behave the same way. Swapping backends is a one-line change
(`npsi/telemetry/collector.py::make_collector`).

## Why a fluid (rate-based) queue model, not packet-level

`npsi/sim/network_model.py` models each link's backlog as a continuous
quantity evolving from arrival-rate minus service-rate, rather than
simulating individual packets. This is standard for congestion-control
studies where queue *occupancy over time* is the object of interest (DCTCP's
own analysis is largely fluid-model-based). It lets a 120-second, multi-burst
scenario simulate in under a second, which is what makes an interactive,
reproducible Windows demo possible at all — a packet-level ns-3-style
simulator would take minutes to hours for the same coverage.

## The three evaluated variants, and what makes the comparison fair

All three variants — reactive, flow-stat baseline, N-PSI — are driven by the
exact same `DecisionEngine` (`npsi/control/decision.py`): same hysteresis
band, same cooldown, same reroute/throttle actions. The only thing that
differs is the **signal** feeding that engine:

| Variant | Signal | What it can "see" |
|---|---|---|
| Reactive | current backlog/capacity ratio, only above ~95% | Nothing until the queue is already essentially full — the classic loss/RTT-triggered behavior the report frames as the status quo |
| Flow-stat baseline | P(congestion) from a classifier trained on coarse, 1-in-10 sampled, 250ms-windowed, one-window-delayed utilization features | A NetFlow/sFlow-style view: real, but slow and lossy |
| N-PSI | P(congestion) from a classifier trained on 20ms-windowed "some/full" stall-time fraction + queue trend + softirq latency + burst rate | Sub-100ms kernel-native stall signal |

This is why the flow-stat baseline in `results/RESULTS.md` sometimes
performs *worse* than doing nothing (see docs/results.md) — it is not a
strawman; it is what a real classifier trained on real NetFlow/sFlow-grade
features actually does when the queue fills up in under 20ms. That failure
mode is itself evidence for the report's central claim, not a bug.

## Why lead time is measured two ways

`npsi/eval/metrics.py::prediction_lead_times` computes "how many seconds
before the queue actually crossed the congestion threshold did the alarm
first fire." Feeding this the model's own *closed-loop* trajectory is
subtly wrong once the controller is good enough to prevent the congestion
event it would have predicted — there is nothing left to measure a lead time
against. So the project reports both:

- **Open-loop** (`npsi/ml/train.py`): no controller action at all, pure
  prediction quality against the untouched ground-truth trace. This is
  where "N-PSI predicts ~100ms ahead of overflow" is measured.
- **Closed-loop** (`npsi/sim/sim_e2e.py`): the controller acts on its own
  signal, and the headline metrics are the *outcome* — packets dropped,
  throughput, queueing delay — which is what actually matters operationally
  and is unaffected by the self-referential lead-time problem.

## Known simplifications (and why they're acceptable at this scope)

- **Static, known topology instead of LLDP discovery.** The Ryu app
  (`npsi/control/ryu_app.py`) reads a pre-computed `topology.json` rather
  than discovering the fabric. The testbed topology is fixed and known at
  build time, so this trades away nothing the evaluation needs; full
  discovery is listed as a stretch item.
- **Destination-IP flow matching, not full 5-tuple.** The controller reroutes
  /throttles at the granularity of "traffic heading toward the incast
  receiver," matching how Hedera/CONGA operate at flow/path granularity
  rather than per-packet.
- **`tc`/sysfs polling for periodic backlog, not raw BPF struct reads.** See
  the docstring in `npsi/telemetry/ebpf_probes.py` — `struct Qdisc` field
  layout is not a stable kernel ABI, and a hardcoded-offset BPF read is
  exactly the kind of code that looks sophisticated and then silently
  breaks (or reads garbage) on a different kernel version. The genuinely
  eBPF-measured signal (kprobe/kretprobe stall timing on `tcp_sendmsg` and
  `dev_queue_xmit`) is the part that actually needs to be eBPF; periodic
  backlog depth doesn't.
