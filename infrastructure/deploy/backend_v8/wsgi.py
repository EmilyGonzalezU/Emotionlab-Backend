"""WSGI entry point for the ECS/Fargate Gunicorn container."""

from app import create_app

app = create_app()
