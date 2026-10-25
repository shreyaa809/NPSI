# N-PSI: Network Pressure Stall Information

Predictive, eBPF-driven congestion control for SDN data-center fabrics.
Full project brief: `CN_Review1_Report.pdf`. Build backlog: `FEATURES.md`.
Design rationale: `docs/architecture.md`.

Congestion control today is reactive — TCP backs off only after a packet is
already lost or RTT is already inflated. Flow-level monitoring (NetFlow/
sFlow) samples too coarsely to see micro-bursts that fill a shallow
data-center switch buffer in a few milliseconds. N-PSI borrows Linux's
Pressure Stall Information idea (kernel-native "share of time spent
stalled," used today for CPU/memory/IO) and applies it to network queues via
eBPF, then feeds a lightweight classifier's prediction into an SDN
controller (Ryu) to reroute or throttle traffic *before* loss occurs.

## Two ways to run this

**1. Windows-runnable simulation demo (works right now, no setup beyond
Python):** a fluid queue model stands in for Mininet+OVS+real kernel, with
the exact same feature engineering, ML models, and decision logic that the
real deployment uses. This is what produced `results/RESULTS.md`.

**2. Real deployment (Linux/WSL2 only):** genuine eBPF kprobes, Mininet +
Open vSwitch, and a Ryu OpenFlow controller. See `docs/linux_deployment.md`.

Both share the same `npsi/` package — see `docs/architecture.md` for why
this split exists and how the two stay in sync.

## Quickstart (simulation demo, ~2 minutes)

```bash
python -m venv .venv
source .venv/Scripts/activate      # Linux/Mac: .venv/bin/activate
pip install -r requirements.txt

# 1. Train the N-PSI and flow-stat-baseline classifiers on simulated telemetry
python -m npsi.ml.train --duration 300 --seed 7 --out-dir models

# 2. Run the closed-loop comparison: reactive vs flow-stat baseline vs N-PSI,
#    on a held-out scenario (different seed from training)
python -m npsi.sim.sim_e2e --duration 120 --seed 42 --models-dir models --out-dir results

# 3. Generate the results table + plots
python -m npsi.eval.report --models-dir models --results-dir results

# Run the test suite
pytest tests/ -v
```

Then read `results/RESULTS.md` (or `docs/results.md` for the interpreted
version with caveats) and look at `results/comparison_plot.png`.

## What the demo actually shows

On the reference incast scenario (many-to-one burst traffic against a
shallow-buffered uplink, held-out from training):

- **N-PSI** predicts imminent congestion ~104ms ahead of actual overflow
  (100% of congestion events caught, 0.37% false-positive rate), and its
  proactive reroute/throttle **eliminates packet loss entirely** in the
  closed-loop run.
- A **fair flow-stat baseline** (coarse, 1-in-10 sampled, 250ms-windowed,
  delayed features — deliberately modeled after NetFlow/sFlow) catches only
  1 of 14 events, and its closed-loop performance is *worse* than a plain
  reactive controller — not because it's a strawman, but because acting on
  a stale, coarse signal at the wrong moment actively hurts.

Full discussion, including honest caveats about sample size and what's
simulation vs. real-kernel-validated, in `docs/results.md`.

## Repository layout

```
npsi/
  telemetry/   shared event schema + simulator backend + real eBPF backend
  features/    N-PSI "some/full" score, supporting features, flow-stat baseline
  ml/          labeling, XGBoost + LSTM models, training CLI, inference
  control/     decision engine, Ryu app, prediction service, topology/paths
  sim/         fluid network model + Windows-runnable closed-loop demo
  testbed/     Mininet topology, traffic generator, real-testbed orchestration
  eval/        metrics (lead time, FPR, loss reduction) + report generation
tests/         pytest suite for the backend-agnostic core
docs/          architecture rationale, Linux deployment guide, results writeup
```

## Status

See `FEATURES.md` for the live build checklist. Core pipeline (telemetry →
features → ML → decision → simulated control loop) is implemented, tested,
and produces the results above.

The real eBPF/Mininet/SDN-controller path has also been brought up and
validated live under WSL2 Ubuntu 22.04 — real eBPF bindings, a real
OpenFlow controller (built on **os-ken**, the maintained fork of Ryu; see
`docs/linux_deployment.md` §0 for why), and the actual 21-host project
topology with 20/20 host↔receiver connectivity confirmed. Two real bugs
were found and fixed along the way (an L2 loop needing proactive static
routing, and a controller/topology load-ordering race) — both documented in
`docs/linux_deployment.md` rather than glossed over. A full real-traffic
loss/throughput run is blocked by a WSL2-kernel-specific gap (no `htb`
queueing discipline compiled in) and works unmodified on a standard Linux
kernel; see `docs/linux_deployment.md` §5 for the two ways to close that
last gap.
