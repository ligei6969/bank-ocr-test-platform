# syntax=docker/dockerfile:1

ARG PYTHON_VERSION=3.10


FROM python:${PYTHON_VERSION}-slim-bookworm AS runtime-dependencies

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:${PATH}"

WORKDIR /build

RUN python -m venv "${VIRTUAL_ENV}"

COPY requirements-runtime.txt .
RUN python -m pip install --upgrade pip \
    && python -m pip install -r requirements-runtime.txt


FROM runtime-dependencies AS paddle-dependencies

COPY requirements-paddle.txt .
RUN python -m pip install -r requirements-paddle.txt


FROM python:${PYTHON_VERSION}-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:${PATH}" \
    OCR_MODE=mock \
    REVIEW_RECORDS_DB_PATH=/app/reports/review_records.db

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libglib2.0-0 \
        libgomp1 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=runtime-dependencies /opt/venv /opt/venv

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


FROM runtime AS mock


FROM runtime AS paddle

COPY --from=paddle-dependencies /opt/venv /opt/venv
