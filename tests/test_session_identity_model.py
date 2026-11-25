"""Pruebas del modelo de identidad de sesiones de EmotionLab.

Principio definitivo: una experiencia = un `sessionId` = una sesión en
EmotionLab. El `sessionId` generado por la Lambda `createSession` es la
identidad permanente de toda la experiencia; el procesamiento enriquece
esa misma sesión y el seguimiento es una agrupación independiente.

Estas pruebas verifican que la importación conserva el identificador
original de Unity y que varios conjuntos de datos de la MISMA experiencia
se agregan a una sola sesión sin perder información.
"""

from __future__ import annotations

from copy import deepcopy

from services.import_service import ImportService
from services.session_service import STATUS_PENDING_IDENTIFICATION


# `sessionId` original que genera la Lambda `createSession`. Es la
# identidad permanente de la experiencia y de todos sus datos.
LENS_SESSION_ID = "6f1c0b9e-0000-4000-8000-000000000001"


class FakeSessionService:
    """Sesiones en memoria que respetan el contrato de ImportService."""

    table = "emotionlab-dev-sessions"

    def __init__(self):
        self.store = {}
        self.created = []

    def build_import_index(self):
        by_source, by_session = {}, {}
        for item in self.store.values():
            session_id = item.get("session_id") or item.get("id")
            source_key = item.get("source_key")
            status = item.get("status") or item.get("estado")
            summary = {
                "id": session_id,
                "session_id": session_id,
                "source_key": source_key,
                "participant_id": item.get("participant_id"),
                "status": status,
                "estado": status,
            }
            if source_key:
                by_source[source_key] = summary
            if session_id:
                by_session[session_id] = summary
        return by_source, by_session

    def create_session(self, session_id, raw_json, **kwargs):
        if session_id in self.store:
            raise ValueError("ConditionalCheckFailedException")
        item = {"session_id": session_id, "raw_json": deepcopy(raw_json)}
        item.update(deepcopy(kwargs))
        item["session_id"] = session_id
        self.store[session_id] = item
        self.created.append(deepcopy(item))
        return deepcopy(item)

    def get_session(self, session_id):
        item = self.store.get(session_id)
        return deepcopy(item) if item else None

    def merge_session_data_set(self, session_id, raw_json, *, fields=None):
        if session_id not in self.store:
            raise ValueError("La sesion no existe")
        self.store[session_id]["raw_json"] = deepcopy(raw_json)
        if fields:
            self.store[session_id].update(deepcopy(fields))


def build_import():
    sessions = FakeSessionService()
    service = ImportService(session_service=sessions)
    return service, sessions


def lens_record(session_id=LENS_SESSION_ID, source="captura1", extra_answers=0):
    """Conjunto de datos COMPLETO de una experiencia del lente."""

    answers = [
        {"idPregunta": "pregunta_demo", "respuestaLabel": "5", "escena": "aula"},
        {"idPregunta": "genero_demo", "respuestaLabel": "test-only", "escena": "aula"},
    ]
    for index in range(extra_answers):
        answers.append(
            {
                "idPregunta": f"extra_{index}",
                "respuestaLabel": "x",
                "escena": "aula",
            }
        )
    return {
        "s3_key": f"raw_sessions/2026-11-15/{session_id}_{source}.json",
        "session_data": {
            "session_id": session_id,
            "idParticipante": "participante_demo",
            "fechaInicio": "2026-11-15 10:00:00",
            "fechaFin": "2026-11-15 10:10:00",
            "difficulty_level": "facil",
            "respuestas": answers,
            "eventos": [
                {
                    "nombre": "session_completed",
                    "timestamp": "2026-11-15 10:10:00",
                }
            ],
        },
    }


def incomplete_record(session_id=LENS_SESSION_ID, source="parcial"):
    """Conjunto de datos incompleto: todavía no permite predecir."""

    return {
        "s3_key": f"raw_sessions/2026-11-15/{session_id}_{source}.json",
        "session_data": {
            "session_id": session_id,
            "idParticipante": "participante_demo",
            "fechaInicio": "",
            "fechaFin": "",
            "difficulty_level": "facil",
            "respuestas": [],
            "eventos": [],
        },
    }


# ==========================================================
# IDENTIDAD DE LA EXPERIENCIA
# ==========================================================


def test_import_keeps_unity_session_id():
    """La importación conserva el identificador original de Unity."""

    service, sessions = build_import()

    result = service.import_batch({"sesiones": [lens_record()]})

    assert result["new"] == [LENS_SESSION_ID]
    assert list(sessions.store.keys()) == [LENS_SESSION_ID]
    assert sessions.store[LENS_SESSION_ID]["session_id"] == LENS_SESSION_ID
    # El identificador NO se reemplaza por un hash ni por un UUID.
    assert not LENS_SESSION_ID.startswith("session_")


def test_two_data_sets_with_same_session_id_do_not_create_two_sessions():
    """Dos conjuntos de datos de la misma experiencia = una sola sesión."""

    service, sessions = build_import()

    first = lens_record(source="captura1")
    second = lens_record(source="captura2", extra_answers=2)

    result = service.import_batch({"sesiones": [first, second]})

    assert len(sessions.store) == 1
    assert result["new"] == [LENS_SESSION_ID]
    assert result["aggregated"] == [LENS_SESSION_ID]
    assert sessions.store[LENS_SESSION_ID]["session_id"] == LENS_SESSION_ID


