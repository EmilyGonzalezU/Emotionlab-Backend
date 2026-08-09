"""Configuracion centralizada para los tokens JWT."""

import os
from datetime import timedelta

JWT_SECRET = os.getenv("JWT_SECRET")
if not JWT_SECRET:
    raise RuntimeError("Debe definir la variable de entorno JWT_SECRET.")
JWT_ALGORITHM = "HS256"
JWT_EXPIRATION = timedelta(hours=8)
