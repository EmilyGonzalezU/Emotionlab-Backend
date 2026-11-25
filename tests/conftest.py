"""Keep the test suite independent of Cognito's public JWKS endpoint."""

from services.cognito_service import CognitoService

def _offline_jwks(self):
    return []


# CognitoService fetches JWKS during initialization. No test here requires the
# live key set; token validation tests provide controlled keys/claims instead.
CognitoService._fetch_jwks = _offline_jwks
