# Browser tests: screenshots and accessibility

`test_pages.py` opens every page in `pages.py` in Chromium, at a laptop (1366 px) and a phone (390 px)
width, in light and dark, and:

- compares a screenshot with its baseline in `baselines/` (the top 2,400 px on a laptop and 3,200 px on a
  phone; the style guide pages whole);
- runs [axe-core](https://github.com/dequelabs/axe-core) (kept in `vendor/`, never loaded from a CDN) at the
  laptop width, failing on "serious" and "critical" WCAG 2.1 A and AA problems.

The pages come from the fictional demo site in `scripts/screenshots.py` with the clock stopped at
5 October 2026, so dates and "3 days ago" read the same on every run.

## Running them

Screenshots only match in the exact browser that made the baselines, so run them in the pinned Playwright
image (Docker), the same one CI uses (`.github/workflows/ui.yml`). From `v2/`:

```sh
docker run --rm --ipc=host -v "$PWD":/v2 -w /v2 mcr.microsoft.com/playwright/python:v1.63.0-noble bash -c \
  "pip install -q --root-user-action=ignore --require-hashes --no-deps -r requirements-dev.txt \
   -r tests/browser/requirements.txt && python -m pytest tests/browser --ui -p no:cacheprovider"
```

Add, after `--ui`:

- `-k dashboard` for some pages (names in `pages.py`, plus `laptop`, `phone`, `light`, `dark`);
- `--update` to save new baselines after a change you meant (axe still runs);
- `--save-all` to keep every capture in `output/` to look at.

A failed comparison leaves the capture, the baseline and a picture of what changed (in red) in `output/`;
in CI they're the `screenshots` artifact of the run, kept for three days.

Without `--ui` (as in `pytest -n auto`) this folder isn't collected.

## When a screenshot fails

1. Open `output/<page>-<width>-<theme>.diff.png`. If the change is a mistake, fix it.
2. If you meant it, refresh the baselines: run with `--update` locally as above, or in GitHub go to
   **Actions > ui > Run workflow**, pick your branch and tick **Save new baselines**; it commits them to
   the branch. Look at the new images in the pull request before merging. (That commit doesn't start CI
   by itself; your next push does.)

A new page in `pages.py` needs `--update -k <name>` once; a page you remove needs its four baselines
deleted (a test lists them).

## Updating Playwright

Change the version in `requirements.in`, regenerate `requirements.txt` (the command is at its top, run in
the new image), change the image tag and digest in `ui.yml` and the command above, then refresh every
baseline and look through the changes.
