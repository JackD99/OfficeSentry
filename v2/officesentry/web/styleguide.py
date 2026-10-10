"""The style guide: every design token (static/tokens.css) and every shared component, with made-up data.

Staff only. ``/styleguide`` is the portal's components (base.html, app.css); ``/styleguide/report`` the client
report components (reports/_components.html, report.css), also as a PDF. tests/browser screenshots both, so
a change to a token or a component shows up there before it shows up on a client's page.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from flask import Blueprint, render_template, request, url_for

from ..reports import summary, views
from ..reports.branding import contrast
from ..reports.data_reports import Chart, DataReport, Highlight, Link, Section, Tile, Trend
from .access import roles_required
from .printing import pdf_response

bp = Blueprint("styleguide", __name__)

TOKENS = Path(__file__).parent / "static" / "tokens.css"
# Text colours and the surface they're measured on: the contrast column on the colour swatches.
TEXT = {"text", "text-2", "muted", "faint", "accent", "ok", "warn", "bad", "info",
        "sev-critical", "sev-high", "sev-medium", "sev-low", "sidebar-text", "sidebar-muted"}


@dataclass
class Token:
    name: str
    light: str
    dark: str
    note: str = ""

    @property
    def colour(self) -> bool:
        return bool(re.fullmatch(r"#[0-9a-fA-F]{6}", self.light))

    def ratios(self) -> tuple[float, float] | None:
        """Contrast with the surface it sits on, light and dark; None for colours that aren't text."""
        if self.name not in TEXT:
            return None
        surface = ("sidebar", "sidebar") if self.name.startswith("sidebar") else ("surface", "surface")
        light, dark = parse()
        return (contrast(self.light, light[surface[0]]), contrast(self.dark, {**light, **dark}[surface[1]]))


@dataclass
class Group:
    title: str
    tokens: list[Token] = field(default_factory=list)

    @property
    def colours(self) -> bool:
        return any(t.colour for t in self.tokens)


def parse(css: str | None = None) -> tuple[dict[str, str], dict[str, str]]:
    """(light, dark) values by token name (without the leading --) from tokens.css."""
    css = css if css is not None else TOKENS.read_text(encoding="utf-8")
    light_block = css[css.index(":root {"):]
    light_block = light_block[:light_block.index("\n}")]
    dark_start = css.index(":root:not(", css.index("@media (prefers-color-scheme: dark)"))
    dark_block = css[dark_start:css.index("}", dark_start)]
    return _decls(light_block), _decls(dark_block)


def _decls(block: str) -> dict[str, str]:
    block = re.sub(r"/\*.*?\*/", "", block, flags=re.S)
    return {m.group(1): m.group(2).strip() for m in re.finditer(r"--([a-z0-9-]+)\s*:\s*([^;]+);", block)}


def groups() -> list[Group]:
    """The tokens in tokens.css order, under its comment headings, with any trailing comment as a note."""
    css = TOKENS.read_text(encoding="utf-8")
    light, dark = parse(css)
    block = css[css.index(":root {"):]
    block = block[:block.index("\n}")]
    out: list[Group] = []
    for line in block.splitlines()[1:]:
        heading = re.fullmatch(r"\s*/\*\s*(.*?)(?:[:.(].*)?\s*\*/\s*", line)
        if heading:
            out.append(Group(heading.group(1).strip()))
            continue
        note = re.search(r"/\*\s*(.*?)\s*\*/", line)
        for name in re.findall(r"--([a-z0-9-]+)\s*:", line):
            if out:
                out[-1].tokens.append(Token(name, light[name], dark.get(name, light[name]),
                                            note.group(1) if note else ""))
    return [g for g in out if g.tokens]


# -- Made-up data for the components --------------------------------------------------------------


def _tenant() -> SimpleNamespace:
    return SimpleNamespace(id=0, name="Fabrikam Dental Group", tenant_ref="fabrikamdental.onmicrosoft.com",
                           cloud="commercial", enabled=1, archived_at=None)


