# The website

The project's website (officesentry.co.uk): a page about Office Sentry, and the repository's own docs as
web pages with search. It's plain HTML made by `build.py`; there is no server code and nothing to sign in to.

## Changing the docs

Edit the Markdown file where it lives, on GitHub or in a pull request. Once it's on `main`, the Deploy
workflow rebuilds the site and copies it to the server by itself (a change to Markdown, `docs/`, `examples/`
or `website/` starts it), so there is nothing else to do.

Which files become pages is the `SECTIONS` list at the top of `build.py`:

| Menu section | Pages |
|---|---|
| Get started | the root README's "Quick start" section, `v2/DEPLOY.md`, `v2/docs/onboarding-a-client.md`, `v2/docs/staff-sign-in.md`, `v2/docs/client-sign-in.md`, `v2/docs/troubleshooting.md`, `v2/docs/getting-help.md`, `v2/docs/securing-the-portal.md` |
| Deploy | `v2/docs/deploy/README.md` (the list of platforms) and each guide in `v2/docs/deploy/` |
| Reference | `v2/README.md`, `v2/docs/checks.md`, `v2/docs/what-office-sentry-reads.md`, `examples/README.md` |
| How-to | `website/pages/how-to/`: one page per job people search for (an MFA report, mail forwarding, unused licences, Cyber Essentials evidence, SharePoint storage, keeping sign-in logs), the manual way first, then Office Sentry's report |
| Compare | `website/pages/compare/`: Office Sentry next to the other ways of reporting on Microsoft 365, and next to AdminDroid and CIPP. Each says when it was checked; keep them factual and fair, and correct them when a product changes |
| Project | `SECURITY.md`, `CHANGELOG.md`, `ROADMAP.md`, `DEVELOPING.md`, `CONTRIBUTING.md`, `SUPPORT.md` |
| Guides | any other file in `v2/docs/`, added by itself |

A new guide in `v2/docs/deploy/` isn't added by itself: list it under Deploy in `SECTIONS` and in the table
in `v2/docs/deploy/README.md` (`v2/tests/test_deploy_platforms.py` fails until both are done).

- A page's title is its file's first heading, unless `SECTIONS` gives one.
- Links between these files become links between pages, and headings keep GitHub's anchors, so a link
  that works on GitHub works on the site. A link to any other file in the repository goes to GitHub.
- Images in `docs/screenshots/` and the files in `examples/` are copied into the site.
- To add a page somewhere other than Guides, or to rename one, add it to `SECTIONS`. The build stops with
  an error if a listed file is missing.

### Not released yet

Docs follow the code, so a pull request that adds a feature describes it on `main` straight away. But the site
should only show what people can install. Mark what the next release adds, and the site leaves it out until
`CHANGELOG.md` gives that version a date:

```markdown
<!-- since 2.4.0 -->
## The new section

Everything up to the closing line, headings, lists and tables included.
<!-- /since -->

| A table row the release adds | ... | <!-- since 2.4.0 -->
```

- A block runs from a `<!-- since x.y.z -->` line to a `<!-- /since -->` line. A line that carries the marker
  anywhere else (a table row, a one-line list item) is left out on its own.
- A page that's all one block, such as a new guide, isn't built at all, and links to it go to GitHub.
- GitHub hides the comments and shows everything, as it shows `main`. Once the version is released, the
  markers do nothing, and the release pull request can delete them.
