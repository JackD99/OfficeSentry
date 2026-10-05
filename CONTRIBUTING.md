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

PDF export needs the Pango libraries (WeasyPrint). On Windows, use Docker.

## Before opening a pull request

```bash
cd v2
ruff check .
pytest
```

CI runs the same two commands on every push and pull request.

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
