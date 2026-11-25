"""Versioned normalization for newly received inverted Unity scale answers.

Raw Unity records are never mutated. The normalized copy and a per-answer
audit report are stored alongside each ingested record and used only as V8
input when that marker is present.
"""

from __future__ import annotations

from copy import deepcopy
import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping

from services.emotion_model_v8_package import MODEL_DIR


NORMALIZATION_VERSION = "unity-inverted-1-to-9-v1"
_LABEL_PATTERN = re.compile(r"^[0-9]+$")


@lru_cache(maxsize=4)
def _question_map(mapping_path: str) -> dict[tuple[str, str], str]:
    config = json.loads(Path(mapping_path).read_text(encoding="utf-8"))
    return {
        (item["escena"], item["idPregunta"]): item["variable"]
        for item in config["equivalencias"]
    }


def _parse_label(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and _LABEL_PATTERN.fullmatch(value.strip()):
        return int(value.strip())
    return None


def _parse_index(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and _LABEL_PATTERN.fullmatch(value.strip()):
        return int(value.strip())
    return None


def annotate_unity_record(
    record: Mapping[str, Any], *, model_dir: Path | str = MODEL_DIR
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return a normalized copy and audit metadata, preserving all raw fields.

    Only the exact (scene, question id) pairs listed in the frozen V8 mapping
    are considered. Invalid or inconsistent answers are left unnormalized.
    """
    normalized_record = deepcopy(dict(record))
    data_key = "session_data" if isinstance(normalized_record.get("session_data"), Mapping) else None
    session_data = normalized_record.get(data_key) if data_key else normalized_record
    raw_data = record.get(data_key) if data_key else record
    if not isinstance(session_data, dict) or not isinstance(raw_data, Mapping):
        return normalized_record, {
            "version": NORMALIZATION_VERSION,
            "status": "invalid_record",
            "answers": [],
        }

    answers = session_data.get("respuestas")
    raw_answers = raw_data.get("respuestas")
    if not isinstance(answers, list) or not isinstance(raw_answers, list):
        return normalized_record, {
            "version": NORMALIZATION_VERSION,
            "status": "no_answers",
            "answers": [],
        }

    question_map = _question_map(str(Path(model_dir) / "mapeo_unity_ml_v1.json"))
    audit_answers = []
    for position, (answer, raw_answer) in enumerate(zip(answers, raw_answers)):
        if not isinstance(answer, dict) or not isinstance(raw_answer, Mapping):
            continue
        scene = raw_answer.get("escena")
        question_id = raw_answer.get("idPregunta")
        variable = question_map.get((scene, question_id))
        if variable is None:
            continue

        label = _parse_label(raw_answer.get("respuestaLabel"))
        index_value = raw_answer.get("respuestaIndex")
        index = _parse_index(index_value) if index_value is not None else None
        reason = None
        normalized_value = None
        status = "normalized"

        existing_version = raw_answer.get("respuesta_normalizacion_version")
        existing_value = _parse_label(raw_answer.get("respuesta_normalizada"))
        if existing_version == NORMALIZATION_VERSION:
            if label is not None and existing_value is not None and 1 <= existing_value <= 9 and existing_value == 10 - label:
                normalized_value = existing_value
                status = "already_normalized"
            else:
                status = "not_normalized"
                reason = "invalid_existing_normalization"
        elif label is None:
            status = "not_normalized"
            reason = "missing_or_non_integer_label"
        elif not 1 <= label <= 9:
            status = "not_normalized"
            reason = "label_out_of_range"
        elif index_value is not None and (index is None or not 0 <= index <= 8):
            status = "not_normalized"
            reason = "index_out_of_range_or_invalid"
        elif index is not None and label + index != 9:
            status = "not_normalized"
            reason = "label_index_inconsistent"
        else:
            normalized_value = 10 - label
            answer["respuesta_normalizada"] = normalized_value
            answer["respuesta_normalizacion_version"] = NORMALIZATION_VERSION

        if normalized_value is not None:
            answer["respuesta_normalizada"] = normalized_value
            answer["respuesta_normalizacion_version"] = NORMALIZATION_VERSION

        audit_answers.append(
            {
                "position": position,
                "escena": scene,
                "idPregunta": question_id,
                "variable": variable,
                "respuestaLabel_original": raw_answer.get("respuestaLabel"),
                "respuestaIndex_original": index_value,
                "respuesta_normalizada": normalized_value,
                "version": NORMALIZATION_VERSION,
                "status": status,
                "reason": reason,
            }
        )

    summary_status = "normalized" if all(item["status"] in {"normalized", "already_normalized"} for item in audit_answers) else "partial"
    return normalized_record, {
        "version": NORMALIZATION_VERSION,
        "status": summary_status if audit_answers else "no_mapped_answers",
        "answers": audit_answers,
    }


def prepare_v8_input(
    normalized_record: Mapping[str, Any], normalization: Mapping[str, Any]
) -> dict[str, Any]:
    """Create a transient V8 input where only valid mapped labels are replaced."""
    model_record = deepcopy(dict(normalized_record))
    data_key = "session_data" if isinstance(model_record.get("session_data"), Mapping) else None
    session_data = model_record.get(data_key) if data_key else model_record
    if not isinstance(session_data, dict):
        return model_record
    answers = session_data.get("respuestas")
    reports = normalization.get("answers")
    if not isinstance(answers, list) or not isinstance(reports, list):
        return model_record
    for report in reports:
        if report.get("status") not in {"normalized", "already_normalized"}:
            continue
        position = report.get("position")
        value = report.get("respuesta_normalizada")
        if isinstance(position, int) and 0 <= position < len(answers) and isinstance(value, int) and 1 <= value <= 9:
            answer = answers[position]
            if isinstance(answer, dict):
                # The persisted raw record remains untouched. This copy is only
                # passed to the official adapter, which reads respuestaLabel.
                answer["respuestaLabel"] = str(value)
    return model_record
