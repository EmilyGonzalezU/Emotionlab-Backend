# Imagen Docker del SERVICIO DE INFERENCIA V2 (K-Means congelado).
#
# Se construye desde la RAÍZ del repositorio para reutilizar el artefacto
# congelado y el motor validado:
#
#   docker build -f model-v2-image/Dockerfile -t emotionlab-model-v2 .
#
# Las dependencias son las EXACTAS del artefacto (scikit-learn 1.6.1), por lo
# que esta imagen queda aislada de la versión de scikit-learn del backend.

FROM public.ecr.aws/lambda/python:3.12

# Dependencias congeladas del modelo V2 (fuente única en la raíz).
COPY requirements-v2.txt ${LAMBDA_TASK_ROOT}/requirements.txt

RUN pip install --no-cache-dir -r ${LAMBDA_TASK_ROOT}/requirements.txt

# Artefacto oficial congelado (incluye emotionlab_inferencia.py, config y joblib).
COPY ia/modelo_v2 ${LAMBDA_TASK_ROOT}/ia/modelo_v2

# Motor de inferencia validado (módulo puro: no importa Flask ni boto3).
COPY services/emotion_model_v2_service.py ${LAMBDA_TASK_ROOT}/services/emotion_model_v2_service.py

# Handler del servicio.
COPY model-v2-image/lambda_function.py ${LAMBDA_TASK_ROOT}/lambda_function.py

CMD ["lambda_function.handler"]
