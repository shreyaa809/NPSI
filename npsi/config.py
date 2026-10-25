"""
Shared constants for the simulation pipeline. Centralized so `ml/train.py`
(open-loop model training/evaluation) and `sim/sim_e2e.py` (closed-loop
comparison) can never drift apart on window sizes or thresholds -- they must
agree, since sim_e2e.py loads models trained under these exact settings.
"""

DT_S = 0.005  # simulator tick resolution, seconds
NPSI_WINDOW_S = 0.02  # N-PSI feature window: fine-grained, sub-100ms
FLOW_WINDOW_S = 0.25  # flow-stat baseline window: coarse, NetFlow/sFlow-like
HORIZON_S = 0.2  # "will the queue exceed threshold in the next 200ms?"
CONGESTION_THRESHOLD = 0.8  # backlog/capacity ratio counted as "actually congested" (ground truth for labeling/eval)
DECISION_THRESHOLD = 0.5  # P(congestion) cutoff that triggers a controller action for the ML-driven variants
REACTIVE_TRIGGER_THRESHOLD = 0.95  # reactive-TCP-equivalent: only acts once the queue is essentially already full
REACTIVE_RELEASE_THRESHOLD = 0.6
VICTIM_LINK = "leaf1-spine1"  # the link the reference incast scenario targets
