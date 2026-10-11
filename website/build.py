"""Builds the Office Sentry website (the product page and the docs) into website/_site.

    pip install -r website/requirements.txt
    python website/build.py
    python -m http.server -d website/_site 8080     # then open http://localhost:8080

The product page is templates/home.html. The docs are the repository's own Markdown files, listed in
SECTIONS below, so a doc is edited where it lives (on GitHub or in a pull request) and the site shows the
change at the next deploy. A new file in v2/docs/ appears under "Guides" by itself. Links between docs
become links between pages, links to anything else in the repository go to GitHub, and headings keep
GitHub's anchors, so the same Markdown links work on GitHub and on the site. README.md explains the rest.

Settings come from the environment (the Deploy workflow sets them):
  SITE_URL            the site's address (default https://officesentry.co.uk)
  SITE_DEMO_URL       the demo's address (default: the site's with "demo." in front)
  SITE_SOURCE_PUBLIC  "true" once the repository is public: shows the GitHub links (default false)
  SITE_CONTACT        where interested MSPs get in touch: an email address or an https:// link (default none)
"""

from __future__ import annotations

import html
import json
import os
import posixpath
import re
import shutil
import struct
import sys
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from jinja2 import Environment, FileSystemLoader, select_autoescape
from markdown_it import MarkdownIt
from mdit_py_plugins.anchors import anchors_plugin
from pygments import highlight as pygments_highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import get_lexer_by_name
from pygments.util import ClassNotFound

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
OUT = HERE / "_site"
REPO = "JackD99/OfficeSentry"
BRANCH = "main"
GITHUB = f"https://github.com/{REPO}"


@dataclass
class Doc:
    path: str  # in the repository
    slug: str  # the page is /docs/<slug>/
    title: str = ""  # default: the file's first heading
    section: str = ""  # only this "## " section of the file (for the root README's Quick start)
    blurb: str = ""  # one line for the docs home page
    html: str = ""
    toc: list = field(default_factory=list)
    search: list = field(default_factory=list)

    @property
    def url(self) -> str:
        return f"/docs/{self.slug}/"


