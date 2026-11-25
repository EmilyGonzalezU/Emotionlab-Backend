"""Pruebas del seguimiento por estudiante y experiencia (TrackingService)."""

import pytest

from core.constants import (
    DYNAMODB_TABLE_SESSIONS,
    DYNAMODB_TABLE_TRACKINGS,
)
from services.tracking_service import TrackingService


class FakeDynamoDB:
    """Persistencia en memoria con filtrado real por ``tracking_id``."""

    def __init__(self):
        self.trackings = {}
        self.sessions = {}

    @staticmethod
    def _eq_value(expression):
        valores = (expression.get_expression() or {}).get("values") or ()
        return valores[-1] if valores else None

    def get_item(self, table_name, key):
        if table_name == DYNAMODB_TABLE_TRACKINGS:
            item = self.trackings.get(key["tracking_code"])
            return dict(item) if item else None

        return None

    def query_all(self, table_name, key_condition_expression, **kwargs):
        valor = self._eq_value(key_condition_expression)

        if table_name == DYNAMODB_TABLE_TRACKINGS:
            coincidencias = [
                dict(item)
                for item in self.trackings.values()
                if item.get("tracking_id") == valor
            ]
            max_items = kwargs.get("max_items")
            return coincidencias[:max_items] if max_items else coincidencias

        if table_name == DYNAMODB_TABLE_SESSIONS:
            return [
                dict(item)
                for item in self.sessions.values()
                if item.get("tracking_id") == valor
            ]

        return []

    def scan_all(self, table_name, **kwargs):
        return []

    def transact_write_items(self, operations):
        # No se usan operaciones de escritura en estas pruebas.
        return None


class FakeSessionService:
    def _resolve_session_key(self, session_id):
        return None

    def list_sessions(self):
        return []


def _procesamiento(grupo, inicial, facil, dificil):
    return {
        "status": "analyzed",
        "prediction": {"group": grupo, "n_clusters": 3},
        "derived_indicators": {
            "etapas": {
                "inicial": inicial,
                "facil": facil,
                "dificil": dificil,
            }
        },
    }


@pytest.fixture
def setup():
    dynamo = FakeDynamoDB()

    # Un seguimiento representa a UN ÚNICO alumno (participante técnico).
    dynamo.trackings["EXP-A"] = {
        "tracking_code": "EXP-A",
        "tracking_id": "EXP-ID-A",
        "participant_id": "STUDENT-1",
        "created_at": "2026-11-01T10:00:00Z",
        "updated_at": "2026-11-01T10:00:00Z",
    }
    dynamo.trackings["EXP-B"] = {
        "tracking_code": "EXP-B",
        "tracking_id": "EXP-ID-B",
        "participant_id": "STUDENT-1",
        "created_at": "2026-11-01T10:00:00Z",
        "updated_at": "2026-11-01T10:00:00Z",
    }

    # Estudiante 1 en la experiencia A: tres sesiones.
    dynamo.sessions["S1"] = {
        "session_id": "S1",
        "participant_id": "STUDENT-1",
        "tracking_id": "EXP-ID-A",
        "fecha": "2026-11-10",
        "hora": "09:00",
        "model_processing": _procesamiento(2, 6.2, 6.9, 8.3),
    }
    dynamo.sessions["S2"] = {
        "session_id": "S2",
        "participant_id": "STUDENT-1",
        "tracking_id": "EXP-ID-A",
        "fecha": "2026-11-12",
        "hora": "09:00",
        "model_processing": _procesamiento(0, 3.0, 4.0, 5.0),
    }
    dynamo.sessions["S3"] = {
        "session_id": "S3",
        "participant_id": "STUDENT-1",
        "tracking_id": "EXP-ID-A",
        "fecha": "2026-11-11",
        "hora": "09:00",
        "model_processing": _procesamiento(1, 5.0, 5.0, 5.0),
    }
    # Otra experiencia del mismo alumno: no debe mezclarse.
    dynamo.sessions["S4"] = {
        "session_id": "S4",
        "participant_id": "STUDENT-1",
        "tracking_id": "EXP-ID-B",
        "fecha": "2026-11-09",
        "hora": "09:00",
        "model_processing": _procesamiento(1, 1.0, 1.0, 1.0),
    }

    return dynamo, TrackingService(dynamo, FakeSessionService())


