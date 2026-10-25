from npsi.features.npsi_score import compute_npsi_score


def test_empty_window_is_zero():
    score = compute_npsi_score([], [])
    assert score.some == 0.0
    assert score.full == 0.0


def test_all_calm():
    score = compute_npsi_score([False] * 10, [False] * 10)
    assert score.some == 0.0
    assert score.full == 0.0


def test_all_full():
    score = compute_npsi_score([True] * 10, [True] * 10)
    assert score.some == 1.0
    assert score.full == 1.0


def test_some_but_not_full():
    some = [True, True, False, True, False]
    full = [False, False, False, False, False]
    score = compute_npsi_score(some, full)
    assert score.some == 3 / 5
    assert score.full == 0.0


def test_full_implies_some_in_practice():
    # full stall ticks should always also be counted as "some" stalled ticks
    # when constructed consistently (this test documents the expected usage,
    # not an invariant enforced by compute_npsi_score itself)
    some = [True, True, True]
    full = [False, True, False]
    score = compute_npsi_score(some, full)
    assert score.full <= score.some
