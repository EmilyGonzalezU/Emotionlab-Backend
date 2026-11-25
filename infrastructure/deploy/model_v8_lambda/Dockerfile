FROM python:3.13.18-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/opt/emotionlab

WORKDIR /opt/emotionlab

COPY ia/modelo_v8/requirements-inferencia.txt ./

RUN python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip install --no-cache-dir -r requirements-inferencia.txt \
    && python -m pip install --no-cache-dir awslambdaric \
    && python -c "import sys; assert sys.version_info[:3] == (3, 13, 18), sys.version"

COPY ia/modelo_v8/ ./ia/modelo_v8/

RUN mkdir -p services \
    && python -c "import json; from pathlib import Path; p=Path('ia/modelo_v8'); m=json.loads((p/'manifest_sha256_v1.json').read_text()); import hashlib; [(_ for _ in ()).throw(RuntimeError(f'Hash incorrecto: {f}')) if hashlib.sha256((p/f).read_bytes()).hexdigest()!=h else None for f,h in m.items()]"

COPY model_v8_lambda_handler.py ./
COPY services/emotion_model_v8_service.py ./services/
COPY services/emotion_model_v8_package.py ./services/
COPY services/emotion_model_v8_contract.py ./services/

ENTRYPOINT ["python", "-m", "awslambdaric"]
CMD ["model_v8_lambda_handler.lambda_handler"]
