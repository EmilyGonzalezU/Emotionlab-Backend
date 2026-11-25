"""
AWS Lambda handler para EmotionLab.

API Gateway -> Lambda -> Mangum -> a2wsgi -> Flask
"""

from __future__ import annotations

from a2wsgi import WSGIMiddleware
from mangum import Mangum

from app import app

asgi_app = WSGIMiddleware(app)
handler = Mangum(asgi_app, lifespan="off")


def lambda_handler(event, context):
    return handler(event, context)
