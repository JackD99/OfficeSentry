"""Release notes on the Updates page: the little Markdown CHANGELOG.md uses, drawn safely.

Paragraphs, bullet lists, pipe tables, ``code``, **bold**, *italic* and links. Everything is escaped
first, links only go to https addresses, and a link relative to the repository (``v2/DEPLOY.md#updating``)
opens that file on GitHub as it was in the release. Anything else shows as plain text.
"""

from __future__ import annotations

import re

from markupsafe import Markup, escape

from ..updates import REPOSITORY

BULLET = re.compile(r"^\s*[-*]\s+(.*)$")
HEADING = re.compile(r"^#{1,6}\s+(.*)$")
TABLE_RULE = re.compile(r"^\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$")
CODE = re.compile(r"`([^`]+)`")
LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
BOLD = re.compile(r"\*\*(.+?)\*\*")
ITALIC = re.compile(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])")


def href(target: str, version: str) -> str | None:
    """Where a link in version ``version``'s notes goes, or None to show its text alone."""
    if target.startswith("https://"):
        return target
    if re.match(r"^[a-z][a-z0-9+.-]*:|^//|^#", target, re.I) or ".." in target:
        return None
    return f"https://github.com/{REPOSITORY}/blob/v{version}/{target.removeprefix('./')}"


def inline(text: str, version: str) -> Markup:
    """One line's worth of Markdown as HTML. The text is escaped before anything is turned into a tag."""
    codes: list[str] = []

    def keep(match: re.Match) -> str:
        codes.append(f"<code>{match[1]}</code>")
        return f"\x00{len(codes) - 1}\x00"

    def link(match: re.Match) -> str:
        url = href(match[2].replace("&amp;", "&"), version)
        if url is None:
            return match[1]
        return f'<a href="{escape(url)}" rel="noopener">{match[1]}</a>'

    html = CODE.sub(keep, str(escape(text.replace("\x00", ""))))  # NUL marks the code spans kept aside
    html = LINK.sub(link, html)
    html = BOLD.sub(r"<strong>\1</strong>", html)
    html = ITALIC.sub(r"<em>\1</em>", html)
    html = re.sub(r"\x00(\d+)\x00", lambda m: codes[int(m[1])], html)
    return Markup(html)  # noqa: S704 (built from escaped text)


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def render(text: str, version: str) -> Markup:
    """A section of release notes as HTML."""
    out: list[str] = []
    paragraph: list[str] = []
    items: list[list[str]] = []
    rows: list[list[str]] = []

    def close() -> None:
        if paragraph:
            out.append(f"<p>{inline(' '.join(paragraph), version)}</p>")
            paragraph.clear()
        if items:
            out.append("<ul>" + "".join(f"<li>{inline(' '.join(i), version)}</li>" for i in items) + "</ul>")
            items.clear()
        if rows:
            head, *body = rows
            cells = "".join(f"<th>{inline(c, version)}</th>" for c in head)
            lines = "".join("<tr>" + "".join(f"<td>{inline(c, version)}</td>" for c in r) + "</tr>" for r in body)
            out.append(
                f'<div class="scroll-x"><table><thead><tr>{cells}</tr></thead><tbody>{lines}</tbody></table></div>'
            )
            rows.clear()

    for line in (text or "").replace("\r\n", "\n").split("\n"):
        if not line.strip():
            close()
        elif line.lstrip().startswith("|"):
            if not rows:
                close()
            if not TABLE_RULE.match(line.strip()):
                rows.append(_cells(line))
        elif HEADING.match(line):
            close()
            out.append(f"<p><strong>{inline(HEADING.match(line)[1], version)}</strong></p>")
        elif BULLET.match(line):
            if paragraph or rows:
                close()
            items.append([BULLET.match(line)[1]])
        elif items and line.startswith((" ", "\t")):
            items[-1].append(line.strip())
        else:
            if items or rows:
                close()
            paragraph.append(line.strip())
    close()
    return Markup("\n".join(out))  # noqa: S704 (each piece is escaped by inline())
