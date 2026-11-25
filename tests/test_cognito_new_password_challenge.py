"""Tests unitarios para el flow NEW_PASSWORD_REQUIRED -> respond_to_new_password_challenge.

Mocks boto3 cognito-idp para NO requerir credenciales AWS reales.

Secuencia testada:
  1. CognitoService.login(email, temp_pass) retorna challenge NEW_PASSWORD_REQUIRED.
  2. CognitoService.respond_to_new_password_challenge(session, user_id_for_srp, new_pass)
     llama a admin_respond_to_auth_challenge() con ChallengeName="NEW_PASSWORD_REQUIRED"
     y ChallengeResponses={USERNAME, NEW_PASSWORD}.
  3. Respuesta exitosa retorna access_token/id_token/refresh_token compatibles con frontend.
"""

from __future__ import annotations

import sys
import warnings
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture
def cognito_svc(monkeypatch: pytest.MonkeyPatch):
    """Construye CognitoService sin JWKS ni AWS reales. Mockey self.idp."""
    from services.cognito_service import CognitoService

    # Requeridas por __init__
    monkeypatch.setenv("COGNITO_USER_POOL_ID", "us-east-1_fakePoolId")
    monkeypatch.setenv("COGNITO_APP_CLIENT_ID", "fakeclientid123456")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    # Evita request HTTP real por JWKS durante __init__
    monkeypatch.setattr(
        CognitoService,
        "_fetch_jwks",
        lambda self: [],
    )

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        # rompemos singleton solo para este test
        CognitoService._instance = None
        svc = CognitoService()

    # Mock de boto3 client (idp)
    svc.idp = MagicMock()
    # Agregamos las excepciones que espera el código, como atributos del mock
    class _Exc(Exception):
        pass
    class MockExceptions:
        InvalidPasswordException = type("InvalidPasswordException", (Exception,), {})
        ExpiredCodeException = type("ExpiredCodeException", (Exception,), {})
        CodeMismatchException = type("CodeMismatchException", (Exception,), {})
        NotAuthorizedException = type("NotAuthorizedException", (Exception,), {})
        UsernameExistsException = type("UsernameExistsException", (Exception,), {})
        UserNotFoundException = type("UserNotFoundException", (Exception,), {})
    svc.idp.exceptions = MockExceptions()
    return svc


# ------------------------------------------------------------------
# Test 1: login retorna NEW_PASSWORD_REQUIRED challenge con Session y USER_ID_FOR_SRP
# ------------------------------------------------------------------
def test_login_returns_new_password_required_challenge(cognito_svc):
    """AdminInitiateAuth -> ChallengeName NEW_PASSWORD_REQUIRED -> dict esperado por frontend."""
    fake_session = "SESSION-Challenge-MOCK-xyz"
    fake_user_id_for_srp = "e-mail@example.com"

    cognito_svc.idp.admin_initiate_auth.return_value = {
        "ChallengeName": "NEW_PASSWORD_REQUIRED",
        "Session": fake_session,
        "ChallengeParameters": {
            "USER_ID_FOR_SRP": fake_user_id_for_srp,
            "requiredAttributes": "[]",
        },
    }

    result = cognito_svc.login("e-mail@example.com", "TempPass123!")

    assert result["challenge"] == "NEW_PASSWORD_REQUIRED"
    assert result["session"] == fake_session
    assert result["user_id_for_srp"] == fake_user_id_for_srp

    # Verifica params que se enviaron a Cognito
    cognito_svc.idp.admin_initiate_auth.assert_called_once()
    kwargs = cognito_svc.idp.admin_initiate_auth.call_args.kwargs
    assert kwargs["AuthFlow"] == "ADMIN_NO_SRP_AUTH"
    assert kwargs["AuthParameters"]["USERNAME"] == "e-mail@example.com"
    assert kwargs["AuthParameters"]["PASSWORD"] == "TempPass123!"
    assert kwargs["UserPoolId"] == "us-east-1_fakePoolId"
    assert kwargs["ClientId"] == "fakeclientid123456"


