"""Pruebas de SessionProcessingService con EmotionLab V8.

La inferencia se delega en el servicio inyectado; el doble V8 verifica el flujo
sin cargar Joblib ni conectarse a AWS.
"""

from __future__ import annotations

from copy import deepcopy

from services.session_processing_service import SessionProcessingService
from services.session_service import STATUS_PENDING_IDENTIFICATION
from services.emotion_model_v8_contract import MODEL_VERSION as MODEL_VERSION_V8
from services.parsers.emotion_v8_parser import EmotionV8Parser

STATUS_ANALYZED = "analyzed"
STATUS_INSUFFICIENT_DATA = "insufficient_data"
STATUS_PROCESSING_ERROR = "processing_error"


class FakeDynamo:
    def __init__(self, items=None):
        self.items = [deepcopy(item) for item in (items or [])]
        self.updates = []

    def scan_all(self, table):
        return [deepcopy(item) for item in self.items]

    def update_item(self, table, key, updates):
        self.updates.append((table, key, deepcopy(updates)))
        for item in self.items:
            if item.get("session_id") == key.get("session_id"):
                item.update(deepcopy(updates))
                return True
        return True


class FakeSessionService:
    table = "emotionlab-dev-sessions"

    def __init__(self, dynamo):
        self.dynamo = dynamo
        self.created = []

    def create_session(self, session_id, raw_json, **kwargs):
        item = {
            "session_id": session_id,
            "raw_json": deepcopy(raw_json),
            **deepcopy(kwargs),
        }
        self.created.append(item)
        self.dynamo.items.append(deepcopy(item))
        return item


class FakeEmotionV8Service:
    """Doble del servicio V8; conserva la semántica de dos grupos y 12 campos."""

    model_version = MODEL_VERSION_V8

    def __init__(self, group=1, failures=None):
        self.group = group
        self.failures = set(failures or [])
        self.calls = []

    def analyze(self, session_id, record):
        call_number = len(self.calls) + 1
        valores = EmotionV8Parser(record).get_model_values()
        self.calls.append({"session_id": session_id, "valores": dict(valores)})
        if call_number in self.failures:
            raise RuntimeError("modelo no disponible")
        complete = all(valor is not None for valor in valores.values())
        if not complete:
            return {
                "session_id": session_id,
                "status": STATUS_INSUFFICIENT_DATA,
                "prediction": {"group": None, "n_clusters": 2},
                "grupo": None,
                "variables_originales": valores,
                "model_version": self.model_version,
            }
        return {
            "session_id": session_id,
            "status": STATUS_ANALYZED,
            "prediction": {"group": self.group, "n_clusters": 2},
            "grupo": self.group,
            "variables_originales": valores,
            "model_version": self.model_version,
        }

    @staticmethod
    def error_block(error, session_id=None):
        return {
            "session_id": session_id,
            "status": STATUS_PROCESSING_ERROR,
            "prediction": {"group": None, "n_clusters": 2},
            "error": str(error),
        }


