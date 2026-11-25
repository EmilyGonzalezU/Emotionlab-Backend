"""Synthetic unit cases for the offline screening utility only.

These fixtures are not EmotionLab participant data and are never loaded by the
application. They test guards and walk-forward mechanics, not forecast validity.
"""

from services.tracking_forecast import evaluate_candidates


def evaluate(values, *, identity=True, interval=True):
    return evaluate_candidates(
        values,
        participant_identity_verified=identity,
        interval_scale_assumption_confirmed=interval,
    )


def test_forecast_is_blocked_without_verified_same_participant_history():
    result = evaluate([1, 2, 3, 4, 5, 6, 7, 8, 9, 9], identity=False)
    assert result == {
        "status": "unavailable",
        "reason": "participant_identity_unverified",
    }


def test_ordinal_values_block_numeric_candidates_until_interval_assumption_is_confirmed():
    result = evaluate([1, 2, 3, 4, 5, 6, 7, 8, 9, 9], interval=False)
    assert result["status"] == "unavailable"
    assert result["reason"] == "ordinal_scale_intervals_unconfirmed"


def test_prediction_is_unavailable_with_too_few_temporal_validation_origins():
    result = evaluate([2, 3, 4, 5, 6, 7, 8, 9, 8])
    assert result["status"] == "insufficient_data"
    assert result["minimum_observations"] == 10


def test_candidate_must_strictly_beat_persistence_and_is_never_marked_production_ready():
    result = evaluate([1, 2, 3, 4, 5, 6, 7, 8, 9, 9])
    assert result["status"] == "candidate_only"
    assert result["reason"] == "offline_validation_only_not_enabled_in_production"
    assert result["method"] == "linear_trend"
    assert result["mae"]["linear_trend"] < result["mae"]["persistence"]
    assert result["prediction"] == 9
    assert result["interval"] is None


def test_no_candidate_beats_persistence_for_constant_series():
    result = evaluate([5] * 10)
    assert result["status"] == "unavailable"
    assert result["reason"] == "no_candidate_beats_persistence"
    assert result["mae"]["persistence"] == 0


def test_temporal_validation_uses_only_prior_observations():
    first = evaluate([1, 2, 3, 4, 5, 6, 7, 8, 9, 9])
    changed_future = evaluate([1, 2, 3, 4, 5, 6, 9, 1, 9, 1])

    first_fold = next(fold for fold in first["fold_audit"] if fold["test_index"] == 6)
    changed_fold = next(fold for fold in changed_future["fold_audit"] if fold["test_index"] == 6)
    assert first_fold["training_end_exclusive"] == 6
    assert first_fold["predictions"] == changed_fold["predictions"]


def test_invalid_missing_or_out_of_range_values_are_never_filled_or_clamped_as_inputs():
    for values in ([1, 2, None, 4, 5, 6, 7, 8, 9, 9], [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]):
        result = evaluate(values)
        assert result["status"] == "unavailable"
        assert result["reason"] == "invalid_or_out_of_range_observation"
