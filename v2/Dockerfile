# One image for both the portal ("web") and the background worker.
# The base is pinned by digest, so a rebuild of the same commit starts from the same bytes; Dependabot
# moves the digest (and patch releases of Python) in its weekly Docker pull request. It comes from AWS's
# mirror of Docker Hub's official images (same image, same digest), since CI's anonymous pulls from Docker
# Hub hit its rate limit.
FROM public.ecr.aws/docker/library/python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    OFFICESENTRY_DATA_DIR=/data
# Pango and a font for PDF reports; tzdata for schedule timezones.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz-subset0 fonts-dejavu-core tzdata \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
# Dependencies first, from the hash-pinned lock alone, so a code change doesn't reinstall them and the
# image holds exactly what CI tested and pip-audit checked (CONTRIBUTING.md, "Dependencies").
COPY requirements.txt ./
RUN pip install --no-cache-dir --require-hashes --no-deps -r requirements.txt \
    && useradd --system --uid 10001 --home-dir /data sentry \
    && mkdir -m 700 /data /keys && chown sentry /data /keys
# docker-compose.yml mounts a volume of its own on /keys and points OFFICESENTRY_SECRETS_FILE there, so the data
# volume and its backups never hold the key that decrypts them. A new volume takes /keys' owner from here.
COPY pyproject.toml ./
COPY officesentry ./officesentry
# Built with the locked setuptools rather than whatever is newest; `pip check` fails the build if the
# lock no longer satisfies pyproject.toml.
RUN pip install --no-cache-dir --no-deps --no-build-isolation . && pip check \
    && rm -rf build officesentry.egg-info
USER sentry
VOLUME /data
EXPOSE 8000
LABEL org.opencontainers.image.title="Office Sentry" \
      org.opencontainers.image.description="Read-only Microsoft 365 reporting for MSPs" \
      org.opencontainers.image.licenses="AGPL-3.0-only"
# The worker runs the same image with: python -m officesentry worker
# --preload builds the app (and applies database upgrades) once, before the
# workers fork, so two workers never upgrade the database at the same moment.
# The access log keeps the path, never the query string (search terms, filter values, the setup link) or the
# referer, which carry what people looked at; serve.py uses the same format without Docker.
CMD ["gunicorn", "--preload", "--workers", "2", "--threads", "4", "--bind", "0.0.0.0:8000", "--timeout", "120", \
     "--access-logfile", "-", "--access-logformat", "%(h)s %(t)s \"%(m)s %(U)s %(H)s\" %(s)s %(b)s %(M)sms \"%(a)s\"", \
     "officesentry.web:create_app()"]