SECTIONS = [
    (
        "Get started",
        [
            Doc(
                "README.md",
                "quick-start",
                "Quick start",
                section="Quick start",
                blurb="Run it with Docker, create the read-only app and add your first client.",
            ),
            Doc(
                "v2/DEPLOY.md",
                "install",
                "Running it on your own server",
                blurb="Docker on a Linux server: HTTPS, backups, off-site copies, updates and Windows.",
            ),
            Doc(
                "v2/docs/onboarding-a-client.md",
                "onboarding",
                blurb="Consent, Global Reader, the first collection, connection health and offboarding.",
            ),
            Doc(
                "v2/docs/staff-sign-in.md",
                "staff-sign-in",
                blurb="Let your team sign in with their Microsoft account, with roles assigned in Entra.",
            ),
            Doc(
                "v2/docs/troubleshooting.md",
                "troubleshooting",
                blurb="The common setup and collection errors, and how to fix them.",
            ),
            Doc(
                "v2/docs/getting-help.md",
                "getting-help",
                blurb="Where to look first, the support package to attach to a bug report, and what it leaves out.",
            ),
            Doc(
                "v2/docs/securing-the-portal.md",
                "securing",
                blurb="What the portal does by itself, what you have to do, and reverse proxy setups.",
            ),
        ],
    ),
    (
        "Deploy",
        [
            Doc(
                "v2/docs/deploy/README.md",
                "deploy",
                "Choosing a platform",
                blurb="Which guide to follow, what every platform needs, and keys from a secret store.",
            ),
            Doc(
                "v2/docs/deploy/portainer.md",
                "deploy/portainer",
                blurb="One stack with HTTPS, behind your own proxy, backups and updates.",
            ),
            Doc(
                "v2/docs/deploy/container-managers.md",
                "deploy/container-managers",
                blurb="The same stack in Dockge, Komodo, Coolify and Dokploy.",
            ),
            Doc(
                "v2/docs/deploy/nas.md",
                "deploy/nas",
                "Unraid, Synology, TrueNAS",
                blurb="On a NAS, with its own folders, scheduled backups and reverse proxy.",
            ),
            Doc(
                "v2/docs/deploy/kubernetes.md",
                "deploy/kubernetes",
                blurb="Manifests for AKS, EKS, GKE, k3s and others: one pod, a disk, backups and restores.",
            ),
            Doc(
                "v2/docs/deploy/azure.md",
                "deploy/azure",
                "Azure",
                blurb="A small Linux VM set up with one paste, its firewall, backups and updates.",
            ),
            Doc(
                "v2/docs/deploy/aws.md",
                "deploy/aws",
                "AWS",
                blurb="A Lightsail or EC2 server set up with one paste, its firewall, snapshots and updates.",
            ),
            Doc(
                "v2/docs/deploy/other-clouds.md",
                "deploy/other-clouds",
                blurb="DigitalOcean, Google Cloud, Linode, Vultr, Oracle Cloud and any VPS that takes cloud-init.",
            ),
            Doc(
                "v2/docs/deploy/cloudflare.md",
                "deploy/cloudflare",
                blurb="DNS only, a Cloudflare Tunnel with Access, or proxied, and what Cloudflare must leave alone.",
            ),
        ],
    ),
    (
        "Reference",
        [
            Doc(
                "v2/README.md",
                "reference",
                "Reports and features",
                blurb="Every report and check, Exchange data, sign-in security, national clouds.",
            ),
            Doc("v2/docs/checks.md", "checks", blurb="Every check with its data source, detail report and permission."),
            Doc(
                "v2/docs/what-office-sentry-reads.md",
                "what-it-reads",
                blurb="For clients approving access: every permission, and what is and isn't read.",
            ),
            Doc(
                "examples/README.md",
                "examples",
                "Example reports",
                blurb="Real PDF and spreadsheet exports, made from fictional demo data.",
            ),
        ],
    ),
    (
        "How-to",
        [
            Doc(
                "website/pages/how-to/mfa-report.md",
                "how-to/mfa-report",
                blurb="Report who has set up MFA and who is actually made to use it, for one tenant with PowerShell "
                "or for every client at once.",
            ),
            Doc(
                "website/pages/how-to/mail-forwarding.md",
                "how-to/mail-forwarding",
                blurb="Find every mailbox forwarding mail outside, from mailbox settings and inbox rules, and whether "
                "the tenant allows it.",
            ),
            Doc(
                "website/pages/how-to/unused-licences.md",
                "how-to/unused-licences",
                blurb="Licences on blocked and inactive accounts, paid licences nobody is assigned, people on a bigger "
                "plan than they use, and the saving.",
            ),
            Doc(
                "website/pages/how-to/cyber-essentials.md",
                "how-to/cyber-essentials",
                blurb="Which Cyber Essentials controls Microsoft 365 can evidence, how to gather it, and what's left "
                "for you to answer.",
            ),
            Doc(
                "website/pages/how-to/sharepoint-storage.md",
                "how-to/sharepoint-storage",
                blurb="How the SharePoint storage quota works, where the space goes (versions, recycle bins, a few "
                "big sites) and how to get it back.",
            ),
            Doc(
                "website/pages/how-to/sign-in-logs.md",
                "how-to/sign-in-logs",
                blurb="Entra ID keeps sign-in and audit logs for 30 days. Ways to keep them longer, from PowerShell "
                "exports to Log Analytics.",
            ),
        ],
    ),
    (
        "Compare",
        [
            Doc(
                "website/pages/compare/reporting-tools.md",
                "compare",
                blurb="Microsoft's admin centres, PowerShell, CIPP, AdminDroid and Office Sentry compared for MSP "
                "reporting.",
            ),
            Doc(
                "website/pages/compare/admindroid.md",
                "compare/admindroid",
                blurb="An AdminDroid alternative for MSPs? Office Sentry and AdminDroid compared on cost, reports, "
                "audit trail and views across clients.",
            ),
            Doc(
                "website/pages/compare/cipp.md",
                "compare/cipp",
                blurb="CIPP manages your clients' Microsoft 365; Office Sentry reports on it and keeps the history. "
                "How they differ, and why to use both.",
            ),
        ],
    ),
    (
        "Project",
        [
            Doc(
                "SECURITY.md",
                "security",
                "Security",
                blurb="The security model, and how to report a vulnerability privately.",
            ),
            Doc("CHANGELOG.md", "changelog", "What's new", blurb="What changed in each release."),
            Doc("ROADMAP.md", "roadmap", "Roadmap", blurb="What's planned next, and what's out of scope."),
            Doc(
                "DEVELOPING.md",
                "developing",
                "Developer guide",
                blurb="Running it locally, how it fits together, tests and simulated Microsoft responses.",
            ),
            Doc(
                "CONTRIBUTING.md",
                "contributing",
                "Contributing",
                blurb="Setting up for development, and how changes are built and checked.",
            ),
            Doc(
                "SUPPORT.md",
                "support",
                "Getting help",
                blurb="Where to go for what, the support package, and what never goes in an issue.",
            ),
        ],
    ),
]
GUIDES = "Guides"  # v2/docs/*.md not listed above