def complete_record(session_id="session-1"):
    """Registro Unity con las doce preguntas oficiales V8."""

    return {
        "s3_key": f"data/2026-09-08/{session_id}.json",
        "session_data": {
            "session_id": session_id,
            "idParticipante": "participante_demo",
            "fechaInicio": "2026-09-08 10:00:00",
            "fechaFin": "2026-09-08 10:10:00",
            "difficulty_level": "facil",
            "respuestas": [
                {"idPregunta": "pregunta_demo", "respuestaLabel": "5", "escena": "waiting_room_inicial"},
                {"idPregunta": "genero_demo", "respuestaLabel": "test-only", "escena": "waiting_room_inicial"},
                {
                    "idPregunta": "nerviosismo",
                    "respuestaLabel": "5",
                    "escena": "waiting_room_inicial",
                },
                {
                    "idPregunta": "sentir_hoy",
                    "respuestaLabel": "6",
                    "escena": "waiting_room_inicial",
                },
                {
                    "idPregunta": "nerviosismo_cierre",
                    "respuestaLabel": "4",
                    "escena": "Retroalimentacion_Oficina",
                },
                {
                    "idPregunta": "sentir_hoy_cierre",
                    "respuestaLabel": "5",
                    "escena": "Retroalimentacion_Oficina",
                },
                {
                    "idPregunta": "nerviosismo_cierre",
                    "respuestaLabel": "7",
                    "escena": "Retroalimentacion_Salon",
                },
                {
                    "idPregunta": "sentir_hoy_cierre",
                    "respuestaLabel": "6",
                    "escena": "Retroalimentacion_Salon",
                },
                {"idPregunta": "preocupacion", "respuestaLabel": "3", "escena": "waiting_room_inicial"},
                {"idPregunta": "preparacion", "respuestaLabel": "4", "escena": "waiting_room_inicial"},
                {"idPregunta": "satisfaccion_cierre", "respuestaLabel": "5", "escena": "Retroalimentacion_Oficina"},
                {"idPregunta": "aliviado_cierre", "respuestaLabel": "6", "escena": "Retroalimentacion_Oficina"},
                {"idPregunta": "satisfaccion_cierre", "respuestaLabel": "7", "escena": "Retroalimentacion_Salon"},
                {"idPregunta": "aliviado_cierre", "respuestaLabel": "8", "escena": "Retroalimentacion_Salon"},
            ],
            "eventos": [],
        },
    }


def incomplete_record(session_id="session-incomplete"):
    record = complete_record(session_id)
    record["session_data"]["fechaInicio"] = ""
    record["session_data"]["fechaFin"] = ""
    record["session_data"]["respuestas"] = []
    record["session_data"]["eventos"] = []
    return record


def build_processor(items=None, group=1, failures=None):
    dynamo = FakeDynamo(items)
    service = FakeSessionService(dynamo)
    engine = FakeEmotionV8Service(group, failures)
    processor = SessionProcessingService(service, engine)
    return processor, service, engine


def test_dry_run_does_not_analyze_or_write():
    processor, service, engine = build_processor()

    summary = processor.process([complete_record()], execute=False)

    assert summary["total"] == 1
    assert summary["features_complete"] == 1
    assert summary["would_create"] == 1
    assert engine.calls == []
    assert service.created == []


def test_preview_analyzes_without_persisting():
    processor, service, engine = build_processor(group=1)

    summary = processor.preview([complete_record()])

    assert summary["total"] == 1
    assert summary["processable"] == 1
    assert summary["analyzed"] == 1
    assert summary["groups"]["1"] == 1
    assert summary["details"][0]["group"] == 1
    assert service.created == []
    assert service.dynamo.updates == []
    assert len(engine.calls) == 1


def test_preview_reports_engine_errors_without_persisting():
    processor, service, engine = build_processor(failures={1})

    summary = processor.preview([complete_record()])

    assert summary["not_processable"] == 1
    assert summary["errors"] == 1
    assert "processing_error" in summary["details"][0]
    assert service.created == []
    assert service.dynamo.updates == []


def test_preview_reports_insufficient_data():
    processor, service, engine = build_processor()

    summary = processor.preview([incomplete_record()])

    assert summary["insufficient_data"] == 1
    assert summary["not_processable"] == 1
    assert summary["analyzed"] == 0
    assert service.created == []


def test_new_session_is_saved_with_v8_analysis():
    processor, service, engine = build_processor(group=1)
    record = complete_record()

    summary = processor.process([record], execute=True)

    assert summary["processed_ok"] == 1
    assert summary["saved"] == 1
    assert service.created[0]["raw_json"] == record
    assert service.created[0]["participant_id"] == "participante_demo"
    assert service.created[0]["status"] == STATUS_PENDING_IDENTIFICATION

    # El análisis se persiste bajo el MISMO `session_id` de la experiencia.
    update = service.dynamo.updates[-1][2]
    assert update["model_processing"]["prediction"]["group"] == 1
    assert update["model_processing"]["status"] == STATUS_ANALYZED
    assert update["ai_status"] == STATUS_ANALYZED
    assert update["model_version"] == MODEL_VERSION_V8


