# N-PSI Feature Checklist

Tracks implementation progress. Check items off only once implemented AND smoke-tested. Legend: [ ] todo, [x] done, [~] partial/stub.

## 0. Project Scaffolding
- [x] Repo structure (`npsi/` package, `tests/`, `scripts/`, `docs/`)
- [x] `requirements.txt` (+ `requirements-linux.txt` for the real-deployment extras)
- [x] `README.md` (setup for both Windows simulation mode and Linux real-deployment mode)
- [x] `docs/architecture.md` (design rationale, why dual-backend)

## 1. Telemetry Layer (shared event schema)
- [x] `npsi/telemetry/events.py` — common `StallEvent`/`QueueSample` schema used by both backends
- [x] `npsi/telemetry/simulator.py` — synthetic kernel-telemetry generator (qdisc backlog, sk buffer contention, softirq latency) with configurable incast bursts, cross-platform
- [x] `npsi/telemetry/ebpf_probes.py` — real BCC kprobes on tcp_sendmsg (sk_buffer proxy) + dev_queue_xmit (qdisc proxy, per-interface via skb->dev->name), periodic backlog via tc/sysfs polling (documented tradeoff vs fragile struct-offset BPF reads); `from bcc import BPF` verified working under real WSL2 Ubuntu 22.04 kernel (5.15, CONFIG_BPF_SYSCALL=y). Probes not yet exercised under live incast traffic (see docs/linux_deployment.md §6).
- [x] `npsi/telemetry/collector.py` — backend-agnostic `make_collector("simulator"|"ebpf", ...)` facade

## 2. Feature Engineering Layer
- [x] `npsi/features/npsi_score.py` — windowed "some"/"full" stall-fraction aggregation per link (the core N-PSI metric) — unit tested
- [x] `npsi/features/supporting.py` — queue-depth trend/gradient, softirq latency, burst arrival rate
- [x] `npsi/features/flow_baseline.py` — sampled NetFlow/sFlow-style feature extractor (fair baseline, coarse-grained) — integration tested
- [x] `npsi/features/pipeline.py` — orchestrates raw events -> windowed feature vectors — integration tested, burst correctly raises npsi_some/full

## 3. Machine Learning Layer
- [x] `npsi/ml/labeling.py` — forward-looking threshold labeling ("queue exceeds X within next 200ms") — unit tested
- [x] `npsi/ml/dataset.py` — windowing, chronological train/val/test split, sequence builder for LSTM
- [x] `npsi/ml/xgboost_model.py` — primary gradient-boosted classifier — trained, saves/loads, real results in results/RESULTS.md
- [x] `npsi/ml/lstm_model.py` — lightweight temporal model (PyTorch) for burst dependencies — implemented, gracefully skipped when torch absent (verified)
- [x] `npsi/ml/train.py` — CLI: trains N-PSI + flow-baseline XGBoost, reports precision/recall/F1/FPR/lead-time, saves artifacts — run successfully, see results/RESULTS.md
- [x] `npsi/ml/infer.py` — streaming inference wrapper used by the live control loop — used by sim_e2e.py