# Copied into the site as they are, so the product page and docs can show and link them.
COPIED = {"docs/screenshots": "screenshots", "examples": "examples"}
# Also served from the site's root, where browsers, phones and crawlers look for them without being told.
ROOT_ASSETS = ("favicon.ico", "apple-touch-icon.png")


# -- Settings -------------------------------------------------------------------------------------------------


def lower_host(url: str) -> str:
    """The address with its host in lower case, as search results and link previews should show it."""
    parts = urlsplit(url)
    return parts._replace(netloc=parts.netloc.lower()).geturl()


def settings() -> dict:
    site = lower_host(os.environ.get("SITE_URL", "").strip().rstrip("/") or "https://officesentry.co.uk")
    parts = urlsplit(site)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        sys.exit(f"SITE_URL must be a web address such as https://officesentry.co.uk, not {site!r}")
    demo = lower_host(
        os.environ.get("SITE_DEMO_URL", "").strip().rstrip("/") or f"{parts.scheme}://demo.{parts.netloc}"
    )
    contact = os.environ.get("SITE_CONTACT", "").strip()
    if contact and not contact.startswith("https://"):
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", contact):
            sys.exit(f"SITE_CONTACT must be an email address or an https:// link, not {contact!r}")
        contact = f"mailto:{contact}?subject=Office%20Sentry%20early%20access"
    return {
        "site_url": site,
        "demo_url": demo,
        "github": GITHUB,
        "contact": contact,
        "source_public": os.environ.get("SITE_SOURCE_PUBLIC", "").strip().lower() == "true",
    }


# -- Markdown -------------------------------------------------------------------------------------------------


def github_slug(text: str) -> str:
    """The anchor GitHub gives a heading: lower case, punctuation dropped, spaces to hyphens."""
    text = text.strip().lower()
    text = "".join(ch for ch in text if ch in " -_" or unicodedata.category(ch)[0] in "LNM")
    return text.replace(" ", "-")


def _highlight(code: str, lang: str, _attrs) -> str:
    try:
        lexer = get_lexer_by_name((lang or "text").split()[0])
    except ClassNotFound:
        return ""
    return pygments_highlight(code, lexer, HtmlFormatter(nowrap=True))


def markdown() -> MarkdownIt:
    md = MarkdownIt("commonmark", {"html": True, "highlight": _highlight}).enable(["table", "strikethrough"])
    anchors_plugin(
        md, min_level=1, max_level=6, slug_func=github_slug, permalink=True, permalinkSymbol="#", permalinkBefore=False
    )
    return md


