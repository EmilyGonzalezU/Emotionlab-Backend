"""Proteccion de rutas mediante tokens JWT."""

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
        g.current_user = auth_service.get_current_user(token)
        return view(*args, **kwargs)

    return wrapped
