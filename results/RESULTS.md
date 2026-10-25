# N-PSI Evaluation Results

## Open-loop prediction quality (no controller action; ml/train.py)

| Model | Precision | Recall | F1 | FPR | Events detected | Mean lead time (s) |
|---|---|---|---|---|---|---|
| N-PSI (XGBoost) | 0.957 | 0.768 | 0.852 | 0.0037 | 100% of 14 | 104 ms |
| Flow-stat baseline (XGBoost) | 1.000 | 0.067 | 0.125 | 0.0000 | 7% of 14 | 250 ms |

## Closed-loop system outcome (controller acts on its own signal; sim/sim_e2e.py)

| Variant | Packets dropped | Loss rate | Loss reduction vs reactive | Throughput (pps) | Mean queueing delay (s) | Controller activations |
|---|---|---|---|---|---|---|
| Reactive TCP | 155.6 | 0.3473% | +0.0% | 372.0 | 0.00732 | 20 |
| Flow-stat baseline | 244.1 | 1.4151% | -56.9% | 141.7 | 0.00160 | 1 |
| N-PSI | 0.0 | 0.0000% | +100.0% | 320.4 | 0.00053 | 45 |

*Interpretation note*: closed-loop lead-time/recall figures are measured on each variant's own (already-mitigated) trajectory, so a fully successful controller can show zero observed congestion events simply because it prevented them -- the correct place to read prediction quality in isolation is the open-loop table above. The closed-loop table's headline evidence is the outcome: packet loss, throughput, and queueing delay actually experienced by the network under each control strategy.
