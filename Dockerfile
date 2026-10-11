FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY pyproject.toml ./
COPY domain ./domain
COPY adapters ./adapters
COPY api ./api
COPY worker ./worker
RUN pip install .

# Migrations run from the image too, behind the manual gate
# (.agents/rules/backend.md § Migrations): docker compose exec api alembic
# upgrade head - never automatically on boot.
COPY alembic.ini ./
COPY alembic ./alembic

# main.py is the ASGI entry point and is not part of the installed packages.
COPY main.py ./

RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin app
USER app

EXPOSE 8000
# Caddy strips /api before proxying; --root-path makes /docs load /api/openapi.json.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--root-path", "/api"]