def test_insufficient_data_marks_session_without_error():
    processor, service, engine = build_processor()

    summary = processor.process([incomplete_record()], execute=True)

    assert summary["insufficient_data"] == 1
    assert summary["processed_ok"] == 1
    assert service.created[0]["status"] == STATUS_PENDING_IDENTIFICATION
    assert "processing_error" not in service.created[0]

    update = service.dynamo.updates[-1][2]
    assert update["model_processing"]["status"] == STATUS_INSUFFICIENT_DATA


def test_engine_failure_preserves_the_capture():
    processor, service, engine = build_processor(failures={1})

    summary = processor.process([complete_record()], execute=True)

    assert summary["processed_ok"] == 1
    assert service.created[0]["raw_json"]["s3_key"].endswith("session-1.json")

    update = service.dynamo.updates[-1][2]
    assert update["model_processing"]["status"] == STATUS_PROCESSING_ERROR


def test_same_source_file_is_skipped():
    record = complete_record()
    existing = {
        "session_id": "session-1",
        "source_key": record["s3_key"],
        "model_processing": {"status": STATUS_ANALYZED},
    }
    processor, service, engine = build_processor([existing])

    summary = processor.process([record], execute=True)

    assert summary["skipped_duplicates"] == 1
    assert summary["saved"] == 0
    assert engine.calls == []
    assert service.created == []


def test_existing_session_with_duplicate_unity_answers_is_rejected():
    existing = {"session_id": "session-1", "raw_json": complete_record("session-1")}
    processor, service, engine = build_processor([existing])

    second = complete_record("session-1")
    second["s3_key"] = "data/2026-09-08/session-1_captura2.json"

    summary = processor.process([second], execute=True)

    assert summary["already_existing"] == 1
    assert summary["processed_ok"] == 1
    assert service.created == []
    assert service.dynamo.updates[-1][2]["model_processing"]["status"] == STATUS_PROCESSING_ERROR


def test_one_engine_error_does_not_stop_other_sessions():
    processor, service, engine = build_processor(failures={1})
    records = [complete_record("session-error"), complete_record("session-ok")]

    summary = processor.process(records, execute=True)

    assert summary["total"] == 2
    assert summary["processed_ok"] == 2
    assert len(service.created) == 2
    assert len(engine.calls) == 2


def test_processing_uses_the_original_unity_session_id():
    """El procesamiento conserva el `session_id` original de la experiencia."""

    processor, service, _ = build_processor()
    unity_id = "6f1c0b9e-0000-4000-8000-000000000001"

    summary = processor.process([complete_record(unity_id)], execute=True)

    assert summary["processed_ok"] == 1
    assert service.created[0]["session_id"] == unity_id


def test_second_data_set_with_same_session_id_enriches_without_duplicating():
    """Un conjunto nuevo de la MISMA experiencia se agrega, no se duplica."""

    existing = {"session_id": "session-1", "raw_json": complete_record("session-1")}
    processor, service, _ = build_processor([existing])

    second = complete_record("session-1")
    second["s3_key"] = "data/2026-09-08/session-1_captura2.json"
    second["session_data"]["respuestas"] = [
        {"idPregunta": "extra", "respuestaLabel": "x", "escena": "aula"}
    ]

    summary = processor.process([second], execute=True)

    # No se crea una segunda sesión EmotionLab.
    assert service.created == []
    assert len(service.dynamo.items) == 1
    assert summary["processed_ok"] == 1

    # La información de ambos conjuntos queda en la misma sesión.
    merged = service.dynamo.updates[0][2]["raw_json"]
    assert len(merged["session_data"]["respuestas"]) == 15


def test_new_data_set_with_duplicate_answers_does_not_reclassify():
    """El adaptador V8 detecta preguntas repetidas entre registros unidos."""

    existing = {
        "session_id": "session-1",
        "raw_json": complete_record("session-1"),
        "model_processing": {"status": STATUS_ANALYZED, "prediction": {"group": 0}},
    }
    processor, service, engine = build_processor([existing], group=1)

    second = complete_record("session-1")
    second["s3_key"] = "data/2026-09-08/session-1_captura2.json"

    processor.process([second], execute=True)

    assert service.created == []
    assert len(engine.calls) == 0
    assert service.dynamo.updates[-1][2]["model_processing"]["status"] == STATUS_PROCESSING_ERROR
