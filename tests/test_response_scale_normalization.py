import json
from copy import deepcopy

import pytest

from services.emotion_model_v8_package import MODEL_DIR
from services.parsers.emotion_v8_parser import EmotionV8Parser
from services.response_scale_normalization import (
    NORMALIZATION_VERSION,
    annotate_unity_record,
    prepare_v8_input,
)
from services.capture_service import CaptureService


def _record(question="sentir_hoy", label="9", index=0, scene="waiting_room_inicial"):
    return {
        "session_data": {
            "idParticipante": "test-participant",
            "respuestas": [
                {
                    "escena": scene,
                    "idPregunta": question,
                    "respuestaLabel": label,
                    "respuestaIndex": index,
                    "timestamp": "2026-11-24T12:00:00Z",
                }
            ],
            "eventos": [{"nombre": "session_started", "payload": {"value": 1}}],
        }
    }


@pytest.mark.parametrize(
    ("label", "index", "expected"),
    [("9", 0, 1), ("8", 1, 2), ("5", 4, 5), ("1", 8, 9)],
)
def test_normalizes_only_confirmed_inverted_scale(label, index, expected):
    raw = _record(label=label, index=index)
    original = deepcopy(raw)

    normalized, report = annotate_unity_record(raw)

    assert raw == original
    assert normalized["session_data"]["respuestas"][0]["respuestaLabel"] == label
    assert normalized["session_data"]["respuestas"][0]["respuesta_normalizada"] == expected
    assert report["version"] == NORMALIZATION_VERSION
    assert report["answers"][0]["status"] == "normalized"
    model_input = prepare_v8_input(normalized, report)
    assert model_input["session_data"]["respuestas"][0]["respuestaLabel"] == str(expected)
    assert model_input["session_data"]["respuestas"][0]["respuestaIndex"] == index


@pytest.mark.parametrize(
    ("label", "index", "reason"),
    [
        (None, 0, "missing_or_non_integer_label"),
        ("10", 0, "label_out_of_range"),
        ("0", 9, "label_out_of_range"),
        ("8", 0, "label_index_inconsistent"),
        ("9", 9, "index_out_of_range_or_invalid"),
        ("9.0", 0, "missing_or_non_integer_label"),
    ],
)
def test_invalid_answers_are_left_unchanged_and_reported(label, index, reason):
    raw = _record(label=label, index=index)
    normalized, report = annotate_unity_record(raw)

    assert normalized["session_data"]["respuestas"][0]["respuestaLabel"] == label
    assert "respuesta_normalizada" not in normalized["session_data"]["respuestas"][0]
    assert report["answers"][0]["status"] == "not_normalized"
    assert report["answers"][0]["reason"] == reason
    assert prepare_v8_input(normalized, report) == normalized


@pytest.mark.parametrize(
    ("question", "scene"),
    [("genero", "waiting_room_inicial"), ("edad", "waiting_room_inicial"), ("unknown", "waiting_room_inicial")],
)
def test_categorical_and_unmapped_questions_are_never_normalized(question, scene):
    raw = _record(question=question, label="9", index=0, scene=scene)
    normalized, report = annotate_unity_record(raw)

    assert normalized == raw
    assert report["status"] == "no_mapped_answers"
    assert report["answers"] == []


def test_events_are_not_modified():
    raw = _record()
    normalized, _report = annotate_unity_record(raw)
    assert normalized["session_data"]["eventos"] == raw["session_data"]["eventos"]


def test_all_twelve_v8_question_mappings_feed_normalized_values():
    mapping = json.loads((MODEL_DIR / "mapeo_unity_ml_v1.json").read_text(encoding="utf-8"))
    answers = []
    expected = {}
    for offset, item in enumerate(mapping["equivalencias"]):
        value = (offset % 9) + 1
        answers.append({
            "escena": item["escena"],
            "idPregunta": item["idPregunta"],
            "respuestaLabel": str(10 - value),
            "respuestaIndex": value - 1,
        })
        expected[item["variable"]] = value
    raw = {"session_data": {"respuestas": answers}}

    normalized, report = annotate_unity_record(raw)
    parsed = EmotionV8Parser(prepare_v8_input(normalized, report))

    assert len(report["answers"]) == 12
    assert parsed.get_model_values() == expected
    assert len(mapping["equivalencias"]) == 12


