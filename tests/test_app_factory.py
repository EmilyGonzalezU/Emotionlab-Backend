"""Tests básicos de create_app() sin AWS real.

Verifica:
  * La fábrica de aplicaciones se puede instanciar.
  * Se registran TODOS los blueprints (incluido el nuevo report_requests_bp).
  * El endpoint /health devuelve stage=ETAPA_2_AWS_MIGRATION.
  * Los servicios se encuentran en app.extensions["services"].
"""

from __future__ import annotations

import sys
import warnings
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture
def client():
    """Cliente Flask test sin AWS real. Advertencia es esperada por servicios no config."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        from app import create_app
        app = create_app()
        app.config["TESTING"] = True
        with app.test_client() as c:
            yield c


def test_app_factory_instantiation(client):  # noqa: ANN001
    assert client is not None


def test_health_check_endpoint(client):  # noqa: ANN001
    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["status"] == "healthy"
    assert data.get("stage") == "ETAPA_2_AWS_MIGRATION"
    assert data.get("storage_backend") in {"s3", "dynamodb"}
    assert "services_loaded" in data
    assert data["services_loaded"]["has_audit_service"] is True
    if data["storage_backend"] == "s3":
        assert data["services_loaded"]["has_dynamodb_service"] is False
    else:
        assert data["services_loaded"]["has_dynamodb_service"] is True


def test_all_blueprints_registered(client):  # noqa: ANN001
    app = client.application
    rules = {str(r) for r in app.url_map.iter_rules()}
    # Auth
    assert any("/auth/login" in r for r in rules)
    assert any("/auth/confirm-new-password" in r for r in rules)
    # Users
    assert any("/users/me" in r for r in rules)
    # Sessions
    assert any("/sessions" in r for r in rules)
    # Participants
    assert any("/participants" in r for r in rules)
    # Dashboard
    assert any("/dashboard" in r for r in rules)
    # Report requests (NUEVO ETAPA 2)
    assert any("/report-requests" in r for r in rules)
    # Reports legacy
    assert any("/reports" in r for r in rules)
    # Imports
    assert any("/imports" in r for r in rules)
    # Ciclo de captura (NUEVO)
    assert any("/captures" in r for r in rules)


def test_all_services_in_extensions(client):  # noqa: ANN001
    app = client.application
    services = app.extensions["services"]
    expected = (
        "dynamodb_service",
        "audit_service",
        "crypto_service",
        "aws_service",
        "user_service",
        "auth_service",
        "session_service",
        "capture_service",
        "participant_service",
        "emotion_v2_service",
        "import_service",
        "dashboard_service",
        "report_service",
        "report_request_service",
    )
    for svc in expected:
        assert svc in services, f"Falta servicio registrado en app.extensions: {svc}"
