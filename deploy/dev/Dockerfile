FROM python:3.12.14-slim-bookworm@sha256:392307d22300de8b5986851a12d9176dfc0fc073e65bf6523ebd7dcbeb23564e

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_PYTHON_DOWNLOADS=never \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

RUN python -m pip install --no-cache-dir uv==0.9.21

COPY pyproject.toml uv.lock README.md alembic.ini ./
COPY src/ ./src/
COPY contracts/ ./contracts/
COPY migrations/ ./migrations/

RUN uv sync --locked --no-dev --extra mcp --python /usr/local/bin/python \
    && python -c "import hnh; import mcp"

RUN groupadd --gid 10001 hnh \
    && useradd --uid 10001 --gid 10001 --home-dir /nonexistent --no-create-home hnh \
    && mkdir -p /data/blobs \
    && chown -R hnh:hnh /data

USER 10001:10001

CMD ["uvicorn", "hnh.transport.http.app:app", "--host", "0.0.0.0", "--port", "8080"]
