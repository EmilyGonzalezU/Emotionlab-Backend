import inspect
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, jsonify
from flask_cors import CORS


def _load_environment():
    """
    Carga el archivo .env dependiendo de cómo se esté ejecutando EmotionLab.

    Desarrollo:
        Emotionlab-Backend/.env

    Ejecutable PyInstaller:
        resources/
        ├── backend/
        │   └── EmotionLabBackend.exe
        └── config/
            └── .env
    """

    # Directorio donde está app.py en desarrollo
    base_dir = Path(__file__).resolve().parent
    development_env = base_dir / ".env"

    # Cuando estamos ejecutando el EXE generado por PyInstaller
    if getattr(sys, "frozen", False):
        executable_dir = Path(sys.executable).resolve().parent

        # EmotionLab/resources/backend/
        # EmotionLab/resources/config/.env
        packaged_env = executable_dir.parent / "config" / ".env"

        if packaged_env.exists():
            load_dotenv(
                dotenv_path=packaged_env,
                override=False,
            )
            return

    # Ejecución normal desde el proyecto
    if development_env.exists():
        load_dotenv(
            dotenv_path=development_env,
            override=False,
        )
    else:
        # Fallback
        load_dotenv(override=False)


# IMPORTANTE:
# Esto debe ejecutarse antes de importar los servicios/rutas
# que utilizan variables de entorno.
_load_environment()


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
from routes.participant_routes import participants_bp
from routes.report_routes import reports_bp
from routes.report_requests import report_requests_bp
from routes.session_routes import sessions_bp
from routes.user_routes import users_bp
from routes.audit_routes import audit_bp


def _cors_origen_permitido(origin):
    permitidos = set(filter(None, (item.strip() for item in os.getenv("CORS_ALLOWED_ORIGINS", "").split(",")))) or {
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:4173",
        "http://127.0.0.1:4173",
        "http://localhost:5000",
        "http://127.0.0.1:5000",
        "file://",
    }

    if origin is None:
        return True

    return origin in permitidos


def create_app(services: dict | None = None) -> Flask:
    app = Flask(__name__)

    if os.getenv("EMOTIONLAB_LOCAL_DIAGNOSTICS") == "1":
        app.logger.setLevel(logging.DEBUG)
        for handler in app.logger.handlers:
            handler.setLevel(logging.DEBUG)

    app.extensions["services"] = services or _build_services()

    # Diagnóstico de arranque para asegurar que la ruta de autenticación usa la
    # misma implementación de Cognito que fue cargada por este proceso.
    # No registra sesiones, contraseñas ni tokens.
    cognito_service = app.extensions["services"].get("cognito_service")

    if cognito_service is not None:
        cognito_class = type(cognito_service)
        auth_service = app.extensions["services"].get("auth_service")

        app.logger.warning(
            "Cognito auth binding: type=%s module=%s file=%s "
            "respond_to_new_password_challenge=%s same_instance=%s",
            cognito_class.__name__,
            cognito_class.__module__,
            inspect.getfile(cognito_class),
            hasattr(
                cognito_service,
                "respond_to_new_password_challenge",
            ),
            getattr(auth_service, "cognito", None) is cognito_service,
        )

    CORS(
        app,
        resources={
            r"/*": {
                "origins": list(filter(None, (item.strip() for item in os.getenv("CORS_ALLOWED_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173").split(",")))),
                "always_send": True,
                "methods": [
                    "GET",
                    "POST",
                    "PUT",
                    "PATCH",
                    "DELETE",
                    "OPTIONS",
                ],
                "allow_headers": [
                    "Content-Type",
                    "Authorization",
                ],
                "supports_credentials": False,
                "expose_headers": [],
                "max_age": 3600,
            }
        },
        supports_credentials=False,
    )

    @app.get("/health")
    def health_check():
        svc = app.extensions.get("services") or {}
        extras = {}

        for k in (
            "dynamodb_service",
            "cognito_service",
            "s3_service",
            "ses_service",
            "ssm_service",
            "cloudwatch_service",
            "audit_service",
            "report_request_service",
        ):
            extras[f"has_{k}"] = svc.get(k) is not None

        return jsonify(
            {
                "status": "healthy",
                "service": "EmotionLabBackend",
                "stage": "ETAPA_2_AWS_MIGRATION",
                "storage_backend": svc.get(
                    "storage_backend",
                    "dynamodb",
                ),
                "services_loaded": extras,
            }
        )

    _register_error_handlers(app)

    for blueprint in (
        auth_bp,
        users_bp,
        sessions_bp,
        participants_bp,
        dashboard_bp,
        reports_bp,
        report_requests_bp,
        audit_bp,
    ):
        app.register_blueprint(blueprint)

    # Mantener la ruta legacy /import y el contrato plural /imports.
    app.register_blueprint(
        imports_bp,
        url_prefix="/import",
        name="imports_legacy",
    )

    app.register_blueprint(
        imports_bp,
        url_prefix="/imports",
        name="imports_plural",
    )

    return app


