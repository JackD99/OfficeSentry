# The public demo (README.md here): the portal's own image plus the fictional data generator, which needs the
# test fixtures and their tools. build-demo.sh passes the portal's image in as BASE and builds from a folder
# holding just tests/, scripts/, requirements-dev.txt and demo.sh.
ARG BASE=officesentry:local
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
