# EmotionLab

Plataforma para registrar experiencias, analizar datos de bienestar y consultar resultados mediante una aplicación web de escritorio y una API REST.

Este repositorio contiene la API **EmotionLab Backend**. La interfaz está en el repositorio [EmotionLab Frontend](https://github.com/EmilyGonzalezU/Emotionlab-Frontend). La API usa Flask; la autenticación, los datos y los archivos de producción se integran con Amazon Cognito, DynamoDB y S3. El análisis local usa EmotionLab V2.

> Esta guía permite preparar y ejecutar el proyecto. Para el modo local completo se necesita acceso a los recursos AWS configurados para el entorno y al endpoint del modelo remoto cuando no se ejecute el análisis local.

## Requisitos

- Git.
- Python 3.12 para ejecutar el backend con EmotionLab V2 local. El script valida `scikit-learn 1.6.1`.
- Node.js y npm para ejecutar el frontend.
- Acceso a los recursos AWS del entorno: Cognito, DynamoDB y S3. Algunas funciones también requieren SES, CloudWatch, Lambda o el servicio externo Unity.
- Acceso al endpoint remoto del modelo V2 si se utiliza `EMOTION_MODEL_V2_MODE=remote`.
- Para despliegues, AWS CLI v2, Terraform 1.5 o posterior y permisos para los recursos que se crearán.

Docker no es necesario para ejecutar la aplicación localmente. El repositorio no incluye un archivo `docker-compose.yml`.

## Instalación y configuración

### 1. Descargar los repositorios

```bash
git clone https://github.com/EmilyGonzalezU/Emotionlab-Backend.git
git clone https://github.com/EmilyGonzalezU/Emotionlab-Frontend.git
```

### 2. Configurar el backend

Abre una terminal en `Emotionlab-Backend` y crea un entorno virtual. En Windows PowerShell:

```powershell
py -3.12 -m venv .venv-v2
.\.venv-v2\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Edita `.env` y completa los valores correspondientes a tu entorno. Como mínimo, revisa la región y los nombres de tablas y buckets, los identificadores de Cognito, los secretos locales y el modo del modelo. Para usar el análisis V2 local, el entorno debe tener `EMOTION_MODEL_V2_MODE=local`. Para usar el servicio remoto, selecciona `remote` y define `EMOTION_MODEL_V2_API_URL`.

Ejemplo de las variables principales (los valores son ilustrativos):

```dotenv
AWS_REGION=us-east-1
STORAGE_BACKEND=s3
COGNITO_USER_POOL_ID=us-east-1_REPLACE_ME
COGNITO_APP_CLIENT_ID=REPLACE_ME
DYNAMODB_USERS_TABLE=emotionlab-dev-users
DYNAMODB_PARTICIPANTS_TABLE=emotionlab-dev-participants
DYNAMODB_SESSIONS_TABLE=emotionlab-dev-sessions
S3_DATA_BUCKET=emotionlab-dev-data
JWT_SECRET=replace-with-a-random-secret-of-at-least-32-characters
SESSION_CODE_HMAC_SECRET=replace-with-a-random-secret-of-at-least-32-characters
EMOTION_MODEL_V2_MODE=local
```

Consulta `.env.example` para la lista completa. Genera valores propios para los secretos y no compartas ni subas `.env`, credenciales AWS o archivos `terraform.tfvars`.

### 3. Configurar el frontend

Abre otra terminal en `Emotionlab-Frontend`:

```powershell
npm ci
Copy-Item .env.example .env.local
```

En desarrollo, la API usa por defecto `http://127.0.0.1:5000`. Si tu backend está en otra dirección, establece `VITE_API_URL` en `.env.local`. Esta variable se incorpora al cliente; no pongas secretos en variables `VITE_*`.

## Ejecución, acceso y detención

### Iniciar la API

Desde `Emotionlab-Backend`, con el entorno virtual activado:

```powershell
.\scripts\run_backend_v2.ps1
```

El script inicia Flask en modo local y comprueba que se esté usando Python 3.12 y `scikit-learn 1.6.1`. Si el script no puede ejecutarse por la política de PowerShell, permite la ejecución para la sesión actual con `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass` y vuelve a iniciarlo.

Comprueba la API en [http://127.0.0.1:5000/health](http://127.0.0.1:5000/health). Para detenerla, presiona `Ctrl+C` en la terminal donde está ejecutándose.

### Iniciar el frontend

Desde `Emotionlab-Frontend`:

```bash
npm run dev
```

Abre la dirección local que muestra Vite en la terminal (normalmente [http://localhost:5173](http://localhost:5173)). Para detenerlo, presiona `Ctrl+C`. También se puede compilar la versión de producción con `npm run build`; ese comando requiere que `VITE_API_URL` tenga la URL HTTPS real de la API.

Los endpoints principales de la API incluyen `/health`, `/auth/*`, `/users`, `/participants`, `/sessions`, `/captures/*`, `/trackings/*`, `/dashboard/*` y `/reports/*`. Las rutas protegidas requieren un token válido de Cognito y permisos de usuario apropiados. No se incluyen usuarios de prueba ni contraseñas en el repositorio; usa cuentas provistas para tu entorno.

## Solución de problemas

- **El script V2 rechaza el entorno:** confirma Python 3.12 y `scikit-learn 1.6.1`. Vuelve a crear `.venv-v2` con Python 3.12 e instala `requirements.txt`.
- **La API no inicia o `/health` informa errores de servicios:** revisa `.env`, la región AWS, credenciales activas y permisos de Cognito, DynamoDB y S3. Confirma que los recursos indicados en `.env` existan.
- **No se puede conectar con AWS:** configura credenciales mediante el perfil AWS de tu organización (`aws configure` o `AWS_PROFILE`) y verifica el acceso con AWS CLI. No guardes claves en este README ni en Git.
- **El navegador bloquea llamadas por CORS:** configura el origen local del frontend en `CORS_ALLOWED_ORIGINS` para el backend y reinicia la API.
- **El análisis remoto falla:** revisa `EMOTION_MODEL_V2_MODE`, `EMOTION_MODEL_V2_API_URL` y la conectividad HTTPS al endpoint. En modo local, utiliza el script V2 y sus versiones requeridas.
- **El frontend muestra error de conexión:** confirma que Flask esté escuchando en el puerto 5000 y que `VITE_API_URL` coincida con la dirección de la API; reinicia Vite después de cambiar variables.
- **Unity no importa datos:** Unity es un servicio externo. Revisa `AWS_UNITY_DATA_URL`, la disponibilidad del endpoint y el formato de los datos recibidos.

Encontrarás detalles adicionales en los archivos de configuración y despliegue del directorio `infrastructure/terraform` y en los ejemplos `.env.example` de cada proyecto.
## Despliegue

La infraestructura AWS se administra con Terraform en `infrastructure/terraform/environments/dev`. El despliegue necesita una cuenta AWS con permisos para crear o utilizar roles IAM, API Gateway, Lambda, Cognito, DynamoDB, S3, ECR y CloudWatch, además de configurar el modelo V2 y los servicios externos que correspondan.

Antes de desplegar, revisa [docs/PORTABILITY.md](docs/PORTABILITY.md) y [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md), prepara los paquetes Lambda y completa las variables del entorno. Los comandos de Terraform pueden crear recursos con costo. El frontend de producción requiere la URL HTTPS real de API Gateway:

```powershell
npm run configure:api -- ..\Emotionlab-Backend\infrastructure\terraform\environments\dev
npm run build
```

