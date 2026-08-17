FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Сертификат Минцифры нужен для обращения к platform-api2.max.ru (раздел 2 ТЗ).
# Положите russian_trusted_root_ca.cer рядом с Dockerfile — иначе шаг пропускается.
COPY certs/ /usr/local/share/ca-certificates/extra/
RUN update-ca-certificates || true
# Отдельно в /app/certs: httpx читает связку certifi, а не системную,
# и обновления системного хранилища до него не доходят.
COPY certs/ /app/certs/

COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN pip install --upgrade pip && pip install -e ".[dev]" && pip install psycopg[binary]

COPY alembic.ini ./
COPY migrations/ ./migrations/

EXPOSE 8000
CMD ["uvicorn", "repairbot.app:app", "--host", "0.0.0.0", "--port", "8000"]
