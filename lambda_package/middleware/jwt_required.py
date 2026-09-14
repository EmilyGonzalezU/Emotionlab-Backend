from functools import wraps

from flask import current_app, g, request

from core.exceptions import AuthenticationError


def jwt_required(view):
    """Exige un encabezado ``Authorization: Bearer <token>`` valido."""

    @wraps(view)
    def wrapped(*args, **kwargs):
        authorization = request.headers.get("Authorization", "")
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise AuthenticationError("Se requiere un token Bearer")

        auth_service = current_app.extensions["services"]["auth_service"]

        # Paso 1: Verificar el token (doble pipeline RS256 Cognito -> HS256 legacy)
        #         y normalizar claims a formato comun {sub, email, rol, token_source}
        claims = auth_service.verify_token(token)
        g.current_claims = claims

        # Paso 2: Resolver el usuario completo desde DynamoDB con fallback
        #         automatico _sync_from_cognito_to_dynamo si solo existe en Cognito.
        g.current_user = auth_service.get_current_user(token)

        return view(*args, **kwargs)

    return wrapped
