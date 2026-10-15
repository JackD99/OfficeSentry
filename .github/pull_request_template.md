## What changes

<!-- What a person using Office Sentry sees now, and what they'll see after this change. -->

## Checks

- [ ] **Database migration:** none, or it takes the next free number above the highest on `main` after merging `main` in (`tests/test_migrations.py` fails on a clash) and never edits an applied one.
- [ ] **Microsoft permissions:** none added, or the new ones are read-only, listed in `entra.GRAPH_PERMISSIONS` and called out here, since every client tenant has to consent again.
- [ ] **Access gate:** every new route has `@needs(...)` naming its capabilities (or `@login_required` for a page anyone signed in may open), and tenant pages load the tenant with `access.tenant_or_404()`.
- [ ] **Read-only:** nothing writes to a client tenant (GET-only Graph, `Get-` Exchange cmdlets only).
- [ ] **Docs and changelog:** the docs this affects are updated (`README.md`, `v2/README.md`, `v2/docs/`, the website, `v2/CLAUDE.md`), anything the next release adds is marked `<!-- since x.y.z -->` so the website waits for the release (website/README.md, "Not released yet"), and `CHANGELOG.md` has a line under the Unreleased heading.
- [ ] **Screenshots:** pages that look different were checked in a browser, with screenshots here (and `scripts/screenshots.py` re-run if the README images changed).
- [ ] `ruff format .`, `ruff check .` and `pytest -n auto` pass in `v2/`.
- [ ] **Sign-off:** every commit has a `Signed-off-by` line (`git commit -s`; CONTRIBUTING.md, "Signing off your commits"). Not needed on the maintainer's own pull requests.