def section_of(text: str, heading: str) -> str:
    """One "## heading" section of a file, as a page of its own (its heading becomes the page's)."""
    lines, out, inside = text.splitlines(), [], False
    for line in lines:
        if line.startswith("## "):
            if inside:
                break
            inside = line[3:].strip() == heading
            if inside:
                out.append("# " + line[3:])
                continue
        if inside:
            out.append(line)
    if not out:
        sys.exit(f"No '## {heading}' section to publish")
    return "\n".join(out)


class Links:
    """Turns a doc's relative links into site pages, copied files or GitHub, wherever they point."""

    def __init__(self, docs: list[Doc]):
        self.pages = {d.path: d for d in docs if not d.section}
        self.sections = {(d.path, github_slug(d.section)): d for d in docs if d.section}

    def resolve(self, base: str, target: str) -> str:
        parts = urlsplit(target)
        if parts.scheme or parts.netloc or target.startswith(("#", "mailto:", "/")) or not parts.path:
            return target
        path = posixpath.normpath(posixpath.join(posixpath.dirname(base), parts.path))
        anchor = f"#{parts.fragment}" if parts.fragment else ""
        if (path, parts.fragment) in self.sections:
            return self.sections[(path, parts.fragment)].url
        if path in self.pages:
            return self.pages[path].url + anchor
        if path == "README.md":
            return "/" + anchor
        for source, published in COPIED.items():
            if path.startswith(source + "/") and (ROOT / path).is_file():
                return f"/{published}/{path[len(source) + 1 :]}"
        kind = "tree" if (ROOT / path).is_dir() else "blob"
        return f"{GITHUB}/{kind}/{BRANCH}/{path}{anchor}"


_HTML_URL = re.compile(r'\b(href|src)="([^"]*)"')


def render(md: MarkdownIt, doc: Doc, links: Links) -> None:
    text = (ROOT / doc.path).read_text(encoding="utf-8")
    if doc.section:
        text = section_of(text, doc.section)
    tokens = md.parse(text)
    # The first heading is the page's title; the template shows it.
    if tokens and tokens[0].type == "heading_open" and tokens[0].tag == "h1":
        doc.title = doc.title or tokens[1].content
        tokens = tokens[3:]
    doc.title = doc.title or Path(doc.path).stem.replace("-", " ").capitalize()
    for i, tok in enumerate(tokens):
        if tok.type == "inline":
            for child in tok.children or []:
                if child.type == "link_open":
                    child.attrSet("href", links.resolve(doc.path, child.attrGet("href") or ""))
                elif child.type == "image":
                    child.attrSet("src", links.resolve(doc.path, child.attrGet("src") or ""))
                elif child.type == "html_inline":
                    child.content = _HTML_URL.sub(lambda m: f'{m[1]}="{links.resolve(doc.path, m[2])}"', child.content)
        elif tok.type == "html_block":
            tok.content = _HTML_URL.sub(lambda m: f'{m[1]}="{links.resolve(doc.path, m[2])}"', tok.content)
        elif tok.type == "heading_open" and tok.tag in ("h2", "h3"):
            doc.toc.append({"level": int(tok.tag[1]), "id": tok.attrGet("id"), "text": _plain(tokens[i + 1])})
    body = md.renderer.render(tokens, md.options, {})
    # Wide tables scroll inside the page rather than widening it on a phone.
    body = body.replace("<table>", '<div class="table-wrap"><table>').replace("</table>", "</table></div>")
    doc.html = body
    doc.search = search_entries(doc, tokens)


def _plain(inline) -> str:
    """An inline token's words, without the "#" link the anchors plugin adds to headings."""
    words, skip = [], False
    for c in inline.children or []:
        if c.type == "link_open" and "header-anchor" in (c.attrGet("class") or ""):
            skip = True
        elif c.type == "link_close" and skip:
            skip = False
        elif not skip and c.type in ("text", "code_inline"):
            words.append(c.content)
        elif not skip and c.type == "softbreak":
            words.append(" ")
    return "".join(words).strip()


