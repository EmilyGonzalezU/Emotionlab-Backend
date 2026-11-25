"""Pruebas del servicio de ingestión de datos Unity (`GET /data`).

Cubre, sin tocar AWS ni la red real:

    - normalización de la respuesta de `/data` (`extract_records`)
    - correlación por `lens_participant_id`
    - recuperación temporal por INTERSECCIÓN de intervalos (una sesión Unity
      puede comenzar antes de la captura de EmotionLab)
    - conversión de los timestamps locales de Unity con la zona configurada
    - ambigüedad y ausencia de candidato
    - manejo tipado de errores y reintentos ante fallos transitorios

El servicio NUNCA crea `session_id`: solo decide qué registro corresponde.
"""

from __future__ import annotations

import pytest

from services.lens_session_service import (
    LensApiRateLimited,
    LensApiUnavailable,
)
from services.unity_data_service import (
    STATUS_AMBIGUOUS,
    STATUS_MATCHED,
    STATUS_NONE,
    UnityDataError,
    UnityDataService,
    _intersects,
    _to_utc,
)

BASE_URL = "https://lens.example/prod"


# ==========================================================
# DOBLES HTTP
# ==========================================================


class FakeResponse:
    def __init__(self, status_code=200, payload=None, json_error=False):
        self.status_code = status_code
        self._payload = payload
        self._json_error = json_error

    def json(self):
        if self._json_error:
            raise ValueError("cuerpo no es JSON")
        return self._payload


class FakeHttp:
    """Cliente HTTP simulado: devuelve respuestas/peticiones por secuencia."""

    def __init__(self, responses=None, error=None):
        self.responses = list(responses or [])
        self.error = error
        self.calls = []

    def get(self, url, timeout=None):
        self.calls.append({"url": url, "timeout": timeout})
        if self.error is not None:
            exc = self.error
            if isinstance(exc, list):
                exc = exc[min(len(self.calls) - 1, len(exc) - 1)]
            raise exc
        index = min(len(self.calls) - 1, len(self.responses) - 1)
        return self.responses[index]


def lens_record(
    *,
    participant="participant-test-001",
    s3_key="lens/participante_20300115_220926_participant-test-001.json",
    subida="2030-01-16T01:09:26Z",
    fecha_inicio="2030-01-15 22:09:26",
    fecha_fin="2030-01-15 22:20:00",
    respuestas=None,
    eventos=None,
):
    """Registro tal como lo entrega `GET /data`."""

    session_data = {
        "idParticipante": participant,
        "fechaInicio": fecha_inicio,
        "fechaFin": fecha_fin,
        "respuestas": respuestas
        if respuestas is not None
        else [{"idPregunta": "pregunta_demo", "respuestaLabel": "5"}],
        "eventos": eventos
        if eventos is not None
        else [{"tipo": "session_started"}, {"tipo": "session_completed"}],
    }
    return {
        "s3_key": s3_key,
        "timestamp_subida": subida,
        "session_data": session_data,
    }


def service(**kwargs):
    kwargs.setdefault("base_url", BASE_URL)
    kwargs.setdefault("local_timezone", "America/Santiago")
    kwargs.setdefault("http_client", FakeHttp())
    return UnityDataService(**kwargs)


# ==========================================================
# NORMALIZACIÓN DE LA RESPUESTA
# ==========================================================


def test_extract_records_from_list():
    payload = [lens_record(), lens_record(participant="otro")]

    records = UnityDataService.extract_records(payload)

    assert len(records) == 2
    assert records[0]["session_data"]["idParticipante"] == "participant-test-001"


def test_extract_records_from_container():
    payload = {"data": [lens_record()]}

    records = UnityDataService.extract_records(payload)

    assert len(records) == 1
    assert records[0]["s3_key"].endswith("participant-test-001.json")


def test_extract_records_from_single_record_object():
    records = UnityDataService.extract_records(lens_record())

    assert len(records) == 1


def test_extract_records_empty_payload_is_empty_list():
    assert UnityDataService.extract_records(None) == []
    assert UnityDataService.extract_records([]) == []