# ------------------------------------------------------------------
# Test 2: respond_to_new_password_challenge llama a admin_respond_to_auth_challenge
#         con ChallengeName NEW_PASSWORD_REQUIRED y retorna AccessToken
# ------------------------------------------------------------------
def test_respond_to_new_password_challenge_success(cognito_svc):
    """Caso feliz: respond_to_new_password_challenge envía USERNAME + NEW_PASSWORD
    y recibe tokens. Retorna dict con access_token."""
    fake_session = "SESSION-Challenge-MOCK-xyz"
    fake_user_id_for_srp = "e-mail@example.com"
    new_password = "NuevaClaveSegura123"
    fake_access = "ACCESS_TOKEN_eyJhbGciOiJIUzI1NiJ9"
    fake_id = "ID_TOKEN_xxxx"
    fake_refresh = "REFRESH_TOKEN_yyyy"

    cognito_svc.idp.admin_respond_to_auth_challenge.return_value = {
        "AuthenticationResult": {
            "AccessToken": fake_access,
            "IdToken": fake_id,
            "RefreshToken": fake_refresh,
            "ExpiresIn": 3600,
            "TokenType": "Bearer",
        }
    }

    result = cognito_svc.respond_to_new_password_challenge(
        session=fake_session,
        user_id_for_srp=fake_user_id_for_srp,
        new_password=new_password,
    )

    # Parámetros enviados a boto3
    cognito_svc.idp.admin_respond_to_auth_challenge.assert_called_once()
    kwargs = cognito_svc.idp.admin_respond_to_auth_challenge.call_args.kwargs
    assert kwargs["UserPoolId"] == "us-east-1_fakePoolId"
    assert kwargs["ClientId"] == "fakeclientid123456"
    assert kwargs["ChallengeName"] == "NEW_PASSWORD_REQUIRED"
    assert kwargs["Session"] == fake_session
    assert kwargs["ChallengeResponses"]["USERNAME"] == fake_user_id_for_srp
    assert kwargs["ChallengeResponses"]["NEW_PASSWORD"] == new_password

    # Respuesta normalizada al contrato de frontend
    assert result["access_token"] == fake_access
    assert result["id_token"] == fake_id
    assert result["refresh_token"] == fake_refresh
    assert result["token_type"] == "Bearer"
    assert result["expires_in"] == 3600


# ------------------------------------------------------------------
# Test 3: respond_to_new_password_challenge valida campos vacíos
# ------------------------------------------------------------------
def test_respond_to_new_password_challenge_validates_inputs(cognito_svc):
    from core.exceptions import AuthenticationError

    # session vacío
    with pytest.raises(ValueError):
        cognito_svc.respond_to_new_password_challenge(
            session="", user_id_for_srp="a@b.c", new_password="Password123"
        )

    # user_id_for_srp vacío
    with pytest.raises(ValueError):
        cognito_svc.respond_to_new_password_challenge(
            session="SESS", user_id_for_srp="", new_password="Password123"
        )

    # new_password demasiado corta
    with pytest.raises(ValueError):
        cognito_svc.respond_to_new_password_challenge(
            session="SESS", user_id_for_srp="a@b.c", new_password="abc1234"
        )

    # Nunca se llamó a boto3
    cognito_svc.idp.admin_respond_to_auth_challenge.assert_not_called()


# ------------------------------------------------------------------
# Test 4: Cognito retorna InvalidPassword -> ValueError
# ------------------------------------------------------------------
def test_respond_to_new_password_challenge_invalid_password(cognito_svc):
    class _Err(Exception):
        pass

    # Registramos excepción custom en el mock
    class MockExceptions:
        InvalidPasswordException = type("InvalidPasswordException", (Exception,), {})
        ExpiredCodeException = type("ExpiredCodeException", (Exception,), {})
        CodeMismatchException = type("CodeMismatchException", (Exception,), {})
        NotAuthorizedException = type("NotAuthorizedException", (Exception,), {})

    cognito_svc.idp.exceptions = MockExceptions
    cognito_svc.idp.admin_respond_to_auth_challenge.side_effect = (
        MockExceptions.InvalidPasswordException(
            "Invalid password: password must have uppercase, lower, symbols..."
        )
    )

    with pytest.raises(ValueError):
        cognito_svc.respond_to_new_password_challenge(
            session="SESS",
            user_id_for_srp="a@b.c",
            new_password="NotARealPassword123!",
        )


