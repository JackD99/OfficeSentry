# The public demo (README.md here): the portal's own image plus the fictional data generator, which needs the
# test fixtures and their tools. start.sh builds the portal image first and passes it in as BASE, from a
# build folder holding just tests/, scripts/, requirements-dev.txt and demo.sh (v2/.dockerignore keeps tests
# out of the portal's own image).
ARG BASE=officesentry-app:local
FROM ${BASE}
USER root
COPY requirements-dev.txt /tmp/
# The same hash-pinned lock CI tests with; what the portal image already has is skipped.
RUN pip install --no-cache-dir --require-hashes --no-deps -r /tmp/requirements-dev.txt && rm /tmp/requirements-dev.txt
COPY tests ./tests
COPY scripts ./scripts
COPY demo.sh /usr/local/bin/officesentry-demo
USER sentry
CMD ["officesentry-demo"]
