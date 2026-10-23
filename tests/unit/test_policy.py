import random

import pytest

from app.domain import fingerprint, is_current, next_leg

IMAGE = [dict(provider="alpha", max_ref_images=4, skip_on_moderation=False),
         dict(provider="beta", max_ref_images=10, skip_on_moderation=False)]
VIDEO = [dict(provider="beta", max_ref_images=2, skip_on_moderation=False),
         dict(provider="alpha", max_ref_images=1, skip_on_moderation=True)]


@pytest.mark.parametrize("refs,expected", [(0, 0), (1, 0), (4, 0), (5, 1), (10, 1), (11, None)])
def test_initial_reference_filter(refs, expected):
    assert next_leg(IMAGE, -1, refs, None) == expected


@pytest.mark.parametrize("error,expected", [("technical", 1), ("moderation", None), ("payload", None)])
def test_video_failure_policy(error, expected):
    assert next_leg(VIDEO, 0, 1, error) == expected


def test_moderation_can_fallback_when_skip_flag_is_false():
    assert next_leg(IMAGE, 0, 4, "moderation") == 1


def test_skipped_leg_does_not_reset_moderation_condition():
    legs = [dict(max_ref_images=10, skip_on_moderation=False),
            dict(max_ref_images=10, skip_on_moderation=True),
            dict(max_ref_images=10, skip_on_moderation=True),
            dict(max_ref_images=10, skip_on_moderation=False)]
    assert next_leg(legs, 0, 1, "moderation") == 3


def test_reference_capacity_still_applies_after_failure():
    assert next_leg(VIDEO, 0, 2, "technical") is None


@pytest.mark.parametrize("error", [None, "technical", "moderation", "payload"])
def test_exhausted_chain(error):
    assert next_leg(IMAGE, 1, 0, error) is None


def test_fingerprint_canonicalizes_object_keys_not_array_order():
    assert fingerprint({"a": 1, "b": 2}) == fingerprint({"b": 2, "a": 1})
    assert fingerprint([1, 2]) != fingerprint([2, 1])


@pytest.mark.parametrize("task_status,attempt_status,index,expected", [
    ("active", "queued", 0, True), ("active", "sending", 0, True),
    ("active", "waiting", 0, True), ("active", "failed", 0, False),
    ("active", "timed_out", 0, False), ("completed", "completed", 0, False),
    ("failed", "waiting", 0, False), ("active", "waiting", 1, False),
])
def test_only_current_live_attempt_can_transition(task_status, attempt_status, index, expected):
    assert is_current({"status": task_status, "current_index": 0},
                      {"status": attempt_status, "leg_index": index}) is expected


def test_random_selection_matches_simple_reference_implementation():
    rng = random.Random(1847)
    for _ in range(1000):
        legs = [dict(max_ref_images=rng.randrange(11), skip_on_moderation=bool(rng.randrange(2)))
                for _ in range(rng.randrange(1, 8))]
        after, refs, error = rng.randrange(-1, len(legs)), rng.randrange(12), rng.choice([None, "technical", "moderation", "payload"])
        eligible = [i for i in range(after+1, len(legs)) if legs[i]["max_ref_images"] >= refs
                    and not (error == "moderation" and legs[i]["skip_on_moderation"])]
        expected = eligible[0] if eligible and error != "payload" else None
        assert next_leg(legs, after, refs, error) == expected
