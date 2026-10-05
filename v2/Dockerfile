# One image for both the portal ("web") and the background worker.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    OFFICESENTRY_DATA_DIR=/data
# Pango and a font for PDF reports; tzdata for schedule timezones.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz-subset0 fonts-dejavu-core tzdata \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
# Dependencies first, from pyproject.toml alone, so a code change doesn't reinstall them.
COPY pyproject.toml ./
RUN python -c "import tomllib; p = tomllib.load(open('pyproject.toml', 'rb'))['project']; \
print('\n'.join(p['dependencies'] + p['optional-dependencies']['pdf']))" > /tmp/requirements.txt \
    && pip install --no-cache-dir -r /tmp/requirements.txt \
    && useradd --system --uid 10001 --home-dir /data sentry \
    && mkdir /data && chown sentry /data
COPY officesentry ./officesentry
RUN pip install --no-cache-dir --no-deps . && rm -rf build officesentry.egg-info
USER sentry
VOLUME /data
EXPOSE 8000
# The worker runs the same image with: python -m officesentry worker
# --preload builds the app (and applies database upgrades) once, before the
# workers fork, so two workers never upgrade the database at the same moment.
CMD ["gunicorn", "--preload", "--workers", "2", "--threads", "4", "--bind", "0.0.0.0:8000", "--timeout", "120", \
     "--access-logfile", "-", "officesentry.web:create_app()"]
