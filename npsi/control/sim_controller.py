"""
Simulation-mode controller: applies DecisionEngine output directly to a
NetworkModel (set_reroute/set_throttle) instead of installing OpenFlow
rules. This is the closed loop for the Windows-runnable demo -- structurally
identical to what `ryu_app.py` + `prediction_service.py` do against a real
Mininet/OVS + Ryu deployment.
"""
from __future__ import annotations

from collections.abc import Callable

from npsi.control.decision import Action, DecisionEngine
from npsi.sim.network_model import NetworkModel

SignalFn = Callable[[object], float]


class SimController:
    """`signal_fn` maps a feature vector (N-PSI or flow-baseline) to the
    scalar score the DecisionEngine acts on. Passing `signal_fn=reactive_signal`
    (see below) reproduces the reactive-TCP baseline with the same engine."""

    def __init__(self, model: NetworkModel, decision_engine: DecisionEngine, signal_fn: SignalFn):
        self.model = model
        self.decision_engine = decision_engine
        self.signal_fn = signal_fn
        self.actions_log: list[Action] = []

    def on_features(self, feature_vectors: list) -> None:
        for fv in feature_vectors:
            score = self.signal_fn(fv)
            action = self.decision_engine.decide(fv.link_id, score)
            if fv.link_id in self.model.links:
                self.model.set_reroute(fv.link_id, action.reroute_fraction)
                self.model.set_throttle(fv.link_id, action.throttle_factor)
            self.actions_log.append(action)


def reactive_signal(fv) -> float:
    """Reactive-TCP-equivalent: only "sees" congestion once the queue is
    already backed up -- the classic loss/RTT-based reactive control loop,
    with no prediction at all."""
    return fv.backlog_ratio_last


def make_model_signal(model_predict_proba) -> SignalFn:
    """Wraps a trained model's predict_proba into a per-feature-vector signal_fn."""

    def _signal(fv) -> float:
        return float(model_predict_proba([fv.to_array()])[0])

    return _signal
