"""Tests unitarios del flujo doble verify_token ETAPA 2:
  * Legacy HS256 JWT decode via JWT_SECRET (probado aquí sin AWS).
  * Cognito RS256 no se puede probar sin JWKS público real; se testea
    que el middleware levante AuthenticationError correctamente cuando
    ni RS256 ni HS256 pasan.
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
def legacy_jwt_secret() -> str:
    # A fixed test-only key avoids reading local secrets and meets HS256 length.
    return "offline-test-only-secret-for-emotionlab-32-bytes"


def test_legacy_jwt_creation_and_verify(legacy_jwt_secret):  # noqa: ANN001
    """Creamos un token HS256 legacy y comprobamos que verify_token() lo acepta."""
    try:
        import jwt as pyjwt  # pyjwt <3
        LegacyJWT = pyjwt
    except ImportError:
        pytest.skip("PyJWT no instalado; salto test legacy HS256")

    # 1) Generar token legacy HS256
    payload = {
        "sub": "user-uuid-legacy-1234",
        "email": "legacy.test@example.com",
        "rol": "teacher",
        "nombre": "Profesor de prueba",
    }
    token = LegacyJWT.encode(payload, legacy_jwt_secret, algorithm="HS256")

    # 2) Instanciar AuthService (sin Cognito configurado — debe caer a fallback HS256)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        from services.auth_service import AuthService
        service = AuthService(jwt_secret=legacy_jwt_secret)

    # 3) Verificar
    claims = service.verify_token(token)
    assert claims is not None
    assert claims.get("sub") == payload["sub"]
    assert claims.get("email") == payload["email"]
    assert claims.get("rol") == payload["rol"]
    assert claims.get("token_source") == "legacy_hs256"


def test_invalid_token_raises_auth_error(legacy_jwt_secret):  # noqa: ANN001
    from core.exceptions import AuthenticationError
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        from services.auth_service import AuthService
        service = AuthService(jwt_secret=legacy_jwt_secret)

    with pytest.raises(AuthenticationError):
        service.verify_token("token-malisimo-no-valido-12345")


def test_cognito_user_without_emotionlab_record_is_not_promoted():
    from core.exceptions import AuthenticationError
    from services.auth_service import AuthService

    class Cognito:
        def login(self, _email, _password):
            return {"access_token": "access", "id_token": "id"}

        def verify_token(self, _token):
            return {"sub": "unprovisioned-user", "email": "user@example.org"}

    class Users:
        def get_user(self, _uid):
            return None

        def _sync_from_cognito_to_dynamo(self, _user):
            raise AssertionError("Unknown Cognito accounts must not be auto-provisioned")

    service = object.__new__(AuthService)
    service.cognito = Cognito()
    service.user_service = Users()
    service.audit = None

    with pytest.raises(AuthenticationError, match="no aprovisionado"):
        service.login("user@example.org", "password")
