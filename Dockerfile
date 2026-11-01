# syntax=docker/dockerfile:1.7

# ---------- build stage: install dependencies into a virtualenv ----------
FROM python:3.12-slim AS build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
COPY requirements.txt .
RUN pip install -r requirements.txt

# ---------- runtime stage: slim image, non-root user ----------
FROM python:3.12-slim AS runtime
LABEL org.opencontainers.image.title="ask-your-database" \
      org.opencontainers.image.description="Ask questions about a SQL database in plain language (Claude, read-only SQL)" \
      org.opencontainers.image.source="https://github.com/momchilborisov05-eng/ask-your-database"

# Libraries the mssql-python driver needs on Debian.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libltdl7 libkrb5-3 libgssapi-krb5-2 \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    ASKDB_STORE=sqlite:////data/askdb.db \
    PORT=8000

RUN groupadd --system app && useradd --system --gid app --home-dir /app --shell /usr/sbin/nologin app \
    && mkdir -p /data && chown app:app /data

WORKDIR /app
COPY --from=build /opt/venv /opt/venv
COPY --chown=app:app app.py assistant.py dashboard.py demo.py safe_sql.py seed_db.py store.py ./
COPY --chown=app:app config ./config
COPY --chown=app:app templates ./templates
COPY --chown=app:app static ./static
# The sample shop, so the image runs on its own (ASKDB_DATABASE points elsewhere in production).
RUN python seed_db.py && chown app:app shop.db

USER app
EXPOSE 8000
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=3s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2).status == 200 else 1)"

# Two worker processes with four threads each; a question can take up to a minute (model + query).
CMD ["sh", "-c", "exec gunicorn --bind 0.0.0.0:${PORT} --workers 2 --threads 4 --timeout 120 --access-logfile - 'app:create_app()'"]