- Text that a release *changes* can't be marked this way. Change it in the release pull request.
- The changelog page leaves out the "Unreleased" section, and the [What's new](#whats-new) band waits for
  the release too.
- The build stops on a marker naming a version with no heading in `CHANGELOG.md`, and on a block that isn't
  closed. `docs/checks.md` is generated, so a new check's row shows as soon as it merges.

To see the site as it will look once the next release is out, build it with `SITE_PREVIEW=true`.

## Changing the page about Office Sentry

`templates/home.html` is the front page, `templates/base.html` the header and footer, and
`assets/site.css` the styles: the app's look from `DESIGN.md`, light and dark, with the app's own fonts,
which the build copies from `v2/officesentry/web/static/fonts/` into `assets/fonts/`. Screenshots come from
`docs/screenshots/` (made by `v2/scripts/screenshots.py`), so the page shows the app as it is. Pages link
the styles and script with a fingerprint of their contents (`site.css?v=...`), so a browser that kept the
last deploy's copy for a day fetches the new one. A new stylesheet or script goes in `assets` in
`build()` and is linked as `{{ assets['name'] }}`.

### What's new

`WHATS_NEW` in `build.py` lists feature releases' headlines, newest first. Each is a heading, a sentence
under it, a few words for the link at the top of the page, up to six items (an icon, a title, a sentence or two
and a link to where it's explained) and a list of smaller changes. The front page shows the newest release
`CHANGELOG.md` gives a date, in a band after the features, says "New in 2.3" with the date, and links to that
version in the changelog; the link at the top of the page jumps to it. So the next release's list grows on
`main`, an item as each change merges, while the site keeps showing the last release's until the next one is
out (a `SITE_PREVIEW` build shows it as "Coming in 2.4"). A list with no items is skipped. Drop the oldest list
once a newer one is released.

`assets/social-card.png` is the picture shown when someone shares a link to the site (LinkedIn, Reddit,
Teams, Slack and so on). To change it, replace it with another 1200×630 PNG. `assets/favicon.svg` is the tab
icon; `favicon.ico` and `apple-touch-icon.png` are the same icon for older browsers and phones, and the build
also puts those two at the site's root, where they're looked for.

## Search engines

Every page has a title, a description, its canonical address and link preview tags, all in
`templates/base.html`. A doc's description is its `blurb` in `SECTIONS`, so give every listed doc one. The
front page also tells search engines the site's name and what Office Sentry is (`home_data` in `build.py`),
and each doc page says it sits under Docs (`breadcrumbs`). The build writes `sitemap.xml` with every page and
a `robots.txt` that points to it, and it uses `SITE_URL` with its host in lower case wherever it gives the
site's address. It also writes `/.well-known/security.txt` (`security_txt.py`), which takes where to report a
vulnerability from `SECURITY.md` (GitHub's private reporting form only once the repository is public) and
expires 350 days after the deploy, so a deploy at least once a year keeps it current.

## How the screenshots and example reports stay current

Every time the Deploy workflow updates the website, it first runs `v2/scripts/screenshots.py` on that
code: it fills a throwaway portal with the fictional demo data, then retakes every screenshot and example
report before building the site. So the site always shows what's on `main`, new pages included, with
nothing to commit. If that step fails, the deploy carries on with the copies in the repository.

The copies in `docs/screenshots/` and `examples/` are what GitHub shows in the README. Refresh them in a
pull request that changes how pages look (`python scripts/screenshots.py` in `v2/`).

To show a new page on the site, add a `Shot` to `SHOTS` in `v2/scripts/screenshots.py`, run it once so the
image exists, and use it in `templates/home.html` with `shot('name', 'alt text')`. If the page needs data
the demo doesn't have, add it to `v2/scripts/demo_data.py`, which the public demo uses too.

## Trying it on your computer

```bash
pip install --require-hashes -r website/requirements.txt
python website/build.py
python -m http.server -d website/_site 8080
```

Then open http://localhost:8080. The build goes to `website/_site/`, which isn't committed.

Settings, all optional (the Deploy workflow sets them): `SITE_URL` (the site's address),
`SITE_DEMO_URL` (the public demo's address), `SITE_SOURCE_PUBLIC` (`true` shows the GitHub links and
calls the project open source, which the workflow sets once the repository is public; until then the page
says the source is coming) and `SITE_CONTACT` (an email address or `https://` link for "Get in touch" and
"Ask for early access", from the `OFFICESENTRY_WEBSITE_CONTACT` variable; without it they're left out).
`SITE_PREVIEW=true` shows what the next release adds as well ([Not released yet](#not-released-yet)); the
Deploy workflow never sets it.

## Dependencies

`requirements.in` lists them and `requirements.txt` pins them with hashes:
`pip-compile --generate-hashes website/requirements.in` after changing the first.

## How it's served

On the project's server, Caddy serves `_site` as static files with a strict Content Security Policy (no
scripts or styles from anywhere else), next to the private portal and the public demo. See
[`v2/deploy/public/README.md`](../v2/deploy/public/README.md).