def _report() -> DataReport:
    """A small detail report: two tables (one flagged and grouped), tiles and highlights."""
    now = datetime.now(UTC)
    day = (now - timedelta(days=2)).date().isoformat()  # report tables show dates as ISO dates
    people = Section(
        "Accounts", ["Name", "Department", "MFA", "Last sign-in", "Licences"],
        rows=[["Adele Vance", "Finance", "Not registered", day, 2],
              ["Ben Walters", "Reception", "Authenticator app", day, 1],
              ["Carla Ruiz", "Clinical", "Passkey", "Never", 1],
              ["Dev Patel", "IT", "Not registered", day, 3]],
        flags=["high", None, None, "critical"], flag_name="Risk", filter_columns=("Department", "MFA"),
        intro="Everyone with a mailbox or a licence. Flagged rows need a look.",
        chart=Chart("MFA", ("Licences",), title="Licences by MFA method"))
    files = Section(
        "Shared files", ["Site", "File", "Shared with"],
        rows=[["Finance", Link("Budget 2027.xlsx", "https://contoso.example/budget"), "Anyone with the link"],
              ["Finance", "Payroll.csv", "accountant@partner.example"],
              ["Clinical", "Rota.docx", "Everyone in the organisation"]],
        flags=["medium", "high", None], group="Site")
    return DataReport(
        key="styleguide", title="Example report", description="Made-up rows for the style guide.",
        tenant={"id": 0, "name": _tenant().name, "tenant_ref": _tenant().tenant_ref}, generated_at=now,
        sections=[people, files], sources=[("Users", now.strftime("%Y-%m-%dT%H:%M:%SZ")), ("Sharing", None)],
        tiles=[Tile("Accounts", 4, "1 without a licence",
                    trend=Trend([3, 3, 3, 4, 4, 4, 4], "3", "4", "12 Jul 2026")),
               Tile("Without MFA", 2, "of 4", tone="bad", trend=Trend([4, 4, 3, 3, 3, 2, 2], "4", "2", "12 Jul 2026")),
               Tile("Shared files", 3, tone="warn"), Tile("Passkeys", "25%", tone="ok")],
        highlights=[Highlight("Admins without MFA", 1, tone="bad"), Highlight("Guests", 0, tone="ok")],
        notes=["Sign-in times need an Entra ID P1 licence; two people have none."])


def _actions() -> list:
    return [summary.Action("Register MFA for the two people without it", "They can sign in with a password alone",
                           "critical", anchor="accounts"),
            summary.Action("Stop sharing Payroll.csv outside the company", "", "high")]


@bp.get("/styleguide")
@roles_required("admin", "analyst")
def index():
    report = _report()
    view = views.View.parse(request.args)
    path = url_for("styleguide.index")
    return render_template(
        "styleguide.html", groups=groups(), tenant=_tenant(), report=report,
        tables=views.table_views(report, view, path), links=views.headline_links(report, path),
        three_days_ago=(datetime.now(UTC) - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        report_groups=[SimpleNamespace(key="identity", title="Identity and access",
                                       reports=[("mfa", "MFA coverage", ""), ("admins", "Admins and roles", "")]),
                       SimpleNamespace(key="email", title="Email",
                                       reports=[("mailboxes", "Mail forwarding", "")])])


@bp.get("/styleguide/report")
@roles_required("admin", "analyst")
def report_components():
    return render_template("styleguide_report.html", pdf=False, css_href=url_for("static", filename="report.css"),
                           **_report_context())


@bp.get("/styleguide/report.pdf")
@roles_required("admin", "analyst")
def report_components_pdf():
    return pdf_response("styleguide_report.html", "style-guide", **_report_context())


def _report_context() -> dict:
    return {"report": views.figures(_report()), "actions": _actions(), "verdicts": [
        summary.Verdict(tone="critical", label="Needs action", headline="Two people can sign in with a password"
                        " alone, one of them an admin."),
        summary.Verdict(tone="high", label="Attention", headline="Files are shared outside the company."),
        summary.Verdict(tone="medium", label="Mostly fine", headline="A few settings to tidy up."),
        summary.Verdict(tone="ok", label="In good shape", headline="Nothing needs action this month.")]}