def _add_foreign_session(dynamo, tracking_id="EXP-ID-A"):
    """Vínculo inconsistente: sesión de OTRO alumno bajo el mismo seguimiento."""

    dynamo.sessions["S5"] = {
        "session_id": "S5",
        "participant_id": "STUDENT-2",
        "tracking_id": tracking_id,
        "fecha": "2026-11-13",
        "hora": "09:00",
        "model_processing": _procesamiento(2, 9.0, 9.0, 9.0),
    }


def test_follow_up_returns_student_sessions_chronologically(setup):
    dynamo, service = setup

    registros = service.get_follow_up("EXP-ID-A")

    # Orden cronológico ascendente y solo las sesiones del alumno.
    assert [r["session_id"] for r in registros] == ["S1", "S3", "S2"]

    # Enmascaramiento: el grupo original 2 se expone como 1.
    assert registros[0]["classification_original"] == 2
    assert registros[0]["classification_final"] == 1
    assert registros[1]["classification_original"] == 1
    assert registros[1]["classification_final"] == 1

    # Bienestar: media de los tres indicadores, escala 1-9.
    assert registros[0]["bienestar_promedio"] == 7.13
    assert registros[0]["indicador_inicial"] == 6.2
    assert registros[0]["indicador_facil"] == 6.9
    assert registros[0]["indicador_dificil"] == 8.3
    assert registros[0]["experience_id"] == "EXP-ID-A"


def test_follow_up_keeps_experiences_separate(setup):
    dynamo, service = setup

    experiencia_a = service.get_follow_up("EXP-ID-A")
    experiencia_b = service.get_follow_up("EXP-ID-B")

    assert {r["session_id"] for r in experiencia_a} == {"S1", "S2", "S3"}
    assert {r["session_id"] for r in experiencia_b} == {"S4"}


def test_follow_up_keeps_each_session_by_session_id(setup):
    """Las experiencias se conservan aunque tengan participant_id distintos."""

    dynamo, service = setup
    _add_foreign_session(dynamo)

    registros = service.get_follow_up("EXP-ID-A")

    assert {r["session_id"] for r in registros} == {"S1", "S2", "S3", "S5"}


def test_follow_up_does_not_duplicate_on_reprocess(setup):
    dynamo, service = setup

    # Reprocesar la MISMA sesión actualiza su `model_processing` en sitio:
    # no se crea un registro nuevo (la clave es el `session_id`).
    dynamo.sessions["S1"]["model_processing"] = _procesamiento(1, 7.0, 7.0, 7.0)

    registros = service.get_follow_up("EXP-ID-A")

    assert [r["session_id"] for r in registros] == ["S1", "S3", "S2"]
    assert registros[0]["classification_original"] == 1
    assert registros[0]["bienestar_promedio"] == 7.0


# ==========================================================
# EVOLUCIÓN DEL BIENESTAR (GRÁFICO LONGITUDINAL)
# ==========================================================


def test_evolution_returns_chronological_wellbeing(setup):
    dynamo, service = setup

    evolucion = service.get_evolution("EXP-ID-A")

    assert evolucion["tracking_id"] == "EXP-ID-A"
    assert evolucion["total_sessions"] == 3
    # Orden CRONOLÓGICO ascendente, numerado desde 1.
    assert [p["session_id"] for p in evolucion["sessions"]] == ["S1", "S3", "S2"]
    assert [p["session_number"] for p in evolucion["sessions"]] == [1, 2, 3]
    assert [p["wellbeing"] for p in evolucion["sessions"]] == [7.13, 5.0, 4.0]
    assert (
        evolucion["sessions"][0]["session_date"]
        == "2026-11-10 09:00"
    )


