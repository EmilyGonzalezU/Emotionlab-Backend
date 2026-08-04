"""Punto de entrada y fabrica de la API Flask de EmotionLab."""

from dotenv import load_dotenv
from flask import Flask, jsonify

load_dotenv()

from core.exceptions import (
    AuthenticationError,
    AuthorizationError,
    InvalidCredentials,
    SessionNotFound,
    UserNotFound,
)
from routes.auth_routes import auth_bp
from routes.dashboard_routes import dashboard_bp
from routes.import_routes import imports_bp
from routes.report_routes import reports_bp
from routes.session_routes import sessions_bp
from routes.user_routes import users_bp


def create_app(services: dict | None = None) -> Flask:
    """Construye la aplicacion; ``services`` permite pruebas sin Firebase real."""
    app = Flask(__name__)
    app.extensions["services"] = services or _build_services()

    @app.get("/health")
    def health():
        return jsonify({"status": "healthy"})

    _register_error_handlers(app)
    for blueprint in (
        auth_bp,
        users_bp,
        sessions_bp,
        dashboard_bp,
        reports_bp,
        imports_bp,
    ):
        app.register_blueprint(blueprint)
    return app


def _build_services() -> dict:
    from services.auth_service import AuthService
    from services.aws_service import AWSService
    from services.crypto_service import CryptoService
    from services.dashboard_service import DashboardService
    from services.import_service import ImportService
    from services.prediction_service import PredictionService
    from services.report_service import ReportService
    from services.session_service import SessionService
    from services.user_service import UserService

    crypto_service = CryptoService()
    user_service = UserService()
    session_service = SessionService()
    prediction_service = PredictionService()
    return {
        "crypto_service": crypto_service,
        "aws_service": AWSService(),
        "user_service": user_service,
        "auth_service": AuthService(user_service, crypto_service),
        "session_service": session_service,
        "prediction_service": prediction_service,
        "import_service": ImportService(
            session_service, prediction_service=prediction_service
        ),
        "dashboard_service": DashboardService(session_service),
        "report_service": ReportService(session_service),
    }


def _register_error_handlers(app: Flask) -> None:
    @app.errorhandler(InvalidCredentials)
    @app.errorhandler(AuthenticationError)
    def authentication_error(error):
        return jsonify({"error": str(error)}), 401

    @app.errorhandler(AuthorizationError)
    def authorization_error(error):
        return jsonify({"error": str(error)}), 403

    @app.errorhandler(UserNotFound)
    @app.errorhandler(SessionNotFound)
    def not_found(error):
        return jsonify({"error": str(error)}), 404

    @app.errorhandler(ValueError)
    def invalid_request(error):
        return jsonify({"error": str(error)}), 400


if __name__ == "__main__":
    create_app().run(debug=True)
