FROM python:3.13.18-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    AWS_REGION=us-east-1 \
    EMOTION_MODEL_V8_MODE=lambda \
    PYTHONPATH=/opt/emotionlab

WORKDIR /opt/emotionlab

COPY infrastructure/deploy/backend_v8/requirements-lambda.txt ./

RUN python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip install --no-cache-dir -r requirements-lambda.txt \
    && python -c "import sys; assert sys.version_info[:3] == (3, 13, 18), sys.version"

COPY app.py lambda_handler.py ./
COPY core/ ./core/
COPY middleware/ ./middleware/
COPY routes/ ./routes/
COPY services/ ./services/
COPY integrations/ ./integrations/
COPY ia/modelo_v8/ ./ia/modelo_v8/

RUN python -m py_compile lambda_handler.py \
    && python -c "import awslambdaric, flask, mangum, a2wsgi"

ENTRYPOINT ["python", "-m", "awslambdaric"]
CMD ["lambda_handler.lambda_handler"]
