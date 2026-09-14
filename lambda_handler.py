"""
AWS Lambda handler para EmotionLab.

Flujo:
API Gateway
    ↓
AWS Lambda
    ↓
lambda_handler
    ↓
Flask (create_app)
    ↓
Routes / Services
"""

from __future__ import annotations

import os

from a2wsgi import WSGIMiddleware
from mangum import Mangum

from app import create_app


# ---------------------------------------------------------------------------
# Crear la aplicación Flask una sola vez.
#
# Lambda puede reutilizar el mismo entorno entre invocaciones, por lo que
# mantener la aplicación fuera del handler evita reconstruirla innecesariamente.
# ---------------------------------------------------------------------------
app = create_app()


# ---------------------------------------------------------------------------
# Adaptadores:
#
# Flask utiliza WSGI.
# a2wsgi convierte WSGI → ASGI.
# Mangum convierte ASGI → evento/respuesta de API Gateway.
# ---------------------------------------------------------------------------
asgi_app = WSGIMiddleware(app)

handler = Mangum(asgi_app, lifespan="off")


# ---------------------------------------------------------------------------
# Punto de entrada de AWS Lambda
# ---------------------------------------------------------------------------
def lambda_handler(event, context):
    return handler(event, context)