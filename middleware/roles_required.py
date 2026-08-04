"""Autorizacion de rutas basada en roles."""

from functools import wraps

from flask import g

from core.exceptions import AuthenticationError, AuthorizationError


def roles_required(*allowed_roles: str):
    """Permite ejecutar la vista solo a usuarios con uno de los roles dados."""
    if not allowed_roles:
        raise ValueError("Debe indicar al menos un rol permitido")

    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            user = getattr(g, "current_user", None)
            if not user:
                raise AuthenticationError("Debe autenticarse antes de verificar el rol")
            if user.get("rol") not in allowed_roles:
                raise AuthorizationError("No tiene permisos para esta accion")
            return view(*args, **kwargs)

        return wrapped

    return decorator
