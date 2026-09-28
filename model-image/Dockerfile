FROM public.ecr.aws/lambda/python:3.13

COPY requirements.txt ${LAMBDA_TASK_ROOT}/requirements.txt

RUN pip install \
    --no-cache-dir \
    -r ${LAMBDA_TASK_ROOT}/requirements.txt \
    --target ${LAMBDA_TASK_ROOT}

COPY lambda_function.py ${LAMBDA_TASK_ROOT}/
COPY utils ${LAMBDA_TASK_ROOT}/utils
COPY ia ${LAMBDA_TASK_ROOT}/ia

CMD ["lambda_function.lambda_handler"]
