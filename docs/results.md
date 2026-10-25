# Results

Raw tables: `results/RESULTS.md` (regenerate with the three commands below).
Plots: `results/comparison_plot.png`. Everything here is from the
Windows-runnable simulation backend (`npsi/sim/`); real-testbed numbers land
in `results/real_<variant>_summary.json` once run per docs/linux_deployment.md,
and are not yet included below.

## Reproduce

```bash
python -m npsi.ml.train --duration 300 --seed 7 --out-dir models
python -m npsi.sim.sim_e2e --duration 120 --seed 42 --models-dir models --out-dir results
python -m npsi.eval.report --models-dir models --results-dir results
```

Training and evaluation use **different seeds** (7 vs. 42) deliberately —
the comparison run is on a burst schedule the model never saw during
training, so the numbers below are held-out, not training-set performance.

## Headline numbers

**Open-loop** (pure prediction quality, no controller action, 300s of
simulated incast traffic, last 20% chronologically held out as test):

| Model | Precision | Recall | F1 | FPR | Events caught | Mean lead time |
|---|---|---|---|---|---|---|
| N-PSI (XGBoost) | 0.96 | 0.77 | 0.85 | 0.37% | **14/14 (100%)** | **104 ms** |
| Flow-stat baseline (XGBoost) | 1.00 | 0.07 | 0.13 | 0.00% | 1/14 (7%) | 250 ms |

**Closed-loop** (controller acts on its own signal, 120s held-out run):

| Variant | Packets dropped | Loss rate | Throughput | Mean queueing delay |
|---|---|---|---|---|
| Reactive TCP (acts only near-full buffer) | 155.6 | 0.35% | 372 pps | 7.3 ms |
| Flow-stat baseline | 244.1 | 1.42% | 142 pps | 1.6 ms |
| **N-PSI** | **0.0** | **0.00%** | 320 pps | **0.53 ms** |

## Reading these honestly

**N-PSI eliminates loss entirely on this scenario, with a fair, coarser
baseline that does worse than doing nothing.** That second half is the more
interesting finding and deserves explanation, not just a footnote: the
flow-stat baseline's classifier is trained on 250ms-windowed, 1-in-10
sampled, one-window-delayed utilization features — by construction, exactly
what NetFlow/sFlow gives you. Bursts in this scenario ramp from calm to
overflow in well under 20ms. A signal that only updates every 250ms
structurally cannot catch that, and it shows: precision 1.00 / recall 0.07
means the baseline almost never fires, and the one time it does, it's
committing capacity (throttling, rerouting) at the wrong moment relative to
the actual burst — which is *why* it ends up worse than a controller that
does nothing predictive at all. This is not a strawman baseline tuned to
lose; it is what the report's own literature-survey claim (flow-level
telemetry "is too slow and too lossy to see micro-bursts") looks like when
you actually build and measure it.

**Caveats that matter for how much weight to put on these numbers:**

- **Small event count.** 14 congestion events in the open-loop test split,
  120s / a handful of bursts in the closed-loop run. The qualitative story
  (N-PSI catches nearly everything with a real lead time; the coarse
  baseline catches almost nothing) is robust across the seeds tried during
  development, but single-digit-to-low-double-digit event counts mean the
  precise percentages (e.g. "100% event recall") should be read as "very
  high, on this scenario" rather than a tight statistical estimate. Running
  more seeds and reporting a confidence interval is the natural next step
  before treating these as publication-grade numbers.
- **Closed-loop lead time/recall are not directly comparable across
  variants** for the reasons in `docs/architecture.md` — a controller that
  prevents congestion has nothing left to measure a lead time against in
  its own trace. Use the open-loop table for prediction-quality comparisons
  and the closed-loop table for outcome comparisons; don't cross-read them.
- **One scenario topology.** A two-uplink leaf-spine with one incast victim
  link. The qualitative mechanism (fine-grained stall time beats coarse
  sampling for micro-burst prediction) should generalize, but the specific
  numbers are scenario-specific until validated on the real Mininet testbed
  and/or a wider topology (see FEATURES.md stretch items).
- **Simulation, not yet real kernel/switches.** These numbers come from the
  fluid queue model, not real eBPF + OVS + Ryu. They are the right numbers
  to report as "the system works end to end and the mechanism behaves as
  predicted"; docs/linux_deployment.md is how to additionally validate
  against a real kernel before a later review.
