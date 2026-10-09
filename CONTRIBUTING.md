# Contributing to Office Sentry

All code lives in `v2/`. Its [README](v2/README.md) covers what the product
does and [CLAUDE.md](v2/CLAUDE.md) covers the conventions (adding
collectors, checks, reports and migrations).

## Development setup

Requires Python 3.11 or newer.

```bash
cd v2
python -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
OFFICESENTRY_INSECURE_COOKIES=1 python -m officesentry web   # http://127.0.0.1:8000
python -m officesentry worker                                 # second terminal
```

Use a virtual environment as above: on Debian or Ubuntu, installing into the
system Python fails on the apt-installed `blinker` (if you must, add
`--ignore-installed blinker`).

PDF export needs the Pango libraries (WeasyPrint); on Debian or Ubuntu,
`apt install libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz-subset0`. On
Windows everything else works natively (`.venv\Scripts\activate`), and PDF
buttons open a print-ready page instead; use Docker if you're working on PDF
layout. Tests that need real PDF rendering skip themselves without Pango.

## Before opening a pull request

```bash
cd v2
ruff check .
pytest -n auto      # plain `pytest` runs one test at a time, easier for debugging
```

CI runs the same on Python 3.11, 3.12 and 3.13 for every pull request and
push to main, plus a subset on Windows, a Docker image build and a dependency
audit (`pip-audit`). The Python 3.12 run measures coverage and fails under
90%; `pytest -n auto --cov=officesentry` shows it locally.

- Keep changes small and finished end to end; update `v2/CLAUDE.md` and
  `v2/README.md` in the same pull request when behaviour changes.
- When a page or report looks different, regenerate the README screenshots
  and the example reports with `python scripts/screenshots.py` (from `v2/`;
  needs `pip install playwright pillow`, a Chromium for Playwright and
  `pdftoppm`) and look at them before committing.
- Mock Microsoft Graph at the HTTP layer with `responses`, never by
  replacing the client class.
- Never add a write call to a tenant or a non-read-only permission.
- Every route needs a login check, and tenant pages go through the access
  gate in `web/access.py`.

## Reporting security issues

See [SECURITY.md](SECURITY.md). Please don't open a public issue.

## Licence

Office Sentry is licensed under the [GNU Affero General Public License v3.0](LICENSE). By contributing, you agree
that your contributions are licensed under the same terms.