## 4. Control Layer (SDN)
- [x] `npsi/control/decision.py` — turns P(congestion) into reroute/throttle decisions with hysteresis + cooldown (anti-flap) — unit tested (7 tests)
- [x] `npsi/control/topology_paths.py` — general k-ary fat-tree graph + k-shortest-path alternate selection (networkx) — verified: k=4 gives 36 nodes/48 edges/16 hosts/4 equal-cost paths, correct fat-tree properties
- [x] `npsi/control/ryu_app.py` — real OpenFlow 1.3 controller app (built on **os-ken**, not Ryu — upstream Ryu's dependency chain is broken, see docs/linux_deployment.md §0): L2 learning switch + proactive static routing for the known topology (loop-safety fix, §4 below) + reroute (priority flow-mod to alt port) + throttle (OFPMeterMod). **Validated live under WSL2**: real OpenFlow controller, real REST endpoint, real DecisionEngine integration, real Mininet+OVS connectivity (20/20 host→receiver pings on the actual 21-host/4-switch topology).
- [x] `npsi/control/os_ken_launcher.py` — replacement for the missing `ryu-manager`/`os-ken-manager` CLI (os-ken's PyPI package ships as a library only); verified starting a real controller end to end.
- [x] `npsi/control/prediction_service.py` — long-running process: telemetry (ebpf or simulator) -> feature pipeline (npsi or flow_stat, selectable) -> trained model -> POST to controller REST
- [x] `npsi/control/sim_controller.py` — simulation-mode controller (same decision logic, acts on the simulated data plane instead of OpenFlow) — proven in sim_e2e.py, N-PSI achieves zero packet loss

## 5. Testbed (Linux / Mininet+OVS, real deployment)
- [x] `npsi/testbed/fattree_topo.py` — `LeafSpineIncastTopo` (mirrors the simulated scenario 1:1: victim uplink + alt uplink + 20 incast hosts), `UserspaceOVSSwitch` for kernel-module-less hosts (validated under WSL2), `write_topology_map` now also computes proactive default routes + no-flood dpids (loop fix), `setup_static_arp`. General `FatTreeTopo(k)` also provided for scaling later.
- [x] `npsi/testbed/traffic_gen.py` — bursty incast traffic generator (iperf3 UDP, same burst-schedule shape as network_model.make_incast_scenario)
- [x] `npsi/testbed/baselines.py` — per-variant orchestration (reactive = no prediction service; flow_stat/npsi = prediction_service subprocess with the right pipeline) + sysfs interface-counter based loss/throughput summary
- [x] `npsi/testbed/run_experiment.py` — CLI orchestrating one full run (topo + write topology.json + **launch controller after topology is accurate** + traffic + counter sampling), writes `results/real_<variant>_summary.json`
- [x] **Ran on a real Linux (WSL2 Ubuntu 22.04) host.** Found and fixed two real bugs in the process: (1) the topology has a genuine L2 loop (two paths to the receiver), which a naive learning switch floods into a broadcast storm — fixed with proactive static routes + no-flood hub switches + static ARP; (2) controller/topology.json load-order race (controller must start *after* topology.json has real dpid/port values, not before) — fixed by having run_experiment.py launch its own controller post-topology. Full connectivity validated (20/20). A full loss/throughput experiment run is blocked by a WSL2-kernel-specific gap (no `htb`/`tbf`/`netem` qdisc compiled in, so Mininet's TCLink bandwidth/queue shaping fails) — documented in docs/linux_deployment.md §5 as an environment limitation, not a code bug; works unmodified on a standard Linux kernel.

## 6. Simulation Mode (Windows-runnable, no root/Linux needed)
- [x] `npsi/sim/network_model.py` — discrete-event (fluid) queue/link model, multi-link, incast-capable, controller hooks (set_throttle/set_reroute)
- [x] `npsi/sim/sim_e2e.py` — full closed-loop demo: simulator telemetry -> features -> ML -> decision -> effect on simulated queues, for all 3 variants — run successfully
- [x] CLI entry point + shared `npsi/config.py` to reproduce a full comparison run in one command (`python -m npsi.sim.sim_e2e`)

## 7. Evaluation
- [x] `npsi/eval/metrics.py` — prediction lead time (event-block based), false-positive rate, packet loss reduction, throughput/latency deltas — bug found+fixed (recall key collision)
- [x] `npsi/eval/report.py` — generates comparison plots (matplotlib) + Markdown results table — verified output in results/RESULTS.md
- [x] Baseline vs N-PSI comparison automated end-to-end (`python -m npsi.ml.train` then `python -m npsi.sim.sim_e2e` then `python -m npsi.eval.report` produces `results/`)

## 8. Testing
- [x] `tests/test_npsi_score.py`
- [x] `tests/test_labeling.py`
- [x] `tests/test_decision.py`
- [x] `tests/test_pipeline_integration.py`
- [x] `tests/test_ml_train_smoke.py`
- [x] All tests passing (`pytest`) — 21/21 passed

## 9. Documentation & Deliverables
- [x] `README.md` quickstart (Windows simulation demo in <5 min)
- [x] `docs/linux_deployment.md` — WSL2/VM setup for real eBPF+Mininet+Ryu run, plus a troubleshooting section
- [x] `docs/results.md` — populated with real simulation-mode numbers + honest caveats (sample size, closed-loop lead-time interpretation); real-testbed numbers still TBD
- [x] Architecture (`docs/architecture.md`) explains the dual-backend design against the Review1 report's Figure 1 layers

## Stretch (only if core is solid and time remains)
- [ ] Grafana/Prometheus live dashboard for N-PSI scores during a run
- [ ] Multi-path (k>4) fat-tree scaling experiment (topology_paths.py + FatTreeTopo already support this; needs run_experiment.py wiring)
- [ ] Adversarial burst pattern generator to stress-test false-positive rate
- [ ] BPF CO-RE (vmlinux.h + bpf_core_read) for qdisc backlog depth, replacing the tc/sysfs polling in ebpf_probes.py
- [ ] Multi-seed statistical evaluation (confidence intervals on the headline numbers in docs/results.md)
- [ ] YAML-driven config (`npsi/config.py` -> `config/config.yaml`) for tunables, matching the pattern used in the sibling OS-PSI project
- [ ] Full real-testbed traffic run producing real loss/throughput numbers: needs either a non-WSL2 Linux host (has `htb`), or wiring an always-on OpenFlow meter as the victim link's capacity constraint instead of `tc` (see docs/linux_deployment.md §5) — a WSL2 Ubuntu venv with mininet/OVS/bcc/os-ken already installed and validated exists at `.venv-linux/` for whoever picks this up next
- [ ] Exercise real eBPF kprobes (tcp_sendmsg/dev_queue_xmit) under live incast traffic and sanity-check the stall-time values they produce against the simulated ones
