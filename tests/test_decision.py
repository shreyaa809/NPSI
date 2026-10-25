import pytest

from npsi.control.decision import DecisionConfig, DecisionEngine


def test_no_action_below_trigger():
    engine = DecisionEngine(DecisionConfig(trigger_threshold=0.5, release_threshold=0.3))
    action = engine.decide("link1", 0.2)
    assert not action.active
    assert action.throttle_factor == 1.0
    assert action.reroute_fraction == 0.0


def test_action_triggers_above_threshold():
    engine = DecisionEngine(DecisionConfig(trigger_threshold=0.5, release_threshold=0.3,
                                            reroute_fraction=0.6, throttle_factor=0.5))
    action = engine.decide("link1", 0.9)
    assert action.active
    assert action.reroute_fraction == 0.6
    assert action.throttle_factor == 0.5


def test_hysteresis_holds_action_between_thresholds():
    engine = DecisionEngine(DecisionConfig(trigger_threshold=0.5, release_threshold=0.3, cooldown_ticks=1))
    engine.decide("link1", 0.9)  # trigger
    action = engine.decide("link1", 0.4)  # between release(0.3) and trigger(0.5) -- should stay active
    assert action.active


def test_cooldown_prevents_immediate_release():
    engine = DecisionEngine(DecisionConfig(trigger_threshold=0.5, release_threshold=0.3, cooldown_ticks=5))
    engine.decide("link1", 0.9)  # tick 0: trigger, ticks_in_state resets to 0
    for _ in range(3):
        action = engine.decide("link1", 0.1)  # below release threshold but still in cooldown
        assert action.active


def test_releases_after_cooldown_and_below_release_threshold():
    engine = DecisionEngine(DecisionConfig(trigger_threshold=0.5, release_threshold=0.3, cooldown_ticks=2))
    engine.decide("link1", 0.9)
    engine.decide("link1", 0.1)
    engine.decide("link1", 0.1)
    action = engine.decide("link1", 0.1)
    assert not action.active


def test_links_are_independent():
    engine = DecisionEngine(DecisionConfig(trigger_threshold=0.5, release_threshold=0.3))
    engine.decide("linkA", 0.9)
    action_b = engine.decide("linkB", 0.1)
    assert not action_b.active


def test_invalid_config_raises():
    with pytest.raises(ValueError):
        DecisionConfig(trigger_threshold=0.3, release_threshold=0.5)
