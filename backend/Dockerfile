# syntax=docker/dockerfile:1

# Dependencies are installed in a builder stage so the runtime image carries no
# compiler and no package manager -- a smaller image with less in it to exploit.
FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /build
COPY pyproject.toml ./
COPY analyzer/ ./analyzer/
COPY app/ ./app/

RUN uv venv /opt/venv \
    && VIRTUAL_ENV=/opt/venv uv pip install --no-cache .


FROM python:3.12-slim AS runtime

# Containers run as a non-root user; nothing here needs to write to the image.
RUN groupadd --system --gid 1001 app \
    && useradd --system --uid 1001 --gid app --no-create-home app

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

COPY --from=builder /opt/venv /opt/venv

WORKDIR /srv
COPY --chown=app:app analyzer/ ./analyzer/
COPY --chown=app:app app/ ./app/
COPY --chown=app:app gunicorn.conf.py ./

USER app
EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health', timeout=2).status == 200 else 1)"

# Gunicorn supervises one Uvicorn worker per core: threads share a single core
# for Python code, so parallelism comes from processes. Workers share nothing
# but Redis, which is what lets this scale out unchanged.
CMD ["gunicorn", "app.main:app", "-c", "gunicorn.conf.py"]
