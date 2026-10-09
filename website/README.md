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
| Get started | the root README's "Quick start" section, `v2/DEPLOY.md`, `v2/docs/onboarding-a-client.md`, `v2/docs/troubleshooting.md` |
| Reference | `v2/README.md`, `v2/docs/checks.md`, `v2/docs/what-office-sentry-reads.md`, `examples/README.md` |
| Project | `SECURITY.md`, `CHANGELOG.md`, `CONTRIBUTING.md` |
| Guides | any other file in `v2/docs/`, added by itself |

- A page's title is its file's first heading, unless `SECTIONS` gives one.
- Links between these files become links between pages, and headings keep GitHub's anchors, so a link
  that works on GitHub works on the site. A link to any other file in the repository goes to GitHub.
- Images in `docs/screenshots/` and the files in `examples/` are copied into the site.
- To add a page somewhere other than Guides, or to rename one, add it to `SECTIONS`. The build stops with
  an error if a listed file is missing.

## Changing the page about Office Sentry

`templates/home.html` is the front page, `templates/base.html` the header and footer, and
`assets/site.css` the styles (the same colours as the app, light and dark). Screenshots come from
`docs/screenshots/` (made by `v2/scripts/screenshots.py`), so the page shows the app as it is.

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

## Dependencies

`requirements.in` lists them and `requirements.txt` pins them with hashes:
`pip-compile --generate-hashes website/requirements.in` after changing the first.

## How it's served

On the project's server, Caddy serves `_site` as static files with a strict Content Security Policy (no
scripts or styles from anywhere else), next to the private portal and the public demo. See
[`v2/deploy/public/README.md`](../v2/deploy/public/README.md).
