# Layout checks: every page at four widths

`test_layout.py` opens every page of the portal in Chromium at 1366, 1440 and 1920 px wide and on a phone
(390 px), and fails a page that:

- **scrolls sideways** (the page is wider than the window), naming what sticks out;
- **cuts text or a control off**: an ancestor with `overflow: hidden` or `clip` hides part of it, and it
  doesn't end in an ellipsis (an ellipsis is truncation on purpose);
- **draws things on top of each other**: two pieces of text, or text and a control, overlap;
- **puts a control past the edge of the window** where nothing can scroll to it;
- **has a table that scrolls sideways** at a laptop width, or, on a phone, a table stacked into one card per
  row (`stack-sm`) that is still too wide.

`audit.js` does the looking, in the page: it measures what is actually drawn (each line of text and each
control, after every ancestor that clips it), so a column clipped by its neighbour or a menu pushed off the
edge is caught even when the HTML looks fine. Hidden things (closed `<details>`, `display: none`,
screen-reader text) are left out. The browser tests (`tests/browser/test_pages.py`) run its page-scroll
check alone on each of their pages, where it already blocks a merge.

Pages are checked once their fonts are in, but they first draw in a fallback font, and a table measured then
could overflow when Inter arrives. `test_tables_are_fitted_again_once_the_fonts_arrive` holds the fonts back
until `tables.js` has measured a page, then checks its tables fit and match a fresh fit.

## Which pages, with what data

`pages.py` works the list out from the portal's routes, so a new page is checked without being added
anywhere: every GET route is a page unless it is a download (`.pdf`, `.xlsx`, ...) or in `NOT_PAGES` with
the reason. `<key>` is every detail report (and on All clients, the monthly review and insurance evidence
too), `<scheme>` every evidence scheme, and other arguments come from `RESOLVERS` in `conftest.py`: a route
with a new kind of argument fails until it has one. Pages are opened:

- as an admin on the demo site (`scripts/screenshots.py`) with a **stress client** added (`stress.py`): a
  very long name, people with long names and long sign-in addresses, every display name stretched, and
  Conditional Access policies that include and exclude sixteen admin roles, groups and people, plus a tenant
  group, report plan, recommendation, staff account and contact with long names;
- as an admin on the client still being onboarded, which has **no data** (every client page once);
- as the stress client's **own user** (the pages a client sees);
- as the admin of a **new install** with nothing in it (every page without arguments);
- **signed out** (the sign-in page).

## Running them

From `v2/`, in the same pinned Playwright image as the browser tests (`tests/browser/README.md`):

```sh
docker run --rm --ipc=host -v "$PWD":/v2 -w /v2 mcr.microsoft.com/playwright/python:v1.63.0-noble bash -c \
  "pip install -q --root-user-action=ignore --require-hashes --no-deps -r requirements-dev.txt \
   -r tests/browser/requirements.txt && python -m pytest tests/layout --layout -n 4 -p no:cacheprovider"
```

Outside Docker it also works with any Chromium under `$PLAYWRIGHT_BROWSERS_PATH` (`/opt/pw-browsers` by
default), since layout doesn't depend on the exact browser build the way screenshots do. Add `-k across` (or
any part of a page's name, such as `stress/reports.data_report[mfa]`) for some pages.

Every page with a problem leaves a screenshot in `output/` with the problem outlined in red, and
`output/report.md` lists every problem by page and width. `python -m tests.layout.report <folder>` rewrites
the report from the `results-*.json` files in a folder.

## In CI

The **Layout** job in `.github/workflows/ui.yml` runs them on pull requests that touch pages, weekly and by
hand, and fails the pull request on any problem: the run's summary page lists every page with one, and the
screenshots are the run's `layout` artifact (kept for a week).

A matrix that no laptop can show (one column per control, as on the insurance evidence rollup) scrolls by
design, with its first column pinned: its scroll box says why in `data-scrolls`, and the sideways-table check
leaves it alone at laptop widths (phones still check it). Anything else that doesn't fit gets fewer default
columns (`Section.optional`, or `optional_across` for Every row) or lets its long values wrap.
