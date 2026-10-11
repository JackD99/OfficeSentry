# Contributing to Office Sentry

All code lives in `v2/`. Its [README](v2/README.md) covers what the product
does and [CLAUDE.md](v2/CLAUDE.md) covers the conventions (adding
collectors, checks, reports and migrations). New here? Start with the
[developer guide](DEVELOPING.md): running it locally, how it fits together,
tests and simulated Microsoft responses. What's planned is in
[ROADMAP.md](ROADMAP.md).

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

### Running it in Docker

`docker-compose.yml` runs the published release image. To run your working
copy instead, add the build file:

```bash
cd v2
cp .env.example .env    # with OFFICESENTRY_INSECURE_COOKIES=1 for plain http://localhost
docker compose -f docker-compose.yml -f docker-compose.build.yml up -d --build
```

or put `COMPOSE_FILE=docker-compose.yml:docker-compose.build.yml` in `.env`.
The local image is called `officesentry:local`, so it is never mistaken for
a release.

### Dependencies

`pyproject.toml` lists what Office Sentry needs. What it actually installs
is pinned, with hashes, in two lock files made from it with
[pip-tools](https://pip-tools.readthedocs.io/):

- `v2/requirements.txt`: the Docker image;
- `v2/requirements-dev.txt`: CI, the same versions plus the test tools.

CI's dependency audit (`pip-audit`) checks both, and the browser tests' and
the website's own locks.

Dependabot refreshes both every Monday. To change a dependency yourself, edit
`pyproject.toml`, then from `v2/` with Python 3.12 (the image's):

```bash
pip install pip-tools
pip-compile --generate-hashes --allow-unsafe --strip-extras --extra pdf --build-deps-for wheel \
  --output-file requirements.txt pyproject.toml
pip-compile --generate-hashes --allow-unsafe --strip-extras --extra pdf --extra dev --build-deps-for wheel \
  --constraint requirements.txt --output-file requirements-dev.txt pyproject.toml
```

Add `--upgrade-package <name>` to move one package, or `--upgrade` for all.
CI fails (`pip check`, `tests/test_release.py`) when a lock no longer
matches `pyproject.toml`. The Windows tier installs from `pyproject.toml`
directly, since the locks are made for Linux.

## Before opening a pull request

```bash
cd v2
ruff format .       # CI fails on unformatted code (ruff format --check)
ruff check .
pytest -n auto      # plain `pytest` runs one test at a time, easier for debugging
```

A change to a shell script (`v2/deploy/`, `.github/scripts/`) is also checked
with [ShellCheck](https://www.shellcheck.net) at its strictest level:
`shellcheck --severity=style <file>` (`tests/test_deploy_hardening.py` runs it
too when it's installed).

CI runs the same on Python 3.11, 3.12 and 3.13 for every pull request, with
a Docker image build, a dependency audit (`pip-audit`) and a subset on
Windows. Changes that only touch Markdown, `docs/`, `examples/` or
`website/` skip the tests, and the **CI passed** check sums it all up. The
Python 3.12 run measures coverage and fails under 90%;
`pytest -n auto --cov=officesentry` shows it locally.

- Keep changes small and finished end to end; update `v2/CLAUDE.md` and
  `v2/README.md` in the same pull request when behaviour changes.
- When a page or report looks different, regenerate the README screenshots
  and the example reports with `python scripts/screenshots.py` (from `v2/`;
  needs `pip install playwright pillow`, a Chromium for Playwright and
  `pdftoppm`) and look at them before committing.
- Styles use the tokens in `v2/officesentry/web/static/tokens.css`; `/styleguide`
  in a running portal shows them and every shared component. Pull requests
  that touch templates or CSS also run the browser tests (screenshots against
  baselines, and axe-core); `v2/tests/browser/README.md` says how to run them.
- When a screenshot or an example PDF changes because you meant it to, add
  the label `refresh-baselines` to the pull request: a workflow retakes the
  screenshots in the pinned browser and redraws the PDF pages (from your
  branch merged with main, as the checks compare them), commits what
  changed to the branch, takes the label off and starts the checks again.
  It only works for branches of this repository, never main or a fork. Look
  at the new images in the pull request before merging.
- Mock Microsoft Graph at the HTTP layer with `responses`, never by
  replacing the client class. `tests/seed.py` answers every list a page at
  a time with a next link, as Microsoft does, so a collector that reads one
  page loses most of the seed and its tests fail.
- Tests ignore any `OFFICESENTRY_*` variables in your shell, fail if they
  leave a database connection open, and fail after two minutes if they
  hang. Coverage counts branches as well as lines.
- Never add a write call to a tenant or a non-read-only permission.
- Every route says what it needs with `@needs(...)`: the capabilities
  (`permissions.py`) a person must hold, or `kind="staff"` for a page
  clients never see. The few pages anyone signed in may open use
  `@login_required` and are listed in `ANYONE` in `tests/test_roles.py`,
  which opens every route as every built-in role and expects a refusal
  exactly where the role lacks what the route declared. Client pages load
  the client through the access gate in `web/access.py`.
  `tests/test_isolation_walk.py` opens every client route with another
  client's IDs; a new POST route goes in its `FORMS` (the form the route
  accepts) so the walk sends a form the route would act on, not an empty
  one.

## Signing off your commits

Office Sentry uses the [Developer Certificate of Origin](https://developercertificate.org)
(DCO): by adding a `Signed-off-by` line to a commit, you confirm you wrote
it, or otherwise have the right to submit it, under the project's licence
(AGPL-3.0). There is no agreement to sign. Add the line with `-s`:

```bash
git commit -s -m "Add a column for the mailbox's archive size"
```

It must name the commit's author email. The **Sign-off (DCO)** check on your
pull request lists any commit without one; to fix them all at once, run
`git rebase --signoff origin/main` and push again with
`git push --force-with-lease`. Pull requests from the maintainer don't need
it.

## How changes are checked

The same gates apply to every change, whoever wrote it:

- Every change is a pull request against `main`; nothing is pushed to `main`
  directly.
- CI runs `ruff check` and `ruff format --check`, ShellCheck over the shell
  scripts, the full test suite on Python 3.11, 3.12 and 3.13 (with a 90%
  floor on line and branch coverage), a Windows subset, a Docker build and
  `pip-audit` over every lock file on every pull request, and a pull request
  isn't merged until it's green. The pull request template's checklist
  covers migrations, new permissions, the access gate and read-only access.
- Tests simulate Microsoft Graph and Exchange at the HTTP level, and
  `tests/test_live_fixture.py` replays responses recorded from a real
  Microsoft 365 Business Premium test tenant (names, emails, domains and IDs
  scrubbed), so collectors are checked against what Microsoft actually
  returns.
- Rules that matter for safety are enforced by tests, not just by review:
  every page requires a login, every page is opened as every built-in role
  and refuses the roles that lack what it needs, client pages go through the
  access gate (walked with another client's IDs, by GET and by POST with a
  valid form, against a second client whose data is marked so that any of
  it showing up is caught), and the Graph client has no write methods.
- Pages and reports that change are checked in a real browser, with
  screenshots, before they're merged, and CI compares about 34 pages with
  their screenshots and runs an accessibility check on them.

The project's direction, scope and releases are decided by its maintainer,
[@JackD99](https://github.com/JackD99).

## Issues, labels and conduct

Use the [issue forms](https://github.com/JackD99/OfficeSentry/issues/new/choose)
for bugs (with the support package from the portal's Settings), report
requests, check requests and other feature requests; questions go in
[Discussions](https://github.com/JackD99/OfficeSentry/discussions). Issues are
public: remove tenant names and IDs, domains, user names and email
addresses, and IP addresses from anything you paste. [SUPPORT.md](SUPPORT.md)
has the full list of where to go for what.

Labels are defined in [`.github/labels.yml`](.github/labels.yml) and synced
to GitHub by the Labels workflow when that file changes on `main`; edit the
file, not the label on GitHub. **good first issue** and **help wanted** mark
issues a new contributor could take; **needs info** means an issue is
waiting for its reporter.

Everyone taking part agrees to the [code of conduct](CODE_OF_CONDUCT.md).

## Releases

Versions follow [semantic versioning](https://semver.org): `2.1.0` adds
features, `2.1.1` only fixes. The [release workflow](.github/workflows/release.yml)
does the work. To make a release:

1. Open a pull request that sets `__version__` in
   `v2/officesentry/__init__.py` (`2.1.0`) and gives the `## [2.1.0]` section of
   [CHANGELOG.md](CHANGELOG.md) its date (`## [2.1.0] - 2026-11-02`), with its
   compare link at the foot of the file. Fill in
   **Action required**, **New Microsoft permissions** and **Database upgrade**
   even when the answer is "None" or "No". Merge it once CI is green.
2. On GitHub: **Actions** → **Release** → **Run workflow**, leave "Use
   workflow from" on `main`, type the version (`2.1.0`) and press **Run
   workflow**. (Pushing a tag, `git push origin v2.1.0`, does the same.)
3. The run checks the version matches `__version__` and the changelog, and
   that the commit passed its full CI run on `main` (so release from `main`
   once that run is green; the release reuses it rather than testing again).
   It builds the amd64 and arm64 images (arm64 natively on GitHub's Arm
   runner) with an SBOM and build provenance, starts each once, scans them,
   signs their provenance, publishes `ghcr.io/jackd99/officesentry:2.1.0` (and `2.1`,
   `2` and `latest`), creates the `v2.1.0` tag and the GitHub release with
   the notes and install files, and removes images older than the last three
   releases. Nothing is published until the checks and the scan have passed.
   A run takes about 5 Actions minutes, which are free for a public repository.
4. Open a pull request that moves `__version__` on to the next development
   version (`2.2.0.dev0`) and adds a `## [2.2.0] - Unreleased` section.

To try the pipeline without releasing, run a release candidate: run the
workflow with `2.1.0-rc.1`. It can come straight from the development version
(`2.1.0.dev0`; the image then reports `2.1.0rc1`), gets only its own image
tag, is marked as a pre-release, and its changelog section may still say
"unreleased".

If the scan fails, there is no release and no version tag on the image (it
stays in the registry under its digest only). Fix the cause, usually by
merging Dependabot's newer base image or dependency, and run it again.

## Reporting security issues

See [SECURITY.md](SECURITY.md). Please don't open a public issue.

## Licence

Office Sentry is licensed under the [GNU Affero General Public License v3.0](LICENSE). By contributing, you agree
that your contributions are licensed under the same terms.