# ------------------------------------------------------------------
# Test 5: Sesión expirada -> AuthenticationError
# ------------------------------------------------------------------
def test_respond_to_new_password_challenge_expired_session(cognito_svc):
    from core.exceptions import AuthenticationError as AuthErr

    class MockExceptions:
        InvalidPasswordException = type("InvalidPasswordException", (Exception,), {})
        ExpiredCodeException = type("ExpiredCodeException", (Exception,), {})
        CodeMismatchException = type("CodeMismatchException", (Exception,), {})
        NotAuthorizedException = type("NotAuthorizedException", (Exception,), {})

    cognito_svc.idp.exceptions = MockExceptions
    cognito_svc.idp.admin_respond_to_auth_challenge.side_effect = (
        MockExceptions.ExpiredCodeException("Session expired")
    )

    with pytest.raises(AuthErr):
        cognito_svc.respond_to_new_password_challenge(
            session="SESS",
            user_id_for_srp="a@b.c",
            new_password="NotARealPassword123!",
        )


# ------------------------------------------------------------------
# Test 6: confirm_new_password de AuthService orquesta tokens + user
#         (mock completo de dependencias sin AWS ni Dynamo real)
# ------------------------------------------------------------------
def test_auth_service_confirm_new_password_orquestacion(monkeypatch: pytest.MonkeyPatch):
    """Integración mínima: AuthService.confirm_new_password llama a
    cognito.respond_to_new_password_challenge y retorna tokens + user.

    Mocks todo excepto la lógica de orquestación.
    """
    from services.auth_service import AuthService
    from services.cognito_service import CognitoService

    monkeypatch.setenv("COGNITO_USER_POOL_ID", "us-east-1_fakePoolId")
    monkeypatch.setenv("COGNITO_APP_CLIENT_ID", "fakeclientid123456")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    # Evitamos fetch JWKS HTTP real
    monkeypatch.setattr(CognitoService, "_fetch_jwks", lambda self: [])
    CognitoService._instance = None

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        cognito = CognitoService()

    cognito._jwks = []
    cognito.idp = MagicMock()

    # Respuesta fake de respond_to_new_password_challenge -> tokens
    fake_access = "ACCESS_ORQUESTA_OK"
    fake_id = "ID_ORQUESTA_OK"
    cognito.idp.admin_respond_to_auth_challenge.return_value = {
        "AuthenticationResult": {
            "AccessToken": fake_access,
            "IdToken": fake_id,
            "RefreshToken": "REFR_ORQ",
            "ExpiresIn": 3600,
            "TokenType": "Bearer",
        }
    }

    # Mock user_service + verify_token
    user_svc = MagicMock()
    fake_user = {
        "uid": "uuid-cognito-1234",
        "email": "a@b.c",
        "nombre": "Usuario de prueba",
        "rol": "teacher",
        "activo": True,
    }
    user_svc.get_user.return_value = fake_user
    user_svc.get_by_email.return_value = fake_user

    cognito.verify_token = MagicMock(return_value={
        "sub": "uuid-cognito-1234",
        "email": "a@b.c",
        "custom:rol": "teacher",
    })

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        auth = AuthService(
            user_service=user_svc,
            cognito_service=cognito,
            jwt_secret="unit-test-jwt-secret-0123456789-abcdef-012345",
            audit_svc=None,
            ses_svc=None,
        )

    result = auth.confirm_new_password(
        session="SESS-ORQ",
        user_id_for_srp="a@b.c",
        new_password="MyNewPass123",
        email="a@b.c",
    )

    # Contrato frontend: access_token || token
    assert result.get("access_token") == fake_access
    assert result.get("token") == fake_access
    assert result.get("id_token") == fake_id
    assert "user" in result
    assert result["user"]["email"] == "a@b.c"
    assert result["user"]["rol"] == "teacher"
    assert result["user"]["nombre"] == "Usuario de prueba"
    assert "password" not in result["user"]


