import pytest

from core.exceptions import AuthenticationError
from services.cognito_service import CognitoService


@pytest.fixture
def cognito(monkeypatch):
    service = object.__new__(CognitoService)
    service.region = "us-east-1"
    service.user_pool_id = "us-east-1_example"
    service.app_client_id = "app-client-id"
    service._jwks = [{"kid": "test-key", "alg": "RS256"}]
    monkeypatch.setattr(service, "_get_key", lambda _kid: object())
    return service


def test_access_token_is_validated_against_client_id(cognito, monkeypatch):
    monkeypatch.setattr(
        "services.cognito_service.jose_jwt.get_unverified_header",
        lambda _token: {"kid": "test-key", "alg": "RS256"},
    )
    monkeypatch.setattr(
        "services.cognito_service.jose_jwt.decode",
        lambda *_args, **_kwargs: {
            "token_use": "access",
            "client_id": "app-client-id",
            "iss": "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_example",
        },
    )

    claims = cognito.verify_token("access-token", token_use="access")

    assert claims["token_use"] == "access"


@pytest.mark.parametrize(
    "claims",
    [
        {"token_use": "access", "client_id": "wrong-client"},
        {"token_use": "id", "aud": "wrong-client"},
        {"token_use": "refresh", "client_id": "app-client-id"},
    ],
)
def test_cognito_rejects_wrong_client_or_token_type(cognito, monkeypatch, claims):
    monkeypatch.setattr(
        "services.cognito_service.jose_jwt.get_unverified_header",
        lambda _token: {"kid": "test-key", "alg": "RS256"},
    )
    monkeypatch.setattr(
        "services.cognito_service.jose_jwt.decode",
        lambda *_args, **_kwargs: claims,
    )

    with pytest.raises(AuthenticationError):
        cognito.verify_token("cognito-token")


def test_id_token_is_validated_against_audience(cognito, monkeypatch):
    monkeypatch.setattr(
        "services.cognito_service.jose_jwt.get_unverified_header",
        lambda _token: {"kid": "test-key", "alg": "RS256"},
    )
    monkeypatch.setattr(
        "services.cognito_service.jose_jwt.decode",
        lambda *_args, **_kwargs: {"token_use": "id", "aud": "app-client-id"},
    )

    assert cognito.verify_token("id-token")["token_use"] == "id"


def test_cognito_rejects_non_rs256_algorithm(cognito, monkeypatch):
    monkeypatch.setattr(
        "services.cognito_service.jose_jwt.get_unverified_header",
        lambda _token: {"kid": "test-key", "alg": "HS256"},
    )

    with pytest.raises(AuthenticationError, match="RS256"):
        cognito.verify_token("wrong-algorithm-token")
