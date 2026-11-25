import inspect
import logging
import os
import sys
from pathlib import Path

# A direct `python app.py` launch should use the frozen V8 runtime when the
# repository's local Micromamba environment exists. Keep imports below this
# bootstrap so a system Python with incompatible scientific packages never
# initializes the application.
if __name__ == "__main__":
    _repo_root = Path(__file__).resolve().parent
    _runtime_candidates = (
        _repo_root / ".venv-v8" / "python.exe",
        _repo_root / ".venv-v8" / "Scripts" / "python.exe",
        _repo_root / ".venv-v8" / "bin" / "python",
    )
    _v8_python = next((path for path in _runtime_candidates if path.is_file()), None)
    if _v8_python is not None and _v8_python.resolve() != Path(sys.executable).resolve():
        os.execv(
            str(_v8_python),
            [str(_v8_python), str(Path(__file__).resolve()), *sys.argv[1:]],
        )
    if sys.version_info[:3] != (3, 13, 18):
        raise SystemExit(
            "EmotionLab V8 requiere Python 3.13.18. Instala el entorno .venv-v8 "
            "o ejecútalo con ese intérprete."
        )

from dotenv import dotenv_values, load_dotenv
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
    selected_env = development_env

    # Cuando estamos ejecutando el EXE generado por PyInstaller
    if getattr(sys, "frozen", False):
        executable_dir = Path(sys.executable).resolve().parent

        # EmotionLab/resources/backend/
        # EmotionLab/resources/config/.env
        packaged_env = executable_dir.parent / "config" / ".env"

        if packaged_env.exists():
            selected_env = packaged_env

    # Ejecución normal desde el proyecto
    if selected_env.exists():
        configured_bucket = (dotenv_values(selected_env).get("S3_REPORT_PDFS_BUCKET") or "").strip()
        inherited_bucket_raw = os.getenv("S3_REPORT_PDFS_BUCKET")
        inherited_bucket = os.getenv("S3_REPORT_PDFS_BUCKET", "").strip()
        if inherited_bucket_raw is not None and inherited_bucket != configured_bucket:
            logging.warning(
                "S3_REPORT_PDFS_BUCKET del entorno de lanzamiento sobrescribe el valor de %s. "
                "Bucket efectivo del entorno: %s",
                selected_env,
                inherited_bucket or "(vacío)",
            )
        load_dotenv(dotenv_path=selected_env, override=False)
        if inherited_bucket_raw is not None:
            bucket_source = "entorno del proceso"
        elif configured_bucket:
            bucket_source = f"dotenv:{selected_env.resolve()}"
        else:
            bucket_source = "sin configuración"
    else:
        # Fallback de dotenv conserva la prioridad del entorno del proceso.
        load_dotenv(override=False)
        bucket_source = (
            "entorno del proceso"
            if os.getenv("S3_REPORT_PDFS_BUCKET") is not None
            else "sin configuración"
        )

    # Provenance is intentionally limited to a non-secret diagnostic label.
    os.environ["EMOTIONLAB_S3_REPORT_BUCKET_SOURCE"] = bucket_source


# IMPORTANTE:
# Esto debe ejecutarse antes de importar los servicios/rutas
# que utilizan variables de entorno.
_load_environment()

# Direct local execution means local V8 inference unless the caller explicitly
# selected a different V8 mode in the process or .env. A legacy V2 remote mode
# must not disable the current local V8 package.
if __name__ == "__main__":
    os.environ.setdefault("EMOTION_MODEL_V8_MODE", "local")


from botocore.exceptions import ClientError

from core.aws_error_logging import log_aws_client_error
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
from routes.tracking_routes import trackings_bp
from routes.session_code_routes import session_codes_bp
from routes.capture_routes import captures_bp


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
        trackings_bp,
        session_codes_bp,
        captures_bp,
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