def test_extract_records_invalid_payload_raises():
    with pytest.raises(UnityDataError):
        UnityDataService.extract_records({"sin": "forma conocida"})


# ==========================================================
# CORRELACIÓN POR PARTICIPANTE
# ==========================================================


def test_select_candidate_by_participant_matched():
    records = [lens_record(participant="A"), lens_record(participant="B")]

    result = service().select_candidate(records, lens_participant_id="B")

    assert result["status"] == STATUS_MATCHED
    assert result["matched_by"] == "participant"
    assert result["candidate"]["session_data"]["idParticipante"] == "B"


def test_select_candidate_by_participant_none():
    records = [lens_record(participant="A")]

    result = service().select_candidate(records, lens_participant_id="Z")

    assert result["status"] == STATUS_NONE
    assert result["candidate"] is None


def test_select_candidate_by_participant_ambiguous():
    records = [lens_record(participant="A"), lens_record(participant="A")]

    result = service().select_candidate(records, lens_participant_id="A")

    assert result["status"] == STATUS_AMBIGUOUS
    assert result["candidate"] is None
    assert len(result["matches"]) == 2


# ==========================================================
# CORRELACIÓN TEMPORAL (INTERSECCIÓN DE INTERVALOS)
# ==========================================================


def test_temporal_match_when_unity_starts_before_capture():
    """La sesión Unity comienza ANTES de la captura pero se solapa."""

    # Unity local 22:09:26 (Santiago, UTC-3) -> 01:09:26Z del día siguiente.
    record = lens_record(
        fecha_inicio="2030-01-15 22:09:26",
        fecha_fin="2030-01-16 01:20:00",
    )

    result = service().select_candidate(
        [record],
        capture_started_at="2030-01-16T01:15:00Z",
        capture_ended_at="2030-01-16T01:30:00Z",
    )

    assert result["status"] == STATUS_MATCHED
    assert result["matched_by"] == "temporal"
    # El inicio normalizado a UTC se conserva para el registro centralizado.
    assert result["candidate"]["_unity_started_at"] == "2030-01-16T01:09:26+00:00"


def test_temporal_no_match_when_intervals_do_not_overlap():
    result = service().select_candidate(
        [lens_record(fecha_inicio="2030-01-15 22:09:26", fecha_fin="2030-01-15 22:20:00")],
        capture_started_at="2030-01-15T13:00:00Z",
        capture_ended_at="2030-01-15T13:45:00Z",
    )

    assert result["status"] == STATUS_NONE


def test_temporal_record_without_end_is_discarded():
    record = lens_record(fecha_fin=None)

    result = service().select_candidate(
        [record],
        capture_started_at="2030-01-15T13:00:00Z",
        capture_ended_at="2030-01-15T13:45:00Z",
    )

    assert result["status"] == STATUS_NONE


def test_temporal_ambiguous_when_two_records_overlap():
    records = [
        lens_record(
            participant="A",
            fecha_inicio="2030-01-15 22:00:00",
            fecha_fin="2030-01-15 22:30:00",
        ),
        lens_record(
            participant="B",
            fecha_inicio="2030-01-15 22:05:00",
            fecha_fin="2030-01-15 22:25:00",
        ),
    ]

    result = service().select_candidate(
        records,
        capture_started_at="2030-01-16T01:15:00Z",
        capture_ended_at="2030-01-16T01:30:00Z",
    )

    assert result["status"] == STATUS_AMBIGUOUS


def test_temporal_uses_local_timezone_for_naive_timestamps():
    """La misma marca local no coincide interpretada como UTC."""

    record = lens_record(
        fecha_inicio="2030-01-15 22:09:26",
        fecha_fin="2030-01-15 22:20:00",
    )
    capture = {
        "capture_started_at": "2030-01-16T01:00:00Z",
        "capture_ended_at": "2030-01-16T01:30:00Z",
    }

    santiago = service(local_timezone="America/Santiago").select_candidate(
        [record], **capture
    )
    utc = service(local_timezone="UTC").select_candidate([record], **capture)

    assert santiago["status"] == STATUS_MATCHED
    assert utc["status"] == STATUS_NONE