@pytest.mark.parametrize("stored_role, canonical_role", [("ADMIN", "admin"), ("TEACHER", "teacher")])
def test_auth_service_confirm_new_password_normaliza_roles(
    monkeypatch: pytest.MonkeyPatch,
    stored_role: str,
    canonical_role: str,
):
    from services.auth_service import AuthService

    cognito = MagicMock()
    cognito.respond_to_new_password_challenge.return_value = {
        "access_token": "ACCESS",
        "id_token": "ID",
        "refresh_token": "REFRESH",
    }
    cognito.verify_token.return_value = {"sub": "cognito-sub"}
    user_service = MagicMock()
    user_service.get_user.return_value = {
        "uid": "cognito-sub",
        "email": "admin@example.com",
        "nombre": "Admin de prueba",
        "rol": stored_role,
        "activo": True,
    }

    auth = AuthService(
        user_service=user_service,
        cognito_service=cognito,
        jwt_secret="unit-test-jwt-secret-0123456789-abcdef-012345",
        audit_svc=None,
        ses_svc=None,
    )

    result = auth.confirm_new_password(
        session="SESSION",
        user_id_for_srp="admin@example.com",
        new_password="NotARealPassword123!",
    )

    assert result["user"]["rol"] == canonical_role
    assert result["user"]["uid"] == "cognito-sub"
    user_service.get_user.assert_called_once_with("cognito-sub")


@pytest.mark.parametrize("stored_role, canonical_role", [
    ("profesor", "teacher"),
    ("administrador", "admin"),
])
def test_auth_service_confirm_new_password_normaliza_aliases_legacy(
    stored_role: str,
    canonical_role: str,
):
    from services.auth_service import AuthService

    cognito = MagicMock()
    cognito.respond_to_new_password_challenge.return_value = {
        "access_token": "ACCESS",
        "id_token": "ID",
        "refresh_token": "REFRESH",
    }
    cognito.verify_token.return_value = {"sub": "cognito-sub"}
    user_service = MagicMock()
    user_service.get_user.return_value = {
        "uid": "cognito-sub",
        "email": "admin@example.com",
        "nombre": "Admin de prueba",
        "rol": stored_role,
        "activo": True,
    }

    auth = AuthService(
        user_service=user_service,
        cognito_service=cognito,
        jwt_secret="unit-test-jwt-secret-0123456789-abcdef-012345",
        audit_svc=None,
        ses_svc=None,
    )

    result = auth.confirm_new_password(
        session="SESSION",
        user_id_for_srp="admin@example.com",
        new_password="NotARealPassword123!",
    )

    assert result["user"]["rol"] == canonical_role


def test_auth_service_confirm_new_password_rechaza_usuario_inexistente():
    from services.auth_service import AuthService

    cognito = MagicMock()
    cognito.respond_to_new_password_challenge.return_value = {
        "access_token": "ACCESS",
        "id_token": "ID",
        "refresh_token": "REFRESH",
    }
    cognito.verify_token.return_value = {"sub": "missing-sub"}
    user_service = MagicMock()
    user_service.get_user.return_value = None
    user_service.get_by_email.return_value = None

    auth = AuthService(
        user_service=user_service,
        cognito_service=cognito,
        jwt_secret="unit-test-jwt-secret-0123456789-abcdef-012345",
        audit_svc=None,
        ses_svc=None,
    )

    with pytest.raises(Exception, match="no encontrado en DynamoDB"):
        auth.confirm_new_password(
            session="SESSION",
            user_id_for_srp="admin@example.com",
            new_password="NotARealPassword123!",
            email="admin@example.com",
        )


def test_auth_service_confirm_new_password_rechaza_rol_desconocido():
    from services.auth_service import AuthService

    cognito = MagicMock()
    cognito.respond_to_new_password_challenge.return_value = {
        "access_token": "ACCESS",
        "id_token": "ID",
        "refresh_token": "REFRESH",
    }
    cognito.verify_token.return_value = {"sub": "cognito-sub"}
    user_service = MagicMock()
    user_service.get_user.return_value = {
        "uid": "cognito-sub",
        "email": "admin@example.com",
        "nombre": "Admin de prueba",
        "rol": "owner",
        "activo": True,
    }

    auth = AuthService(
        user_service=user_service,
        cognito_service=cognito,
        jwt_secret="unit-test-jwt-secret-0123456789-abcdef-012345",
        audit_svc=None,
        ses_svc=None,
    )

    with pytest.raises(Exception, match="rol desconocido"):
        auth.confirm_new_password(
            session="SESSION",
            user_id_for_srp="admin@example.com",
            new_password="NotARealPassword123!",
        )


