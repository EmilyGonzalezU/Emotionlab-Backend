from dotenv import load_dotenv
from flask import Flask, jsonify
from flask_cors import CORS

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
    """Construye la aplicación Flask."""

    app = Flask(__name__)

    app.extensions["services"] = services or _build_services()

    # Configuración limpia de CORS permitiendo orígenes de desarrollo local
    CORS(
        app,
        resources={
            r"/*": {
                "origins": [
                    "http://localhost:5173",
                    "http://127.0.0.1:5173",
                    "http://localhost:3000",
                    "http://127.0.0.1:3000",
                ],
                "methods": ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
                "allow_headers": ["Content-Type", "Authorization", "Access-Control-Allow-Origin"],
                "supports_credentials": True,
            }
        },
    )

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

    # Manejo seguro para instanciar AWS solo si las claves existen en el .env
    aws_service_instance = None
    try:
        aws_service_instance = AWSService()
    except Exception as err:
        print(f"⚠️ AWSService no inicializado (Modo Solo-Firebase activo): {err}")

    return {
        "crypto_service": crypto_service,
        "aws_service": aws_service_instance,
        "user_service": user_service,
        "auth_service": AuthService(
            user_service,
            crypto_service,
        ),
        "session_service": session_service,
        "prediction_service": prediction_service,
        "import_service": ImportService(
            session_service,
            prediction_service=prediction_service,
        ),
        "dashboard_service": DashboardService(
            session_service
        ),
        "report_service": ReportService(
            session_service
        ),
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


app = create_app()


if __name__ == "__main__":
    app.run(
        host="127.0.0.1",
        port=5000,
        debug=True,
        use_reloader=False,
    )