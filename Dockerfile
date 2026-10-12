FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN pip install --no-cache-dir uv

COPY pyproject.toml README.md ./
COPY src/ src/
COPY alembic/ alembic/
COPY alembic.ini ./

RUN uv pip install --system -e "."

EXPOSE 8000

CMD ["uvicorn", "driftline.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