@pytest.mark.parametrize("role_input, canonical_role", [
    ("teacher", "teacher"),
    ("TEACHER", "teacher"),
    ("profesor", "teacher"),
    ("admin", "admin"),
    ("ADMIN", "admin"),
    ("administrador", "admin"),
    ("especialista", "teacher"),
])
def test_user_service_create_user_persists_canonical_role(
    role_input: str,
    canonical_role: str,
):
    from services.user_service import UserService

    cognito = MagicMock()
    cognito.admin_create_user.return_value = {
        "uid": "cognito-sub-123",
        "cognito_username": "cognito-username",
        "user_status": "FORCE_CHANGE_PASSWORD",
        "temporary_password": "TEMP_PASSWORD_REDACTED",
    }
    dynamo = MagicMock()
    dynamo.put_item.return_value = True
    service = UserService(dynamodb_service=dynamo, cognito_service=cognito)

    result = service.create_user(
        uid="ignored-legacy-uid",
        nombre="Usuario de prueba",
        email="test@example.com",
        rol=role_input,
    )

    cognito.admin_create_user.assert_called_once_with(
        email="test@example.com",
        nombre="Usuario de prueba",
        rol=canonical_role,
        legacy_uid=None,
    )
    item = dynamo.put_item.call_args.args[1]
    assert item["uid"] == "cognito-sub-123"
    assert item["rol"] == canonical_role
    assert item["email"] == "test@example.com"
    assert item["nombre"] == "Usuario de prueba"
    assert item["activo"] is True
    assert item["user_status"] == "FORCE_CHANGE_PASSWORD"
    assert item["pk_type"] == "USER"
    assert result["rol"] == canonical_role


def test_cognito_admin_create_user_no_envia_custom_role(cognito_svc):
    cognito_svc.idp.admin_create_user.return_value = {
        "User": {
            "Username": "cognito-username",
            "Enabled": True,
            "UserStatus": "FORCE_CHANGE_PASSWORD",
            "Attributes": [{"Name": "sub", "Value": "cognito-sub-123"}],
        }
    }

    cognito_svc.admin_create_user(
        email="test@example.com",
        nombre="Usuario de prueba",
        rol="admin",
    )

    attributes = cognito_svc.idp.admin_create_user.call_args.kwargs["UserAttributes"]
    kwargs = cognito_svc.idp.admin_create_user.call_args.kwargs
    assert {attribute["Name"] for attribute in attributes} == {
        "email",
        "email_verified",
        "name",
    }
    assert not any(attribute["Name"] == "custom:rol" for attribute in attributes)
    assert kwargs["Username"] == "test@example.com"
    assert "TemporaryPassword" not in kwargs
    assert "MessageAction" not in kwargs
    result = cognito_svc.admin_create_user(
        email="second@example.com",
        nombre="Segundo usuario",
        rol="teacher",
    )
    assert "temporary_password" not in result


def test_user_service_create_user_rolls_back_cognito_if_dynamo_fails():
    from services.user_service import UserService

    cognito = MagicMock()
    cognito.admin_create_user.return_value = {
        "uid": "cognito-sub-123",
        "cognito_username": "cognito-username",
    }
    dynamo = MagicMock()
    dynamo.put_item.side_effect = RuntimeError("DynamoDB unavailable")
    service = UserService(dynamodb_service=dynamo, cognito_service=cognito)

    with pytest.raises(RuntimeError, match="DynamoDB unavailable"):
        service.create_user(
            uid="ignored-legacy-uid",
            nombre="Usuario de prueba",
            email="test@example.com",
            rol="teacher",
        )

    cognito.admin_delete_user.assert_called_once_with("cognito-username")