def search_entries(doc: Doc, tokens) -> list[dict]:
    """The page cut at its headings: what the search box looks through."""
    entries = [{"t": doc.title, "h": "", "u": doc.url, "x": []}]
    for i, tok in enumerate(tokens):
        if tok.type == "heading_open":
            entries.append({"t": doc.title, "h": _plain(tokens[i + 1]), "u": f"{doc.url}#{tok.attrGet('id')}", "x": []})
        elif tok.type == "inline" and (i == 0 or tokens[i - 1].type != "heading_open"):
            entries[-1]["x"].append(_plain(tok))
    for e in entries:
        e["x"] = re.sub(r"\s+", " ", " ".join(e["x"]))[:3000]
    return [e for e in entries if e["x"] or e["h"]]


# -- Files ----------------------------------------------------------------------------------------------------


def png_size(path: Path) -> tuple[int, int]:
    with path.open("rb") as fh:
        head = fh.read(24)
    if head[:8] != b"\x89PNG\r\n\x1a\n":
        return (0, 0)
    return struct.unpack(">II", head[16:24])


def examples() -> list[dict]:
    """The example exports, from the table in examples/README.md (file and what it is)."""
    rows = re.findall(
        r"^\| \[([^\]]+)\]\(([^)]+)\) \| (.+?) \|$", (ROOT / "examples/README.md").read_text("utf-8"), re.M
    )
    return [
        {
            "file": f,
            "href": f"/examples/{f}",
            "text": text,
            "kind": f.rsplit(".", 1)[-1].upper(),
            "title": example_title(name),
        }
        for name, f, text in rows
        if (ROOT / "examples" / f).is_file()
    ]


# File names are lower case; these words aren't.
_WORDS = {
    "monthly security licensing review": "Monthly Security & Licensing Review",
    "mfa": "MFA",
    "all clients monthly review": "Monthly review for all clients",
    "sharepoint": "SharePoint",
    "what changed all clients": "What changed across all clients",
    "conditional access": "Conditional Access",
    "cyber essentials": "Cyber Essentials",
    "sign in": "sign-in",
    "right sizing": "right-sizing",
}


def example_title(name: str) -> str:
    title = name.rsplit(".", 1)[0].replace("-", " ")
    for word, fixed in _WORDS.items():
        title = re.sub(rf"\b{word}\b", fixed, title)
    return title[0].upper() + title[1:]


def version() -> str:
    """The newest released version in CHANGELOG.md, if there is one (not the "unreleased" section above it)."""
    for found in re.finditer(r"^## \[?v?(\d+\.\d+\.\d+)\]?(.*)$", (ROOT / "CHANGELOG.md").read_text("utf-8"), re.M):
        if "unreleased" not in found[2].lower():
            return found[1]
    return ""


# -- Search engines -------------------------------------------------------------------------------------------


def home_data(cfg: dict) -> dict:
    """Schema.org data for the front page: the site's name, and what Office Sentry is and costs."""
    site = cfg["site_url"]
    app = {
        "@type": "SoftwareApplication",
        "@id": f"{site}/#software",
        "name": "Office Sentry",
        "alternateName": "OfficeSentry",
        "description": "Free Microsoft 365 reporting tool for MSPs: security, licensing, MFA, mailbox and SharePoint "
        "reports for every client, with the history kept. Read-only and self-hosted.",
        "url": f"{site}/",
        "applicationCategory": "BusinessApplication",
        "operatingSystem": "Linux (Docker)",
        "isAccessibleForFree": True,
        "offers": {"@type": "Offer", "price": "0", "priceCurrency": "GBP"},
        "license": "https://www.gnu.org/licenses/agpl-3.0.html",
        "image": f"{site}/assets/social-card.png",
        "screenshot": f"{site}/screenshots/tenant-overview.png",
    }
    if released := version():
        app["softwareVersion"] = released
    if cfg["source_public"]:
        app["sameAs"] = [GITHUB]
    site_info = {
        "@type": "WebSite",
        "@id": f"{site}/#website",
        "name": "Office Sentry",
        "alternateName": "OfficeSentry",
        "url": f"{site}/",
    }
    return {"@context": "https://schema.org", "@graph": [site_info, app]}


