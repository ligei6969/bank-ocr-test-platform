# syntax=docker/dockerfile:1

FROM python:3.10-slim-bookworm AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    OCR_MODE=mock \
    REVIEW_RECORDS_DB_PATH=/app/reports/review_records.db

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libglib2.0-0 \
        libgomp1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements-runtime.txt .
RUN python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip install --no-cache-dir -r requirements-runtime.txt

COPY app ./app

RUN useradd --system --uid 10001 --create-home appuser \
    && mkdir -p /app/reports \
    && chown -R appuser:appuser /app/reports

USER appuser

EXPOSE 8000
VOLUME ["/app/reports"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/').read()" || exit 1

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]


FROM base AS paddle

USER root
COPY requirements-paddle.txt .
RUN python -m pip install --no-cache-dir -r requirements-paddle.txt
USER appuser


FROM base AS mock