def _build_services() -> dict:
    """Construye y enlaza todos los servicios EmotionLab — ETAPA 2 AWS.

    Orden de creación (dependencias):
        1. Servicios base AWS singleton
        2. AuditService
        3. CryptoService
        4. UserService
        5. AuthService
        6. ParticipantService
        7. PredictionService
        8. SessionService
        9. ReportService
        10. ReportRequestService
        11. DashboardService
        12. ImportService
    """

    # ==========================================================
    # 1. SERVICIOS AWS BASE
    # ==========================================================
    from services.storage_repository import (
        DynamoDBRepository,
        S3Repository,
    )

    from services.cognito_service import CognitoService
    from services.s3_service import S3Service
    from services.ses_service import SESService
    from services.ssm_service import SSMService
    from services.cloudwatch_service import CloudWatchService
    from services.audit_service import AuditService

    storage_backend = (
        os.getenv("STORAGE_BACKEND", "dynamodb")
        .strip()
        .lower()
    )

    if storage_backend == "s3":
        storage_repository = S3Repository()
        dynamodb_service = None

    elif storage_backend == "dynamodb":
        from services.dynamodb_service import DynamoDBService

        dynamodb_service = DynamoDBService()
        storage_repository = DynamoDBRepository(
            dynamodb_service
        )

    else:
        raise RuntimeError(
            "STORAGE_BACKEND debe ser 'dynamodb' o 's3'"
        )

    def _safe_build(factory_cls, name: str):
        try:
            return factory_cls()

        except Exception as exc:
            import warnings

            warnings.warn(
                f"[ETAPA2] No se pudo inicializar {name}: {exc}. "
                f"Operaciones relacionadas con {name} fallarán "
                f"hasta configurar AWS.",
                stacklevel=2,
            )

            return None

    ssm_service = _safe_build(
        SSMService,
        "SSMService",
    )

    cognito_service = _safe_build(
        CognitoService,
        "CognitoService",
    )

    s3_service = _safe_build(
        S3Service,
        "S3Service",
    )

    ses_service = _safe_build(
        SESService,
        "SESService",
    )

    cloudwatch_service = _safe_build(
        CloudWatchService,
        "CloudWatchService",
    )

    # ==========================================================
    # 2. AUDIT
    # ==========================================================
    audit_service = AuditService(
        dynamodb_service=storage_repository
    )

    # ==========================================================
    # 3. CRYPTO LEGACY
    # ==========================================================
    from services.crypto_service import CryptoService

    crypto_service = CryptoService()

    # ==========================================================
    # 4. USER / AUTH
    # ==========================================================
    from services.user_service import UserService

    user_service = UserService(
        dynamodb_service=storage_repository,
        cognito_service=cognito_service,
        audit_svc=audit_service,
    )

    from services.auth_service import AuthService

    auth_service = AuthService(
        user_service=user_service,
        cognito_service=cognito_service,
        crypto_service=crypto_service,
        audit_svc=audit_service,
        ses_svc=ses_service,
    )

    # ==========================================================
    # 6. PARTICIPANT
    # ==========================================================
    from services.participant_service import ParticipantService

    participant_service = ParticipantService(
        dynamodb_service=storage_repository
    )

    # ==========================================================
    # 7-8. PREDICTION + SESSION
    # ==========================================================
    from services.prediction_service import PredictionService

    prediction_service = PredictionService()

    from services.aws_service import AWSService

    aws_service = AWSService()

    from services.session_service import SessionService

    session_service = SessionService(
        dynamodb_service=storage_repository,
        s3_service=s3_service,
        audit_svc=audit_service,
        legacy_data_service=aws_service,
    )

    # ==========================================================
    # 9-10. REPORT + REPORT REQUESTS
    # ==========================================================
    from services.report_service import ReportService

    report_service = ReportService(
        session_service=session_service,
        s3_service=s3_service,
    )

    from services.report_request_service import (
        ReportRequestService,
    )

    report_request_service = ReportRequestService(
        dynamodb_service=storage_repository,
        audit_svc=audit_service,
        report_service=report_service,
        session_service=session_service,
        s3_service=s3_service,
        ses_service=ses_service,
    )

    # ==========================================================
    # 11-12. DASHBOARD + IMPORT
    # ==========================================================
    from services.dashboard_service import DashboardService

    dashboard_service = DashboardService(
        session_service=session_service
    )

    from services.import_service import ImportService

    import_service = ImportService(
        session_service=session_service,
        prediction_service=prediction_service,
    )

    return {
        # AWS BASE
        "dynamodb_service": dynamodb_service,
        "storage_repository": storage_repository,
        "storage_backend": storage_backend,
        "cognito_service": cognito_service,
        "s3_service": s3_service,
        "ses_service": ses_service,
        "ssm_service": ssm_service,
        "cloudwatch_service": cloudwatch_service,

        # AUDIT
        "audit_service": audit_service,

        # LEGACY
        "crypto_service": crypto_service,
        "aws_service": aws_service,

        # AUTH / USERS
        "user_service": user_service,
        "auth_service": auth_service,

        # SESIONES / PARTICIPANTES / IA
        "session_service": session_service,
        "participant_service": participant_service,
        "prediction_service": prediction_service,
        "import_service": import_service,
        "dashboard_service": dashboard_service,

        # REPORTES
        "report_service": report_service,
        "report_request_service": report_request_service,
    }


