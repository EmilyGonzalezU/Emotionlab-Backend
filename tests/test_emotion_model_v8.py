import json
import pytest

from services.emotion_model_v8_package import MODEL_DIR, verify_package
from services.emotion_model_v8_service import EmotionModelV8Service
from services.parsers.emotion_v8_parser import EmotionV8Parser


def _full_record():
    mapping = json.loads((MODEL_DIR / "mapeo_unity_ml_v1.json").read_text(encoding="utf-8"))
    answers = []
    for i, item in enumerate(mapping["equivalencias"], 1):
        answers.append({
            "escena": item["escena"],
            "idPregunta": item["idPregunta"],
            "respuestaLabel": str((i % 9) + 1),
            "respuestaIndex": 0,
        })
    return {
        "session_id": "session-v8-001",
        "session_data": {"idParticipante": "participant-1", "respuestas": answers},
    }


def test_v8_package_manifest_matches_every_file():
    manifest = verify_package()
    assert len(manifest) == 11
    metadata = json.loads((MODEL_DIR / "metadatos_v1.json").read_text(encoding="utf-8"))
    assert metadata["version_modelo"] == "1.0.0-experimental"
    assert metadata["algoritmo"] == "KMeans"
    assert metadata["numero_grupos"] == 2
    assert metadata["numero_variables"] == 12


def test_official_unity_adapter_extracts_12_ordered_labels_not_indexes():
    parsed = EmotionV8Parser(_full_record())
    values = parsed.get_model_values()
    config = json.loads((MODEL_DIR / "configuracion_inferencia_v1.json").read_text(encoding="utf-8"))
    assert list(values) == config["variables"]
    assert len(values) == 12
    assert values["estado_pre"] == 2
    assert set(values.values()) == set(range(1, 10))


def test_official_unity_adapter_preserves_missing_values():
    record = _full_record()
    record["session_data"]["respuestas"].pop(0)
    parsed = EmotionV8Parser(record)
    assert parsed.get_model_values()["estado_pre"] is None
    assert parsed.get_extraction_report()["observadas"] == 11


def test_official_unity_adapter_rejects_duplicate_model_question():
    record = _full_record()
    record["session_data"]["respuestas"].append(
        dict(record["session_data"]["respuestas"][0])
    )
    with pytest.raises(ValueError, match="duplicada"):
        EmotionV8Parser(record)


def test_official_unity_adapter_rejects_out_of_scale_label():
    record = _full_record()
    record["session_data"]["respuestas"][0]["respuestaLabel"] = "10"
    with pytest.raises(ValueError, match="entero entre 1 y 9"):
        EmotionV8Parser(record)


def test_service_keeps_cluster_id_and_marks_tension_index_pending():
    class FakeGenerator:
        def procesar_sesion(self, _record):
            return {
                "version_contrato": "1.1.0-experimental",
                "estado": "calculado_sin_estimacion",
                "id_participante": "participant-1",
                "grupo": 1,
                "observadas": 12,
                "estimadas": [],
                "variables_sin_datos_suficientes": [],
                "variables_originales": {f"v{i}": i for i in range(12)},
                "variables_procesadas": {f"v{i}": i for i in range(12)},
                "error": None,
                "perfil_tension": "menor_tension_relativa",
                "indice_relativo_centroides": None,
            }

    service = EmotionModelV8Service(generator=FakeGenerator())
    result = service.analyze("session-v8-001", _full_record())
    assert result["session_id"] == "session-v8-001"
    assert result["status"] == "analyzed"
    assert result["grupo"] == result["classification_original"] == 1
    assert result["prediction"] == {"group": 1, "n_clusters": 2}
    assert "classification_final" not in result
    assert result["tension_index"]["status"] == "pending_validation"
    assert result["tension_index"]["value"] is None
    assert "wellbeing" not in result


def test_model_loading_refuses_non_v8_python_before_joblib_load(monkeypatch):
    service = EmotionModelV8Service()
    monkeypatch.setattr("services.emotion_model_v8_service.platform.python_version", lambda: "3.12.8")
    with pytest.raises(RuntimeError, match="requiere Python 3.13.18"):
        _ = service.generator
