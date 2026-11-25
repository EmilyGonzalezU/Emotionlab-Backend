"""Offline candidate evaluation for one verified participant/dimension/phase.

This module is deliberately not wired into production tracking responses.
Current EmotionLab capture stores ``participant_id=__PENDING__`` and the Unity
``idParticipante`` is a capture correlation key, not a student identity. The
response scale is ordinal; numeric candidates require an explicit interval
scale assumption before they can be evaluated with MAE.
"""

from __future__ import annotations

import math
from typing import Any, Callable, Mapping, Sequence


SCALE_MIN = 1.0
SCALE_MAX = 9.0
MIN_INITIAL_TRAINING_POINTS = 5
MIN_VALIDATION_ORIGINS = 5
MIN_HISTORY_FOR_EVALUATION = (
    MIN_INITIAL_TRAINING_POINTS + MIN_VALIDATION_ORIGINS
)


def _persistence(values: Sequence[float]) -> float:
    return float(values[-1])


def _moving_average_3(values: Sequence[float]) -> float:
    window = values[-3:]
    return sum(window) / len(window)


def _linear_trend(values: Sequence[float]) -> float:
    count = len(values)
    mean_x = (count - 1) / 2
    mean_y = sum(values) / count
    denominator = sum((index - mean_x) ** 2 for index in range(count))
    slope = sum(
        (index - mean_x) * (value - mean_y)
        for index, value in enumerate(values)
    ) / denominator
    return mean_y + slope * (count - mean_x)


_CANDIDATES: Mapping[str, Callable[[Sequence[float]], float]] = {
    "moving_average_3": _moving_average_3,
    "linear_trend": _linear_trend,
}


def _valid_series(values: Sequence[Any]) -> list[float] | None:
    if not isinstance(values, (list, tuple)):
        return None
    result: list[float] = []
    for value in values:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        numeric = float(value)
        if not math.isfinite(numeric) or not (SCALE_MIN <= numeric <= SCALE_MAX):
            return None
        result.append(numeric)
    return result


def evaluate_candidates(
    values: Sequence[Any],
    *,
    participant_identity_verified: bool,
    interval_scale_assumption_confirmed: bool,
) -> dict[str, Any]:
    """Compare simple candidates with persistence using expanding-origin CV.

    The first five points in each fold are training-only; each later point is
    predicted once from strictly earlier observations. A candidate is selected
    only when its pooled MAE is strictly lower than persistence. This is an
    offline screening rule, not sufficient validation for production use.
    """

    if not participant_identity_verified:
        return {"status": "unavailable", "reason": "participant_identity_unverified"}
    if not interval_scale_assumption_confirmed:
        return {"status": "unavailable", "reason": "ordinal_scale_intervals_unconfirmed"}

    series = _valid_series(values)
    if series is None:
        return {"status": "unavailable", "reason": "invalid_or_out_of_range_observation"}
    if len(series) < MIN_HISTORY_FOR_EVALUATION:
        return {
            "status": "insufficient_data",
            "reason": "too_few_temporal_validation_origins",
            "observations_used": len(series),
            "minimum_observations": MIN_HISTORY_FOR_EVALUATION,
        }

    errors: dict[str, list[float]] = {"persistence": []}
    fold_audit = []
    for name in _CANDIDATES:
        errors[name] = []

    for test_index in range(MIN_INITIAL_TRAINING_POINTS, len(series)):
        history = series[:test_index]
        actual = series[test_index]
        predictions = {"persistence": _persistence(history)}
        predictions.update({
            name: method(history)
            for name, method in _CANDIDATES.items()
        })
        bounded = {
            name: max(SCALE_MIN, min(SCALE_MAX, prediction))
            for name, prediction in predictions.items()
        }
        for name, prediction in bounded.items():
            errors[name].append(abs(actual - prediction))
        fold_audit.append({
            "training_end_exclusive": test_index,
            "test_index": test_index,
            "actual": actual,
            "predictions": bounded,
        })

    metrics = {
        name: sum(values_) / len(values_)
        for name, values_ in errors.items()
    }
    baseline_mae = metrics["persistence"]
    winners = [
        name for name in _CANDIDATES
        if metrics[name] < baseline_mae
    ]
    if not winners:
        return {
            "status": "unavailable",
            "reason": "no_candidate_beats_persistence",
            "observations_used": len(series),
            "validation_origins": len(fold_audit),
            "mae": metrics,
            "interval": None,
            "fold_audit": fold_audit,
        }

    best_mae = min(metrics[name] for name in winners)
    best = [name for name in winners if metrics[name] == best_mae]
    if len(best) != 1:
        return {
            "status": "unavailable",
            "reason": "candidate_tie",
            "observations_used": len(series),
            "validation_origins": len(fold_audit),
            "mae": metrics,
            "interval": None,
            "fold_audit": fold_audit,
        }

    method = best[0]
    prediction = _CANDIDATES[method](series)
    return {
        "status": "candidate_only",
        "reason": "offline_validation_only_not_enabled_in_production",
        "method": method,
        "prediction": max(SCALE_MIN, min(SCALE_MAX, prediction)),
        "observations_used": len(series),
        "validation_origins": len(fold_audit),
        "mae": metrics,
        "interval": None,
        "interval_reason": "no_calibrated_temporal_coverage_evidence",
        "fold_audit": fold_audit,
    }
