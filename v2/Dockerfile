FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 OFFICESENTRY_DATA_DIR=/data
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz-subset0 fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml ./
COPY officesentry ./officesentry
RUN pip install --no-cache-dir ".[pdf]" && useradd --system --uid 10001 sentry \
    && mkdir /data && chown sentry /data
USER sentry
VOLUME /data
EXPOSE 8000
CMD ["gunicorn", "-w", "2", "-b", "0.0.0.0:8000", "officesentry.web:create_app()"]