def test_temporal_uses_data_ready_at_when_no_end():
    record = lens_record(
        fecha_inicio="2030-01-15 22:09:26",
        fecha_fin="2030-01-15 22:20:00",
    )

    result = service().select_candidate(
        [record],
        capture_started_at="2030-01-16T01:00:00Z",
        data_ready_at="2030-01-16T01:30:00Z",
    )

    assert result["status"] == STATUS_MATCHED


# ==========================================================
# CONSULTA `GET /data`
# ==========================================================


def test_fetch_records_builds_url_and_returns_records():
    http = FakeHttp(responses=[FakeResponse(payload=[lens_record()])])
    svc = service(http_client=http, timeout=9)

    records = svc.fetch_records()

    assert len(records) == 1
    assert http.calls[0]["url"] == BASE_URL + "/data"
    assert http.calls[0]["timeout"] == 9


def test_fetch_records_retries_on_server_error(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda *_: None)
    http = FakeHttp(
        responses=[
            FakeResponse(status_code=500),
            FakeResponse(payload={"records": [lens_record()]}),
        ]
    )
    svc = service(http_client=http, retries=2)

    records = svc.fetch_records()

    assert len(records) == 1
    assert len(http.calls) == 2


def test_fetch_records_raises_rate_limited_on_429():
    http = FakeHttp(responses=[FakeResponse(status_code=429)])
    svc = service(http_client=http)

    with pytest.raises(LensApiRateLimited):
        svc.fetch_records()


def test_fetch_records_raises_unavailable_after_retries(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda *_: None)
    http = FakeHttp(responses=[FakeResponse(status_code=503)])
    svc = service(http_client=http, retries=1)

    with pytest.raises(LensApiUnavailable):
        svc.fetch_records()


def test_fetch_records_raises_on_client_error():
    http = FakeHttp(responses=[FakeResponse(status_code=400)])
    svc = service(http_client=http)

    with pytest.raises(UnityDataError):
        svc.fetch_records()


def test_fetch_records_raises_on_invalid_json():
    http = FakeHttp(responses=[FakeResponse(json_error=True)])
    svc = service(http_client=http)

    with pytest.raises(UnityDataError):
        svc.fetch_records()


def test_fetch_records_without_base_url_raises(monkeypatch):
    monkeypatch.delenv("LENS_API_BASE_URL", raising=False)
    svc = UnityDataService(http_client=FakeHttp())

    with pytest.raises(UnityDataError):
        svc.fetch_records()


# ==========================================================
# HELPERS DE TIEMPO
# ==========================================================


def test_to_utc_parses_z_suffix():
    moment = _to_utc("2030-01-15T13:00:00Z")

    assert moment is not None
    assert moment.isoformat() == "2030-01-15T13:00:00+00:00"


def test_to_utc_returns_none_for_invalid_value():
    assert _to_utc("no-es-fecha") is None
    assert _to_utc(None) is None


def test_intersects_is_inclusive():
    from datetime import datetime, timezone

    a = datetime(2026, 11, 15, 13, 0, tzinfo=timezone.utc)
    b = datetime(2026, 11, 15, 13, 45, tzinfo=timezone.utc)

    assert _intersects(a, b, b, b) is True
    assert _intersects(a, b, a, a) is True


def test_intersects_detects_disjoint_intervals():
    from datetime import datetime, timezone

    a_start = datetime(2026, 11, 15, 13, 0, tzinfo=timezone.utc)
    a_end = datetime(2026, 11, 15, 13, 45, tzinfo=timezone.utc)
    b_start = datetime(2026, 11, 15, 14, 0, tzinfo=timezone.utc)
    b_end = datetime(2026, 11, 15, 14, 30, tzinfo=timezone.utc)

    assert _intersects(a_start, a_end, b_start, b_end) is False
    assert _intersects(b_start, b_end, a_start, a_end) is False