def test_evolution_keeps_other_participant_session(setup):
    """La evolución representa las experiencias por session_id."""

    dynamo, service = setup
    _add_foreign_session(dynamo)

    evolucion = service.get_evolution("EXP-ID-A")

    assert evolucion["tracking_id"] == "EXP-ID-A"
    assert [p["session_id"] for p in evolucion["sessions"]] == [
        "S1", "S3", "S2", "S5",
    ]
    assert evolucion["total_sessions"] == 4


def test_evolution_does_not_duplicate_on_reprocess(setup):
    dynamo, service = setup

    # Reprocesar la MISMA sesión actualiza su procesamiento en sitio: la
    # evolución no debe sumar un punto nuevo.
    dynamo.sessions["S1"]["model_processing"] = _procesamiento(1, 7.0, 7.0, 7.0)

    evolucion = service.get_evolution("EXP-ID-A")

    assert evolucion["total_sessions"] == 3
    s1 = next(p for p in evolucion["sessions"] if p["session_id"] == "S1")
    assert s1["wellbeing"] == 7.0


def test_evolution_handles_incomplete_analysis(setup):
    dynamo, service = setup

    # Falta un indicador: no se calcula un promedio parcial.
    dynamo.sessions["S2"]["model_processing"] = _procesamiento(1, None, 5.0, 5.0)

    evolucion = service.get_evolution("EXP-ID-A")

    s2 = next(p for p in evolucion["sessions"] if p["session_id"] == "S2")
    assert s2["wellbeing"] is None
    assert evolucion["total_sessions"] == 3


def test_evolution_empty_tracking(setup):
    dynamo, service = setup

    dynamo.trackings["EXP-C"] = {
        "tracking_code": "EXP-C",
        "tracking_id": "EXP-ID-C",
        "created_at": "2026-11-01T10:00:00Z",
        "updated_at": "2026-11-01T10:00:00Z",
    }

    evolucion = service.get_evolution("EXP-ID-C")

    assert evolucion["total_sessions"] == 0
    assert evolucion["sessions"] == []


def test_evolution_missing_tracking(setup):
    dynamo, service = setup

    with pytest.raises(ValueError, match="no existe"):
        service.get_evolution("NO-EXISTE")


def test_evolution_does_not_re_run_model(setup):
    dynamo, service = setup

    from copy import deepcopy

    before = deepcopy(dynamo.sessions)

    # Solo lee resultados persistidos: no escribe ni recalcula nada.
    service.get_evolution("EXP-ID-A")

    assert dynamo.sessions == before


def test_evolution_renumbers_after_adding_and_removing(setup):
    """La secuencia se recalcula al incorporar o quitar sesiones."""

    dynamo, service = setup

    # Se incorpora una sesión más antigua: la numeración se recalcula.
    dynamo.sessions["S0"] = {
        "session_id": "S0",
        "participant_id": "STUDENT-1",
        "tracking_id": "EXP-ID-A",
        "fecha": "2026-11-08",
        "hora": "09:00",
        "model_processing": _procesamiento(1, 2.0, 2.0, 2.0),
    }

    evolucion = service.get_evolution("EXP-ID-A")
    assert [p["session_id"] for p in evolucion["sessions"]] == [
        "S0", "S1", "S3", "S2",
    ]
    assert [p["session_number"] for p in evolucion["sessions"]] == [1, 2, 3, 4]

    # Al desvincular una sesión, la secuencia se recalcula sin huecos.
    dynamo.sessions["S3"].pop("tracking_id")

    evolucion = service.get_evolution("EXP-ID-A")
    assert [p["session_id"] for p in evolucion["sessions"]] == ["S0", "S1", "S2"]
    assert [p["session_number"] for p in evolucion["sessions"]] == [1, 2, 3]
