import os

COLLECTION_USERS = "users"
COLLECTION_SESSIONS = "sessions"
COLLECTION_PARTICIPANTS = "participants"
COLLECTION_USER_ACTIVITY = "user_activity"

# ---------------------------------------------------------------------
# Nombres de tablas DynamoDB (configurables via variables de entorno)
# ---------------------------------------------------------------------

DYNAMODB_TABLE_USERS = os.getenv(
    "DYNAMODB_USERS_TABLE",
    "emotionlab-dev-users",
)
DYNAMODB_TABLE_PARTICIPANTS = os.getenv(
    "DYNAMODB_PARTICIPANTS_TABLE",
    "emotionlab-dev-participants",
)
DYNAMODB_TABLE_SESSIONS = os.getenv(
    "DYNAMODB_SESSIONS_TABLE",
    "emotionlab-dev-sessions",
)
DYNAMODB_TABLE_AUDIT_LOGS = os.getenv(
    "DYNAMODB_AUDIT_LOGS_TABLE",
    "emotionlab-dev-audit-logs",
)
DYNAMODB_TABLE_REPORT_REQUESTS = os.getenv(
    "DYNAMODB_REPORT_REQUESTS_TABLE",
    "emotionlab-dev-report-requests",
)

DYNAMODB_TABLE_TRACKINGS = os.getenv(
    "DYNAMODB_TRACKINGS_TABLE",
    "emotionlab-dev-trackings",
)

# ---------------------------------------------------------------------
# Tablas legacy del lente (administradas FUERA de este repositorio).
# El flujo vigente usa LENS_API_BASE_URL por HTTPS; el backend no consulta
# estas tablas. Se conservan como constantes por compatibilidad histórica.
#
# La Lambda `createSession`, invocada por el APK del lente, escribe la
# sesión en una tabla propia y reserva el `displayCode` de 6 dígitos en
# una segunda tabla. EmotionLab solo las consulta (lectura) para
# localizar el ciclo de captura a partir del código mostrado.
#
# CONTRATO HISTÓRICO (no utilizado por el flujo vigente):
#
#   DYNAMODB_LENS_SESSIONS_TABLE
#       clave primaria  : sessionId
#       atributos leídos: sessionId, status, createdAt
#                         (la tabla contiene además `sessionToken` y
#                          `displayCode`; el token nunca se devuelve)
#
#   DYNAMODB_LENS_DISPLAY_CODES_TABLE
#       clave primaria  : displayCode
#       atributos leídos: sessionId, expires_at
#
# Los valores se mantienen solo para consumidores legacy. No conceder
# permisos DynamoDB externos a estas tablas para el backend actual.
# ---------------------------------------------------------------------

DYNAMODB_LENS_SESSIONS_TABLE = os.getenv(
    "DYNAMODB_LENS_SESSIONS_TABLE",
    "emotionlab-sessions",
)

DYNAMODB_LENS_DISPLAY_CODES_TABLE = os.getenv(
    "DYNAMODB_LENS_DISPLAY_CODES_TABLE",
    "emotionlab-session-display-codes",
)

# ---------------------------------------------------------------------
# Limitador de intentos fallidos del displayCode (propiedad de EmotionLab).
#
# Es independiente de las tablas del lente. Protege tanto la resolución
# compatible como el inicio manual de capturas en EmotionLab.
# ---------------------------------------------------------------------

DYNAMODB_SESSION_CODE_RATE_LIMITS = os.getenv(
    "DYNAMODB_SESSION_CODE_RATE_LIMITS_TABLE",
    "emotionlab-dev-session-code-rate-limits",
)

# ---------------------------------------------------------------------
# Valores prefijados / literal para SK y GSIs
# ---------------------------------------------------------------------

PARTICIPANT_UNASSIGNED = "UNASSIGNED"
PK_TYPE_USER = "USER"
PK_TYPE_PARTICIPANT = "PARTICIPANT"
PK_TYPE_SESSION = "SESSION"
PK_TYPE_AUDIT_LOG = "AUDIT_LOG"
PK_TYPE_REPORT_REQUEST = "REPORT_REQUEST"
# Marca técnica del "slot" de captura: garantiza, de forma atómica, que
# exista una sola experiencia activa GLOBAL. No es una sesión y por eso los
# listados de sesiones lo ignoran.
PK_TYPE_CAPTURE_LOCK = "CAPTURE_LOCK"

# ---------------------------------------------------------------------
# Acciones de auditoria estandarizadas
# ---------------------------------------------------------------------

AUDIT_LOGIN_OK = "LOGIN_OK"
AUDIT_LOGIN_FAIL = "LOGIN_FAIL"
AUDIT_USER_CREATED = "USER_CREATED"
AUDIT_USER_UPDATED = "USER_UPDATED"
AUDIT_USER_DELETED = "USER_DELETED"
AUDIT_USER_ENABLED = "USER_ENABLED"
AUDIT_USER_DISABLED = "USER_DISABLED"
AUDIT_PASSWORD_CHANGED = "PASSWORD_CHANGED"
AUDIT_PASSWORD_RESET = "PASSWORD_RESET"
AUDIT_SESSIONS_IMPORTED = "SESSIONS_IMPORTED"
AUDIT_PARTICIPANT_CREATED = "PARTICIPANT_CREATED"
AUDIT_SESSION_ASSIGNED = "SESSION_ASSIGNED"
AUDIT_TRACKING_CREATED = "TRACKING_CREATED"
AUDIT_TRACKING_SESSION_ADDED = "TRACKING_SESSION_ADDED"
AUDIT_TRACKING_SESSION_REMOVED = "TRACKING_SESSION_REMOVED"
AUDIT_TRACKING_DELETED = "TRACKING_DELETED"
AUDIT_SESSION_CODE_RESOLVED = "SESSION_CODE_RESOLVED"
AUDIT_SESSION_CODE_RESOLVE_FAILED = "SESSION_CODE_RESOLVE_FAILED"
AUDIT_CAPTURE_STARTED = "CAPTURE_STARTED"
AUDIT_CAPTURE_PIN_REJECTED = "CAPTURE_PIN_REJECTED"
AUDIT_CAPTURE_RECORD_LINKED = "CAPTURE_RECORD_LINKED"
AUDIT_CAPTURE_RECORD_REJECTED = "CAPTURE_RECORD_REJECTED"
AUDIT_CAPTURE_FINALIZING = "CAPTURE_FINALIZING"
AUDIT_CAPTURE_IMPORT_STARTED = "CAPTURE_IMPORT_STARTED"
AUDIT_CAPTURE_IMPORTED = "CAPTURE_IMPORTED"
AUDIT_CAPTURE_IMPORT_FAILED = "CAPTURE_IMPORT_FAILED"
AUDIT_CAPTURE_COMPLETED = "CAPTURE_COMPLETED"
AUDIT_REPORT_REQUESTED = "REPORT_REQUESTED"
AUDIT_REPORT_APPROVED = "REPORT_APPROVED"
AUDIT_REPORT_REJECTED = "REPORT_REJECTED"
AUDIT_REPORT_DOWNLOADED = "REPORT_DOWNLOADED"
