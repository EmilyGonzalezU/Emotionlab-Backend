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

# ---------------------------------------------------------------------
# Valores prefijados / literal para SK y GSIs
# ---------------------------------------------------------------------

PARTICIPANT_UNASSIGNED = "UNASSIGNED"
PK_TYPE_USER = "USER"
PK_TYPE_PARTICIPANT = "PARTICIPANT"
PK_TYPE_SESSION = "SESSION"
PK_TYPE_AUDIT_LOG = "AUDIT_LOG"
PK_TYPE_REPORT_REQUEST = "REPORT_REQUEST"

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
AUDIT_REPORT_REQUESTED = "REPORT_REQUESTED"
AUDIT_REPORT_APPROVED = "REPORT_APPROVED"
AUDIT_REPORT_REJECTED = "REPORT_REJECTED"
AUDIT_REPORT_DOWNLOADED = "REPORT_DOWNLOADED"