def _register_error_handlers(app: Flask) -> None:

    @app.errorhandler(InvalidCredentials)
    @app.errorhandler(AuthenticationError)
    def authentication_error(error):
        return jsonify(
            {
                "error": "Credenciales inválidas o sesión expirada."
            }
        ), 401

    @app.errorhandler(AuthorizationError)
    def authorization_error(error):
        return jsonify(
            {
                "error": "No tiene permisos para esta acción."
            }
        ), 403

    @app.errorhandler(UserNotFound)
    @app.errorhandler(SessionNotFound)
    def not_found(error):
        return jsonify(
            {
                "error": "El recurso solicitado no existe."
            }
        ), 404

    @app.errorhandler(ValueError)
    def invalid_request(_error):
        return jsonify(
            {
                "error": "La solicitud contiene datos no validos."
            }
        ), 400

    @app.errorhandler(404)
    def route_not_found(_error):
        return jsonify(
            {
                "error": "Ruta no encontrada"
            }
        ), 404

    @app.errorhandler(405)
    def method_not_allowed(_error):
        return jsonify(
            {
                "error": "Método HTTP no permitido"
            }
        ), 405

    @app.errorhandler(Exception)
    def unhandled_exception(error):
        app.logger.error("Error interno no controlado (tipo=%s)", type(error).__name__)

        try:
            cw = app.extensions["services"].get(
                "cloudwatch_service"
            )

            if cw is not None:
                cw.put_event(
                    level="ERROR",
                    message="UNHANDLED_EXCEPTION",
                    context={"exception_type": type(error).__name__},
                )

        except Exception:
            pass

        return (
            jsonify(
                {
                    "error": (
                        "Ocurrió un error interno "
                        "en el servidor."
                    )
                }
            ),
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