def _build_emotion_v8_engine():
    """Construye el motor V8 local o su cliente remoto Lambda."""

    legacy_mode = os.getenv("EMOTION_MODEL_V2_MODE", "").strip().lower()
    mode = os.getenv("EMOTION_MODEL_V8_MODE", "").strip().lower()

    if not mode:
        if legacy_mode and legacy_mode != "local":
            from services.emotion_model_v8_service import EmotionModelV8UnavailableService

            reason = (
                "La configuraci?n remota existente usa el contrato V2. "
                "V8 requiere configurar expl?citamente EMOTION_MODEL_V8_MODE."
            )
            logging.getLogger(__name__).error(reason)
            return EmotionModelV8UnavailableService(reason)
        mode = "local"

    if mode == "local":
        from services.emotion_model_v8_service import EmotionModelV8Service
        return EmotionModelV8Service()

    if mode == "lambda":
        from services.emotion_model_v8_lambda_client import EmotionModelV8LambdaClient
        return EmotionModelV8LambdaClient()

    from services.emotion_model_v8_service import EmotionModelV8UnavailableService

    reason = (
        f"EMOTION_MODEL_V8_MODE no reconocido: {mode!r}. "
        "Valores admitidos: local, lambda."
    )
    logging.getLogger(__name__).error(reason)
    return EmotionModelV8UnavailableService(reason)


def _build_services() -> dict:
    """Construye y enlaza todos los servicios EmotionLab — ETAPA 2 AWS.

    Orden de creación (dependencias):
        1. Servicios base AWS singleton
        2. AuditService
        3. CryptoService
        4. UserService
        5. AuthService
        6. ParticipantService
        7. SessionService
        8. ReportService
        9. ReportRequestService
        10. DashboardService
        11. ImportService
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
    # 7-8. SESSION
    # ==========================================================
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
    # SEGUIMIENTO SEUDONIMIZADO
    # ==========================================================
    from services.tracking_service import TrackingService

    tracking_service = (
        TrackingService(
            dynamodb_service=dynamodb_service,
            session_service=session_service,
            audit_svc=audit_service,
        )
        if dynamodb_service is not None
        else None
    )

    # ==========================================================
    # IDENTIFICACIÓN DE SESIONES DEL LENTE (displayCode)
    # ==========================================================
    from services.lens_session_service import LensSessionService

    lens_session_service = (
        LensSessionService(
            dynamodb_service=dynamodb_service,
            audit_svc=audit_service,
            ssm_service=ssm_service,
        )
        if dynamodb_service is not None
        else None
    )

    # ==========================================================
    # INGESTIÓN DE DATOS UNITY (GET /data del API Gateway del lente)
    # ==========================================================
    from services.unity_data_service import UnityDataService

    unity_data_service = UnityDataService()

    # ==========================================================
    # MODELO V8 CONGELADO (K-MEANS) — ÚNICO motor activo
    # ==========================================================
    # La instancia valida el manifiesto; la carga Joblib es perezosa y exige
    # el runtime V8 exacto antes de abrir los artefactos.
    emotion_v2_service = _build_emotion_v8_engine()

    # ==========================================================
    # CICLO DE CAPTURA (EXPERIENCIA DE EMOTIONLAB)
    # ==========================================================
    from services.capture_service import CaptureService

    capture_service = (
        CaptureService(
            session_service=session_service,
            lens_session_service=lens_session_service,
            audit_svc=audit_service,
            aws_service=aws_service,
            s3_service=s3_service,
            unity_data_service=unity_data_service,
            emotion_v2_service=emotion_v2_service,
        )
        if dynamodb_service is not None
        else None
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
        tracking_service=tracking_service,
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
        "tracking_service": tracking_service,
        "lens_session_service": lens_session_service,
        "unity_data_service": unity_data_service,
        "capture_service": capture_service,
        "participant_service": participant_service,
        "emotion_v2_service": emotion_v2_service,
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
        if isinstance(error, ClientError):
            # Diagnóstico seguro: servicio/operación/código/mensaje/request id
            # y módulo de origen. Conserva el traceback para el log local.
            log_aws_client_error(app.logger, error)
        else:
            app.logger.error(
                "Error interno no controlado (tipo=%s)",
                type(error).__name__,
            )

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
