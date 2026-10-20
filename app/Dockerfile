# syntax=docker/dockerfile:1
# Pin the reviewed Python security patch and OS image; changing a tag alone must
# not silently change the artifact we test. GPU images have independent pins.
FROM python:3.11.17-slim-bookworm@sha256:0a310eeecf4e1f5a0743f9a6520c90c88d089c903ca5fd283f501e3a805f5f89
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 DATABASE_PATH=/data/pulsefeed.db
WORKDIR /app
COPY pyproject.toml requirements.lock ./
# Install only runtime requirements from the shared app/test constraints. Keeping
# this before COPY pulsefeed lets UI/source edits reuse the installed dependencies.
# Use Docker's layer cache: Railway's cache mounts require a service-specific ID,
# which would make this shared Dockerfile depend on one deployment account.
RUN python -c "import pathlib,tomllib; p=tomllib.loads(pathlib.Path('pyproject.toml').read_text()); pathlib.Path('/tmp/runtime-requirements.txt').write_text('\n'.join(p['project']['dependencies'])+'\n')" \
    && python -m pip install --no-cache-dir --only-binary=:all: -c requirements.lock -r /tmp/runtime-requirements.txt \
    && python -m pip check \
    && rm /tmp/runtime-requirements.txt
COPY pulsefeed ./pulsefeed
RUN python -m pip install --no-cache-dir --no-deps . \
    && python -m pip check \
    && mkdir /data
EXPOSE 8000
# One coordinator owns SQLite admission/reconciliation. Extra ASGI workers would
# compete over durable jobs; scale model endpoints separately from this process.
CMD ["sh", "-c", "exec uvicorn pulsefeed.main:create_app --factory --host 0.0.0.0 --port ${PORT:-8000} --workers 1 --no-access-log"]