def test_user_service_log_activity_without_extra(monkeypatch):
    from services.user_service import UserService
    import services.audit_service as audit_module

    audit = MagicMock()
    monkeypatch.setattr(audit_module, "audit_service", lambda: audit)
    service = UserService(dynamodb_service=MagicMock(), cognito_service=MagicMock())

    service.log_activity(
        "actor-uid",
        "Actualización de usuario",
        target_uid="target-uid",
        actor_info={"email": "actor@example.com", "rol": "admin"},
    )

    audit.log_user_updated.assert_called_once_with(
        actor_uid="actor-uid",
        actor_email="actor@example.com",
        actor_rol="admin",
        target_uid="target-uid",
        changes=[],
    )


def test_user_service_log_activity_with_extra(monkeypatch):
    from services.user_service import UserService
    import services.audit_service as audit_module

    audit = MagicMock()
    monkeypatch.setattr(audit_module, "audit_service", lambda: audit)
    service = UserService(dynamodb_service=MagicMock(), cognito_service=MagicMock())

    service.log_activity(
        "actor-uid",
        "Creación de usuario (Cognito + DynamoDB)",
        target_uid="target-uid",
        actor_info={"email": "actor@example.com", "rol": "admin"},
        extra={"creado_en_cognito": True},
    )

    audit.log_user_created.assert_called_once_with(
        actor_uid="actor-uid",
        actor_email="actor@example.com",
        actor_rol="admin",
        target_uid="target-uid",
        target_email="",
        target_rol="",
        creado_en_cognito=True,
    )


def test_user_service_log_activity_creation_registers_audit(monkeypatch):
    from services.user_service import UserService
    import services.audit_service as audit_module

    audit = MagicMock()
    monkeypatch.setattr(audit_module, "audit_service", lambda: audit)
    service = UserService(dynamodb_service=MagicMock(), cognito_service=MagicMock())
    service.get_user = MagicMock(return_value={
        "uid": "target-uid",
        "email": "target@example.com",
        "rol": "teacher",
    })

    service.log_activity(
        "actor-uid",
        "Creación de usuario (Cognito + DynamoDB)",
        target_uid="target-uid",
        actor_info={"email": "actor@example.com", "rol": "admin"},
        extra={"origen": "users_route"},
    )

    audit.log_user_created.assert_called_once_with(
        actor_uid="actor-uid",
        actor_email="actor@example.com",
        actor_rol="admin",
        target_uid="target-uid",
        target_email="target@example.com",
        target_rol="teacher",
        origen="users_route",
    )


def test_user_service_log_activity_deletion_registers_audit(monkeypatch):
    from services.user_service import UserService
    import services.audit_service as audit_module

    audit = MagicMock()
    monkeypatch.setattr(audit_module, "audit_service", lambda: audit)
    service = UserService(dynamodb_service=MagicMock(), cognito_service=MagicMock())

    service.log_activity(
        "actor-uid",
        "Cuenta eliminada",
        target_uid="target-uid",
        actor_info={"email": "actor@example.com", "rol": "admin"},
    )

    audit.log_user_deleted.assert_called_once_with(
        actor_uid="actor-uid",
        actor_email="actor@example.com",
        actor_rol="admin",
        target_uid="target-uid",
        target_email="",
    )


def test_user_service_log_activity_handles_eventual_missing_target(monkeypatch):
    from services.user_service import UserService
    import services.audit_service as audit_module

    audit = MagicMock()
    monkeypatch.setattr(audit_module, "audit_service", lambda: audit)
    service = UserService(dynamodb_service=MagicMock(), cognito_service=MagicMock())
    service.get_user = MagicMock(return_value=None)

    service.log_activity(
        "actor-uid",
        "Creación de usuario (Cognito + DynamoDB)",
        target_uid="cognito-sub-123",
        actor_info={"email": "actor@example.com", "rol": "admin"},
    )

    audit.log_user_created.assert_called_once_with(
        actor_uid="actor-uid",
        actor_email="actor@example.com",
        actor_rol="admin",
        target_uid="cognito-sub-123",
        target_email="",
        target_rol="",
    )
