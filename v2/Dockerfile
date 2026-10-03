# One image for both the portal ("web") and the background worker.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    OFFICESENTRY_DATA_DIR=/data
# Pango and a font for PDF reports; tzdata for schedule timezones.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz-subset0 fonts-dejavu-core tzdata \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml ./
COPY officesentry ./officesentry
RUN pip install --no-cache-dir ".[pdf]" \
    && useradd --system --uid 10001 --home-dir /data sentry \
    && mkdir /data && chown sentry /data
USER sentry
VOLUME /data
EXPOSE 8000
# The worker runs the same image with: python -m officesentry worker
CMD ["gunicorn", "--workers", "2", "--threads", "4", "--bind", "0.0.0.0:8000", "--timeout", "120", \
     "--access-logfile", "-", "officesentry.web:create_app()"]
