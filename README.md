# EmotionLab Backend AWS

API REST de EmotionLab construida con Flask. La plataforma usa servicios AWS como fuente oficial de autenticación, persistencia, almacenamiento, correo y observabilidad.

## Arquitectura

- **Flask**: API HTTP y registro de rutas.
- **Amazon Cognito**: autenticación y gestión de usuarios.
- **DynamoDB**: usuarios, participantes, sesiones, auditoría y solicitudes de reporte.
- **S3**: datos brutos de sesiones y reportes generados.
- **Lambda**: adaptador HTTP y tareas de sincronización/procesamiento.
- **SES**: notificaciones por correo.
- **CloudWatch**: registros y métricas operacionales.

La aplicación no usa Firebase ni Google Cloud.

## Instalación

Requiere Python 3.11 o superior.

```bash
python -m venv .venv
.venv\\Scripts\\activate
pip install -r requirements.txt
copy .env.example .env
```

Completa `.env` con los nombres de recursos AWS de tu entorno. No subas ese archivo ni credenciales al repositorio.

## Variables de entorno

| Grupo | Variables |
| --- | --- |
| AWS | `AWS_REGION`, `STORAGE_BACKEND` |
| Cognito | `COGNITO_USER_POOL_ID`, `COGNITO_APP_CLIENT_ID` |
| DynamoDB | `DYNAMODB_USERS_TABLE`, `DYNAMODB_PARTICIPANTS_TABLE`, `DYNAMODB_SESSIONS_TABLE`, `DYNAMODB_REPORT_REQUESTS_TABLE`, `DYNAMODB_AUDIT_LOGS_TABLE` |
| S3 | `S3_DATA_BUCKET`, `S3_SESSIONS_RAW_BUCKET`, `S3_REPORT_PDFS_BUCKET` |
| SES | `SES_SENDER_ADDRESS` |
| CloudWatch | `CLOUDWATCH_LOG_GROUP`, `CLOUDWATCH_ENABLED` |
| Lambda | `LAMBDA_SYNC_FUNCTION_NAME`, `LAMBDA_MODEL_FUNCTION_NAME` |

Consulta `.env.example` para valores de desarrollo no secretos.

## Ejecución local

```bash
python app.py
```

Verifica el servicio con:

```bash
curl http://127.0.0.1:5000/health
```

## Endpoints principales

- `/health`: estado de la API y servicios inicializados.
- `/auth/*`: autenticación con Cognito y recuperación de contraseña.
- `/users`, `/participants`, `/sessions`: recursos de la plataforma.
- `/dashboard/*`: indicadores operacionales.
- `/reports/*` y `/report-requests/*`: generación, aprobación y descarga de reportes.
- `/import` y `/imports`: importación y procesamiento de sesiones.

Las rutas protegidas requieren un token válido de Cognito y aplican autorización por roles.

## Pruebas y validación

```bash
python -m compileall .
python -m pip check
python -m pytest -q
```

## Flujo de producción

Las rutas protegidas validan JWT de Cognito y aplican los roles `admin` y
`teacher`. Los servicios de dominio persisten metadatos en DynamoDB; sesiones
grandes y PDF se almacenan en S3 privado. Las solicitudes de informe siguen
`PENDING -> APPROVED -> COMPLETED` (o `REJECTED`/`ERROR`) y solo `COMPLETED`
emite una URL presigned temporal.

Los servicios principales son `CognitoService`, `DynamoDBService`,
`S3Service`, `SESService`, `CloudWatchService`, `AuditService`,
`SessionService` y `ReportRequestService`. La auditoría no debe recibir
contraseñas, tokens ni credenciales en sus metadatos.

Consulta `docs/backend-audit-final.md` y `docs/cleanup-report.md` para la
auditoría de calidad y los artefactos que requieren una decisión antes de
eliminarse.

La función Lambda HTTP se expone desde `lambda_handler.py`; la lógica de negocio permanece en los módulos Flask para compartir validaciones y servicios.
# Portabilidad y despliegue en una cuenta AWS nueva

La arquitectura, variables, permisos, comandos de instalación, pruebas, Electron, costos y limitaciones están documentados en [docs/PORTABILITY.md](docs/PORTABILITY.md).

Empaquete funciones antes de planificar Terraform:

```powershell
python scripts/build_lambda_packages.py
```

La URL del frontend se configura desde el output Terraform `api_endpoint` con `npm run configure:api` en el proyecto hermano `Emotionlab-Frontend`. El build de producción exige `VITE_API_URL`.

Guías específicas:

- [Arquitectura](docs/ARCHITECTURE.md)
- [Configuración](docs/CONFIGURATION.md)
- [Despliegue](docs/DEPLOYMENT.md)
- [Seguridad](docs/SECURITY.md)
- [Pruebas](docs/TESTING.md)
- [Resolución de problemas](docs/TROUBLESHOOTING.md)
