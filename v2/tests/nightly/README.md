# Nightly suite and live canary

Two scheduled GitHub Actions workflows run the checks that are too slow, or need too much, for every pull
request. Each opens an issue when it fails (labelled `nightly` or `canary`), updates it while it keeps
failing, and closes it when a run passes again, so a failure arrives as a notification rather than a red run
nobody opens.

They run on Mondays; set the repository variable `SCHEDULED_CHECKS_NIGHTLY` to `true` and they run every
night (standard runners are free for a public repository).
Either can be run by hand: **Actions**, pick the workflow, **Run workflow**.

## Nightly suite (`.github/workflows/nightly.yml`)

The tests in this folder, collected only with `pytest --nightly`:

| Test | What it checks |
|---|---|
| `test_upgrades.py` | Each release tag's own `scripts/demo_data.py` writes a demo database; this code upgrades it, checks its integrity and opens every page a crawl reaches from the dashboard and each tenant (about 1,000). A server error or crash fails. |
| `test_pdf_snapshots.py` | The example PDFs (`examples/`), drawn at a fixed date with the Docker image's fonts (`fonts.conf`), page by page against `pdf_baselines/`. A moved block, a lost column or a different page count fails; anti-aliasing doesn't. |
| `test_scale.py` | `scripts/scale_data.py` at 200 tenants and 90 nights, then the busiest pages opened twice (first visit and normal visit) against time budgets. |

Pull requests that change this folder, the demo or scale scripts, or how PDFs are drawn (report templates,
`report.css`, `tokens.css`, charts) run the suite too.

Run it locally from `v2/` (needs Pango, `pdftoppm` from poppler-utils and the release tags):

```sh
pip install --require-hashes --no-deps -r tests/nightly/requirements.txt
git fetch --tags
python -m pytest tests/nightly --nightly
```

### When a PDF looks different

The run's **pdf-pages** download (kept three days) has each changed page, its baseline and a picture of the
difference in red; locally they're in `output/`. If the change is a mistake, fix it. If you meant it, refresh
the baselines: add the label **refresh-baselines** to the pull request (it redraws them, commits them to the
branch, takes the label off and compares them again; approve the checks GitHub holds for that commit, or push
once more, before merging), or in GitHub, **Actions > Nightly checks > Run workflow**, pick the branch and tick
**Save new PDF baselines** (it commits them to the branch), or run `pytest tests/nightly/test_pdf_snapshots.py
--nightly --update` on Ubuntu 24.04 with the packages `nightly.yml` installs. Look at the new images before
merging.

### When a page goes over its time budget

The run's summary has every page's times. Find what made it slower; if the slowdown is meant, raise its
budget in `test_scale.py` and say why.

## Live canary (`.github/workflows/canary.yml`)

`python -m officesentry live-test --canary REPORT` runs every collector against the test tenant and compares
the shape of each answer (which fields come back, and their types; never the values) with
`tests/fixtures/live_tenant.json`, the recording CI replays (`officesentry/canary.py`). It fails when a field
the code reads stops coming back or changes type, when a call that worked starts failing, or when Microsoft
sends a `Deprecation` or `Sunset` header the recording doesn't have. Re-recording the fixture keeps those
headers, which acknowledges them; they stay listed in every report, with their dates. No response is saved
or uploaded, and the log shows each collector's status only.

In GitHub Actions it signs in with no stored password or key: the run's own OIDC token, which the test
tenant's app trusts through a federated credential. Until the `CANARY_CLIENT_ID` variable below exists the
workflow skips itself. To set it up (once, about 10 minutes):

1. Go to **https://entra.microsoft.com** and sign in to the test tenant as a Global Administrator.
2. Open **Applications**, then **App registrations**, then **All applications**, and click the test app
   (*Office Sentry test (read-only)*, the one `live-test` uses).
3. In its left menu click **Certificates & secrets**, then the **Federated credentials** tab, then
   **+ Add credential**.
4. For **Federated credential scenario** choose **GitHub Actions deploying Azure resources**.
5. Fill in: **Organization** `JackD99`, **Repository** `OfficeSentry`, **Entity type** `Branch`,
   **GitHub branch name** `main`. Check that the **Subject identifier** below reads exactly
   `repo:JackD99/OfficeSentry:ref:refs/heads/main`.
6. Set **Name** to `github-canary-main`, leave **Audience** as `api://AzureADTokenExchange`, and click **Add**.
7. Still on the app, open **Overview** and copy the **Application (client) ID** and the **Directory (tenant) ID**.
8. In GitHub, open the repository's **Settings**, then **Secrets and variables**, then **Actions**. Add the
   first as a repository secret (**Secrets** tab) and the second as a repository variable (**Variables** tab):
   - `CANARY_TENANT_ID`: the Directory (tenant) ID, a secret so it never shows in the public run logs
   - `CANARY_CLIENT_ID`: the Application (client) ID
9. Open **Actions**, click **Live canary**, then **Run workflow** on `main`. It takes about three minutes; a green
   run with "Nothing changed" in its summary means it works.

The credential only lets workflow runs on `main` sign in as the test app, which can only read. A pull request,
a fork or another branch gets a token with a different subject, which Entra refuses. At go-public, move it to
a protected `canary` environment: add `environment: canary` to the job and change the credential's entity type
to **Environment** (`repo:JackD99/OfficeSentry:environment:canary`).