def test_reapplying_rule_to_versioned_normalization_does_not_invert_twice():
    once, first_report = annotate_unity_record(_record(label="9", index=0))
    twice, second_report = annotate_unity_record(once)

    assert twice["session_data"]["respuestas"][0]["respuestaLabel"] == "9"
    assert twice["session_data"]["respuestas"][0]["respuesta_normalizada"] == 1
    assert second_report["answers"][0]["status"] == "already_normalized"
    assert prepare_v8_input(twice, second_report)["session_data"]["respuestas"][0]["respuestaLabel"] == "1"
    assert first_report["version"] == second_report["version"]


def test_invalid_existing_normalization_fails_closed():
    raw = _record(label="9", index=0)
    answer = raw["session_data"]["respuestas"][0]
    answer["respuesta_normalizada"] = 2
    answer["respuesta_normalizacion_version"] = NORMALIZATION_VERSION

    normalized, report = annotate_unity_record(raw)

    assert report["answers"][0]["status"] == "not_normalized"
    assert report["answers"][0]["reason"] == "invalid_existing_normalization"
    assert prepare_v8_input(normalized, report)["session_data"]["respuestas"][0]["respuestaLabel"] == "9"


def test_missing_index_does_not_create_a_value_from_index_or_zero():
    raw = _record(label="8", index=None)
    normalized, report = annotate_unity_record(raw)

    assert normalized["session_data"]["respuestas"][0]["respuesta_normalizada"] == 2
    assert normalized["session_data"]["respuestas"][0]["respuestaIndex"] is None
    assert report["answers"][0]["status"] == "normalized"


def test_historical_records_without_marker_remain_unmodified_for_v8():
    raw = _record(label="9", index=0)
    merged, model_input, normalization = CaptureService._merge_capture_records(
        [{"record": raw, "unity_session_id": "old-session"}]
    )

    assert merged == raw
    assert model_input == raw
    assert normalization is None


def test_capture_aggregation_preserves_raw_and_uses_separate_normalized_copy():
    raw = _record(label="9", index=0)
    _normalized, report = annotate_unity_record(raw)
    merged, model_input, combined_report = CaptureService._merge_capture_records(
        [{
            "record": raw,
            "response_normalization": report,
            "unity_session_id": "new-session",
        }]
    )

    assert merged == raw
    assert merged["session_data"]["respuestas"][0]["respuestaLabel"] == "9"
    assert model_input["session_data"]["respuestas"][0]["respuestaLabel"] == "1"
    assert combined_report["version"] == NORMALIZATION_VERSION
    assert combined_report["records"][0]["answers"][0]["respuesta_normalizada"] == 1


def test_capture_model_call_receives_normalized_copy_and_persists_audit_only():
    class CapturingModel:
        model_version = "v8-test"

        def analyze(self, _session_id, record):
            self.received_record = record
            return {"status": "analyzed", "variables_originales": {"estado_pre": 1}}

    raw = _record(label="9", index=0)
    _normalized, report = annotate_unity_record(raw)
    merged, model_input, combined_report = CaptureService._merge_capture_records(
        [{"record": raw, "response_normalization": report}]
    )
    service = CaptureService.__new__(CaptureService)
    service.emotion_v2 = CapturingModel()

    fields = service._analyze_v2(
        "new-session",
        merged,
        {},
        model_input=model_input,
        normalization=combined_report,
    )

    assert raw["session_data"]["respuestas"][0]["respuestaLabel"] == "9"
    assert service.emotion_v2.received_record["session_data"]["respuestas"][0]["respuestaLabel"] == "1"
    assert fields["model_processing"]["response_normalization"]["version"] == NORMALIZATION_VERSION
    assert fields["model_processing"]["source_record_sha256"] == CaptureService._record_sha256(raw)
