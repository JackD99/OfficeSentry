# The public demo (README.md here): the portal's own image plus the fictional data generator, which needs the
# test fixtures and their tools. build-demo.sh builds the portal's image from the same code, passes it in as
# BASE and builds this from a folder holding just tests/, scripts/, requirements-dev.txt and demo.sh.
ARG BASE=officesentry-demo-base:local
FROM ${BASE}
USER root
COPY requirements-dev.txt /tmp/
# The same hash-pinned lock CI tests with; what the portal image already has is skipped. The portal's image ships
# without pip, so Python's own bundled copy installs it first, offline.
RUN python -m ensurepip --default-pip \
    && pip install --no-cache-dir --require-hashes --no-deps -r /tmp/requirements-dev.txt && rm /tmp/requirements-dev.txt
COPY tests ./tests
COPY scripts ./scripts
COPY demo.sh /usr/local/bin/officesentry-demo
USER sentry
CMD ["officesentry-demo"]