def test_aggregation_preserves_every_data_set():
    """Ningún conjunto se pierde: las respuestas se acumulan."""

    service, sessions = build_import()

    first = lens_record(source="captura1")
    second = lens_record(source="captura2", extra_answers=2)

    service.import_batch({"sesiones": [first, second]})

    stored = sessions.store[LENS_SESSION_ID]["raw_json"]["session_data"]

    # 2 respuestas base + 2 del primer conjunto + 2 del segundo.
    assert len(stored["respuestas"]) == 6
    for answer in (
        first["session_data"]["respuestas"]
        + second["session_data"]["respuestas"]
    ):
        assert answer in stored["respuestas"]


def test_different_experiences_are_never_merged():
    """Experiencias distintas jamás se fusionan, aunque sean del mismo alumno.

    El backend no conoce la identidad real del alumno: la correspondencia
    vive en el Excel local del especialista. Por eso nunca debe agrupar
    sesiones por participante, nombre, contenido ni metadatos. Cada
    experiencia conserva su propio `session_id`.
    """

    service, sessions = build_import()

    # Dos experiencias de la MISMA persona (según el Excel externo) con
    # datos idénticos: mismo `idParticipante` y las mismas respuestas.
    first = lens_record(session_id="uuid-experiencia-1", source="captura1")
    second = lens_record(session_id="uuid-experiencia-2", source="captura1")

    result = service.import_batch({"sesiones": [first, second]})

    assert set(result["new"]) == {
        "uuid-experiencia-1",
        "uuid-experiencia-2",
    }
    assert result["aggregated"] == []
    assert len(sessions.store) == 2
    # Ninguna experiencia adopta el identificador de la otra.
    assert sessions.store["uuid-experiencia-1"]["session_id"] == (
        "uuid-experiencia-1"
    )
    assert sessions.store["uuid-experiencia-2"]["session_id"] == (
        "uuid-experiencia-2"
    )


def test_aggregation_works_across_separate_batches():
    """La agregación también ocurre entre importaciones distintas."""

    service, sessions = build_import()

    service.import_batch({"sesiones": [lens_record(source="captura1")]})
    result = service.import_batch(
        {"sesiones": [lens_record(source="captura2", extra_answers=1)]}
    )

    assert len(sessions.store) == 1
    assert result["aggregated"] == [LENS_SESSION_ID]

    stored = sessions.store[LENS_SESSION_ID]["raw_json"]["session_data"]
    # 2 respuestas base + 2 del primer conjunto + 1 del segundo.
    assert len(stored["respuestas"]) == 5


def test_same_source_file_is_a_real_duplicate():
    """El MISMO archivo fuente no se agrega ni crea una segunda sesión."""

    service, sessions = build_import()
    record = lens_record(source="captura1")

    service.import_batch({"sesiones": [record]})
    result = service.import_batch({"sesiones": [deepcopy(record)]})

    assert len(sessions.store) == 1
    assert result["skipped_existing"] == [LENS_SESSION_ID]
    assert result["aggregated"] == []


# ==========================================================
# ENRIQUECIMIENTO DE LA MISMA SESIÓN
# ==========================================================


def test_aggregating_a_later_data_set_enriches_the_session():
    """La importación NO ejecuta IA: solo enriquece la MISMA sesión."""

    service, sessions = build_import()

    service.import_batch({"sesiones": [incomplete_record()]})
    stored = sessions.store[LENS_SESSION_ID]
    assert stored.get("model_processing") is None
    assert stored["status"] == STATUS_PENDING_IDENTIFICATION

    result = service.import_batch(
        {"sesiones": [lens_record(source="captura2")]}
    )

    assert result["aggregated"] == [LENS_SESSION_ID]
        # ImportService no corre el modelo: el análisis vigente lo realiza el ciclo de
    # captura o un reprocesamiento explícito.
    assert "model_processing" not in sessions.store[LENS_SESSION_ID]


def test_session_detail_is_retrievable_by_original_session_id():
    """El detalle se recupera por el `session_id` original y con todo."""

    service, sessions = build_import()

    service.import_batch({"sesiones": [lens_record(source="captura1")]})
    service.import_batch(
        {"sesiones": [lens_record(source="captura2", extra_answers=2)]}
    )

    detail = sessions.get_session(LENS_SESSION_ID)

    assert detail["session_id"] == LENS_SESSION_ID
    # 2 respuestas base + 2 del primer conjunto + 2 del segundo.
    assert len(detail["raw_json"]["session_data"]["respuestas"]) == 6


# ==========================================================
# REGISTROS SIN IDENTIFICADOR ORIGINAL
# ==========================================================


def test_record_without_original_id_is_not_assigned_to_a_lens_experience():
    """Un registro sin `session_id` no se adhiere a una experiencia ajena."""

    service, _ = build_import()

    orphan = {
        "s3_key": "raw_sessions/2026-11-15/legacy_without_id.json",
        "session_data": {
            "idParticipante": "participante_demo",
            "fechaInicio": "2026-11-15 10:00:00",
            "fechaFin": "2026-11-15 10:10:00",
            "difficulty_level": "facil",
            "respuestas": [],
            "eventos": [],
        },
    }

    result = service.import_batch({"sesiones": [orphan]})

    derived = result["new"][0]

    # Nunca adopta el identificador de otra experiencia del lente.
    assert derived != LENS_SESSION_ID
    assert derived.startswith("session_")

    # El identificador derivado es estable entre llamadas.
    assert ImportService._session_id(orphan) == derived