def breadcrumbs(cfg: dict, doc: Doc) -> dict:
    """Schema.org data for a doc page, so search results show "Docs > page" rather than its address."""
    trail = [("Docs", "/docs/"), (doc.title, doc.url)]
    return {
        "@context": "https://schema.org",
        "@type": "BreadcrumbList",
        "itemListElement": [
            {"@type": "ListItem", "position": i, "name": name, "item": cfg["site_url"] + url}
            for i, (name, url) in enumerate(trail, 1)
        ],
    }


# -- Build ----------------------------------------------------------------------------------------------------


def all_sections() -> list[tuple[str, list[Doc]]]:
    listed = {d.path for _, docs in SECTIONS for d in docs}
    guides = [
        Doc(f"v2/docs/{p.name}", p.stem)
        for p in sorted((ROOT / "v2/docs").glob("*.md"))
        if f"v2/docs/{p.name}" not in listed
    ]
    return SECTIONS + ([(GUIDES, guides)] if guides else [])


def build() -> Path:
    cfg = settings()
    sections = all_sections()
    docs = [d for _, ds in sections for d in ds]
    md, links = markdown(), Links(docs)
    for doc in docs:
        render(md, doc, links)

    if OUT.exists():
        shutil.rmtree(OUT)
    shutil.copytree(HERE / "assets", OUT / "assets")
    for name in ROOT_ASSETS:
        shutil.copy(HERE / "assets" / name, OUT / name)
    for source, published in COPIED.items():
        shutil.copytree(ROOT / source, OUT / published, ignore=shutil.ignore_patterns("*.md"))
    (OUT / "assets/code.css").write_text(code_css(), encoding="utf-8")

    shots = {
        p.stem: {"src": f"/screenshots/{p.name}", "w": png_size(p)[0], "h": png_size(p)[1]}
        for p in sorted((ROOT / "docs/screenshots").glob("*.png"))
    }
    env = Environment(
        loader=FileSystemLoader(HERE / "templates"),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.globals.update(cfg, sections=sections, shots=shots, version=version())

    def page(template: str, path: str, **ctx) -> None:
        target = OUT / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            env.get_template(template).render(path="/" + path.removesuffix("index.html"), **ctx), encoding="utf-8"
        )

    page("home.html", "index.html", examples=examples(), structured_data=home_data(cfg))
    page("docs_index.html", "docs/index.html", title="Documentation")
    for doc in docs:
        page(
            "doc.html",
            f"docs/{doc.slug}/index.html",
            doc=doc,
            title=doc.title,
            edit=f"{GITHUB}/edit/{BRANCH}/{doc.path}",
            structured_data=breadcrumbs(cfg, doc),
        )
    page("404.html", "404.html", title="Page not found")

    (OUT / "assets/search.json").write_text(
        json.dumps([e for d in docs for e in d.search], ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
    )
    urls = ["/", "/docs/"] + [d.url for d in docs]
    (OUT / "sitemap.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        + "".join(f"  <url><loc>{html.escape(cfg['site_url'] + u)}</loc></url>\n" for u in urls)
        + "</urlset>\n",
        encoding="utf-8",
    )
    (OUT / "robots.txt").write_text(
        f"User-agent: *\nAllow: /\nSitemap: {cfg['site_url']}/sitemap.xml\n", encoding="utf-8"
    )
    return OUT


def code_css() -> str:
    """Code colours for light and dark, from Pygments' own styles."""
    light = HtmlFormatter(style="default").get_style_defs(".prose pre")
    dark = HtmlFormatter(style="github-dark").get_style_defs(".prose pre")
    strip = re.compile(r"^.*\{ (?:background|line-height)[^}]*\}\n?", re.M)  # the page sets those
    return strip.sub("", light) + "\n@media (prefers-color-scheme: dark) {\n" + strip.sub("", dark) + "\n}\n"


if __name__ == "__main__":
    out = build()
    pages = sum(1 for _ in out.rglob("*.html"))
    print(f"Built {pages} pages into {out}")
