"""
Turns a congestion "score" in [0,1] into a reroute/throttle action, with
hysteresis (different trigger vs release thresholds) and a cooldown so the
controller doesn't flap flows back and forth every window.

Deliberately signal-agnostic: the exact same engine drives all three
evaluated variants --
  - reactive baseline: score = current backlog/capacity ratio (i.e. it only
    "sees" congestion once the queue is already there -- classic loss/RTT
    style reactive control)
  - flow-stat baseline: score = P(congestion) from the coarse flow-stat classifier
  - N-PSI: score = P(congestion) from the N-PSI classifier
This keeps the comparison to "how good is the signal", not "how good is the
controller", which is the actual claim of the project.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class DecisionConfig:
    trigger_threshold: float = 0.5
    release_threshold: float = 0.3  # must be < trigger_threshold (hysteresis band)
    cooldown_ticks: int = 10  # minimum windows an action is held before it can be released
    reroute_fraction: float = 0.6
    throttle_factor: float = 0.5

    def __post_init__(self):
        if self.release_threshold >= self.trigger_threshold:
            raise ValueError("release_threshold must be lower than trigger_threshold")


@dataclass
class _LinkState:
    active: bool = False
    ticks_in_state: int = 0


@dataclass(frozen=True, slots=True)
class Action:
    link_id: str
    reroute_fraction: float
    throttle_factor: float
    active: bool


class DecisionEngine:
    def __init__(self, config: DecisionConfig | None = None):
        self.config = config or DecisionConfig()
        self._state: dict[str, _LinkState] = {}

    def decide(self, link_id: str, score: float) -> Action:
        cfg = self.config
        st = self._state.setdefault(link_id, _LinkState())
        st.ticks_in_state += 1

        if not st.active:
            if score >= cfg.trigger_threshold:
                st.active = True
                st.ticks_in_state = 0
                return Action(link_id, cfg.reroute_fraction, cfg.throttle_factor, True)
            return Action(link_id, 0.0, 1.0, False)

        if st.ticks_in_state >= cfg.cooldown_ticks and score < cfg.release_threshold:
            st.active = False
            st.ticks_in_state = 0
            return Action(link_id, 0.0, 1.0, False)
        return Action(link_id, cfg.reroute_fraction, cfg.throttle_factor, True)
