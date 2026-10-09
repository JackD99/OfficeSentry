#!/bin/sh
# The public demo container: fresh fictional data, then the portal until 04:00 UTC. Then the container stops,
# Docker starts it again (restart: always) with an empty /data (a tmpfs), and the demo is new for the day:
# anything a visitor did is gone and every date is current again.
set -eu
find /data -mindepth 1 -delete
python scripts/demo_data.py --data-dir /data --public
seconds=$(python -c '
from datetime import UTC, datetime, timedelta
now = datetime.now(UTC)
reset = now.replace(hour=4, minute=0, second=0, microsecond=0)
print(int(((reset if reset > now else reset + timedelta(days=1)) - now).total_seconds()))')
# No access log: visitors are the public, and nothing here needs their addresses.
exec timeout "$seconds" gunicorn --preload --workers 2 --threads 4 --bind 0.0.0.0:8000 --timeout 120 \
    "officesentry.web:create_app()"
