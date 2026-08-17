"""Punto de entrada y fábrica de la API Flask de EmotionLab."""

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


def _cors_origen_permitido(origin):
    """
    Valida el encabezado ``Origin`` para CORS.

    Orígenes permitidos:
    - Desarrollo local con Vite: http://localhost:5173 / 127.0.0.1:5173
    - Frontend servido con Vite preview: http://localhost:4173 / 127.0.0.1:4173
    - Backend Flask en localhost (healthcheck, tests): http://127.0.0.1:5000 / localhost:5000
    - Electron (file://) → Origin llega como "file://" o como "null" en algunas
      plataformas. Se permite "file://" y se trata el origin nulo/a vacío
      como un origin de Electron cuando la petición viene desde el propio
      equipo (127.0.0.1 / localhost).
    """

    permitidos = {
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:4173",
        "http://127.0.0.1:4173",
        "http://localhost:5000",
        "http://127.0.0.1:5000",
        "file://",
    }

    if origin is None:
        # En Electron con file:// a veces el navegador envía Origin: null
        # u omite el encabezado. Se acepta porque el backend está ligado
        # a 127.0.0.1 y sólo recibe peticiones locales.
        return True

    return origin in permitidos


def create_app(services: dict | None = None) -> Flask:
    """Construye la aplicación Flask."""

    app = Flask(__name__)

    app.extensions["services"] = services or _build_services()

    # Usar el validador dinámico de origins en lugar de una lista fija.
    # No usamos "*" jamás.
    CORS(
        app,
        resources={
            r"/*": {
                "origins": _cors_origen_permitido,
                "always_send": True,
                "methods": ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
                "allow_headers": ["Content-Type", "Authorization"],
                "supports_credentials": False,
                "expose_headers": [],
                "max_age": 3600,
            }
        }
    )

    @app.get("/health")
    def health_check():
        return jsonify({"status": "healthy", "service": "EmotionLabBackend"})

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

    @app.errorhandler(404)
    def route_not_found(_error):
        return jsonify({"error": "Ruta no encontrada"}), 404

    @app.errorhandler(405)
    def method_not_allowed(_error):
        return jsonify({"error": "Método HTTP no permitido"}), 405

    @app.errorhandler(Exception)
    def unhandled_exception(error):
        # En marcha blanca nunca exponer detalles internos al cliente.
        # Se loguea a través del logger por defecto de Flask.
        app.logger.exception("Error no manejado: %s", error)
        return (
            jsonify({"error": "Ocurrió un error interno en el servidor."}),
            500,
        )


app = create_app()


if __name__ == "__main__":
    app.run(
        host="127.0.0.1",
        port=5000,
        debug=False,
        use_reloader=False,
    )