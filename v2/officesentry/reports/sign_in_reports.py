"""Sign-in activity: who signed in, from where, with what, and what failed.

Reads the summaries ``collectors/sign_ins.py`` stores (never the raw log) and
leads with what a technician acts on: accounts and addresses with repeated
failures (password guessing), successful sign-ins from countries the tenant
rarely uses, sign-ins that needed only a password, and risky sign-ins.
"""

from __future__ import annotations

import re
from datetime import datetime

from .. import signins
from ..signins import REPEATED_FAILURES, SPRAY_ACCOUNTS
from ..snapshot import TenantSnapshot
from ..text import plural
from .data_reports import DataReport, Rows, Section, Tile

NOT_COLLECTED = "Sign-in activity hasn't been collected yet. Refresh \"Sign-in activity\" on the tenant page."
NO_P1 = ("Sign-in activity comes from the Entra ID sign-in log, which needs Entra ID P1 (included in Microsoft 365 "
         "Business Premium, E3 and E5). This tenant doesn't have it. The Inactive accounts report still lists "
         "accounts by when they were created and licensed.")
NO_PERMISSION = ("Office Sentry can't read this tenant's sign-in log. It needs the AuditLog.Read.All permission: "
                 "open the tenant page, send the consent link to the client's Global Admin again and ask them to "
                 "accept.")
NO_P2 = ("Risky sign-ins need Entra ID P2 (Identity Protection, included in Microsoft 365 E5 or as an add-on); "
         "without it Microsoft hides sign-in risk.")

REPEATED = "Accounts with repeated failures"
ADDRESSES = "Addresses with many failures"
UNUSUAL = "Successful sign-ins from unusual countries"
NO_MFA = "Successful sign-ins without MFA"
RISKY = "Risky sign-ins"
REASONS = "Failure reasons"
COUNTRIES = "Sign-ins by country"
APPS = "Sign-ins by app"
EVERYONE = "Every account"

_TIME = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}")


def _n(value) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _text(value) -> str:
    return value if isinstance(value, str) else ""


def _utc(value) -> str:
    """'2026-09-30 10:04' from an ISO time; blank when it isn't one."""
    return value[:16].replace("T", " ") if isinstance(value, str) and _TIME.match(value) else ""


def _list(value) -> list[str]:
    return [str(v) for v in value if v is not None] if isinstance(value, list) else []


def _detail(value) -> str:
    """Entra ID Protection's riskDetail, blank for "none"."""
    return "" if value == "none" else _text(value)


def _share(part: int, whole: int) -> str:
    if not whole:
        return ""
    share = part / whole
    return "Under 1%" if 0 < share < 0.005 else f"{share:.0%}"


def _rows(snap: TenantSnapshot, kind: str) -> list[dict]:
    return [r for r in snap.objects("sign_ins", kind) or [] if isinstance(r, dict)]


def sign_in_report(snap: TenantSnapshot, now: datetime, report: DataReport) -> None:
    status = signins.status(snap)
    if status is None:
        report.unavailable = NOT_COLLECTED
        return
    if not status.get("available"):
        report.unavailable = NO_PERMISSION if status.get("reason") == "permission" else NO_P1
        return
    days = _n(status.get("days")) or signins.WINDOW_DAYS
    accounts = _rows(snap, "sign_in_account")
    unusual = signins.unusual_countries(snap)
    risk_visible = bool(status.get("riskVisible"))
    sections: list[Section] = []

    # -- Risky sign-ins (Entra ID P2) --------------------------------------------------------------
    risky_ok = 0
    if risk_visible:
        risky = Section(RISKY, ["When (UTC)", "Account", "Risk level", "Risk state", "Result", "Detections",
                                "Country", "IP address", "App"], compare=False,
                        intro="Sign-ins Entra ID Protection thought weren't made by the account's owner. Check "
                              "each one with the person; if they don't recognise it, reset the password, sign "
                              "them out everywhere and check their MFA methods and inbox rules.",
                        empty=f"No risky sign-ins in the last {days} days.")
        for r in sorted(_rows(snap, "risky_sign_in"), key=lambda r: _text(r.get("time")), reverse=True):
            ok = r.get("succeeded") is True
            risky_ok += ok
            severity = signins.risk_severity(r.get("level"), r.get("state"))
            risky.add([_utc(r.get("time")), r.get("user"), _text(r.get("level")).capitalize() or "Unknown",
                       signins.RISK_STATES.get(_text(r.get("state")), _text(r.get("state"))),
                       "Signed in" if ok else signins.meaning(r.get("errorCode")),
                       _list(r.get("eventTypes")) or _detail(r.get("detail")),
                       signins.country(r.get("country")),
                       r.get("ip"), r.get("app")],
                      (severity or "low") if ok else "low")
        sections.append(risky)
    else:
        report.notes.append(NO_P2)

    # -- Repeated failures ----------------------------------------------------------------------------
    repeated = Section(REPEATED, ["Account", "Failed", "Wrong password", "Locked out", "Succeeded", "Addresses",
                                  "Countries", "Last failure (UTC)", "Last failure reason"], compare=False,
                       intro=f"Accounts that failed to sign in {REPEATED_FAILURES} or more times in the last "
                             f"{days} days (MFA and other prompts aren't counted). Many wrong passwords from "
                             "several addresses or countries is someone guessing the password. If the person "
                             "didn't make them, make sure MFA is enforced for the account and check any success "
                             "that followed.",
                       empty=f"No account failed to sign in {REPEATED_FAILURES} or more times.")
    for a in sorted(accounts, key=lambda a: (-_n(a.get("failed")), _text(a.get("user")).lower())):
        if _n(a.get("failed")) < REPEATED_FAILURES:
            continue
        flag = "high" if _n(a.get("lockouts")) or _n(a.get("credentialFailures")) >= 20 else "medium"
        repeated.add([a.get("user"), _n(a.get("failed")), _n(a.get("credentialFailures")), _n(a.get("lockouts")),
                      _n(a.get("succeeded")), _n(a.get("failureIpCount")), _list(a.get("countries")),
                      _utc(a.get("lastFailure")),
                      signins.meaning(a.get("lastFailureCode"), a.get("lastFailureReason"))
                      if a.get("lastFailure") else ""], flag)
    sections.append(repeated)

    # -- Addresses that failed against many accounts ---------------------------------------------------
    addresses = Section(ADDRESSES, ["IP address", "Failed sign-ins", "Wrong password", "Accounts tried", "Accounts",
                                    "Countries", "Successful sign-ins from here", "Last failure (UTC)"],
                        compare=False,
                        intro=f"Addresses that failed against several accounts, or {REPEATED_FAILURES} or more times. "
                              f"One address trying {SPRAY_ACCOUNTS} or more accounts is password spraying. "
                              "Successful sign-ins from the same address can be the office's own internet "
                              "connection (people mistyping), or a guess that worked: check which.",
                        empty="No address failed against several accounts.")
    for i in sorted(_rows(snap, "sign_in_failure_ip"),
                    key=lambda i: (-_n(i.get("accounts")), -_n(i.get("failures")))):
        tried, failures = _n(i.get("accounts")), _n(i.get("failures"))
        if tried < 2 and failures < REPEATED_FAILURES:
            continue
        addresses.add([i.get("ip"), failures, _n(i.get("credentialFailures")), tried, _list(i.get("users")),
                       _list(i.get("countries")), _n(i.get("succeeded")), _utc(i.get("lastSeen"))],
                      "high" if tried >= SPRAY_ACCOUNTS else "medium")
    sections.append(addresses)

    # -- Unusual countries -----------------------------------------------------------------------------
    home = sorted({_text(o.get("countryLetterCode")) for o in snap.objects("tenant_profile", "organization") or []
                   if _text(o.get("countryLetterCode"))})
    where = f"other than {', '.join(home)} (the organisation's country)" if home else "other than the main one"
    rare = Section(UNUSUAL, ["Account", "Country", "Successful sign-ins", "Failed", "Cities", "IP addresses", "Apps",
                             "Last success (UTC)"], compare=False,
                   intro=f"Countries with less than {signins.RARE_SHARE:.0%} of the tenant's successful sign-ins, "
                         f"{where}. Travel explains most. If the person doesn't recognise one, someone else has "
                         "their password (and got past any MFA): reset it and sign them out everywhere.",
                   empty="No successful sign-ins from countries the tenant rarely uses.")
    rare_accounts = set()
    for loc in sorted(_rows(snap, "sign_in_location"),
                      key=lambda r: (_text(r.get("country")), _text(r.get("user")).lower())):
        if _text(loc.get("country")) not in unusual or not _n(loc.get("succeeded")):
            continue
        rare_accounts.add(_text(loc.get("user")).lower())
        rare.add([loc.get("user"), loc.get("country"), _n(loc.get("succeeded")), _n(loc.get("failed")),
                  _list(loc.get("cities")), _list(loc.get("ips")), _list(loc.get("apps")),
                  _utc(loc.get("lastSuccess"))], "medium")
    sections.append(rare)

    # -- Single-factor sign-ins ---------------------------------------------------------------------
    single = Section(NO_MFA, ["Account", "Without MFA", "With MFA", "Apps", "Countries", "Last success (UTC)"],
                     compare=False,
                     intro="Successful sign-ins that needed only a password. Conditional Access exclusions, trusted "
                           "locations and apps a policy doesn't cover show up here; the MFA coverage report shows "
                           "why each account isn't asked. Flagged: accounts that never used MFA in this period.",
                     empty="Every successful sign-in used MFA.")
    single_count = 0
    for a in sorted(accounts, key=lambda a: (-_n(a.get("singleFactor")), _text(a.get("user")).lower())):
        n = _n(a.get("singleFactor"))
        if not n:
            continue
        single_count += n
        single.add([a.get("user"), n, _n(a.get("mfa")), _list(a.get("apps")), _list(a.get("countries")),
                    _utc(a.get("lastSuccess"))], "low" if _n(a.get("mfa")) else "medium")
    sections.append(single)

    # -- Failure reasons --------------------------------------------------------------------------
    reasons = Section(REASONS, ["Error code", "What it means", "Kind", "Sign-ins", "Accounts", "Last seen (UTC)"],
                      compare=False,
                      intro="Why sign-ins didn't complete. Prompts (MFA, \"Stay signed in?\") are recorded by "
                            "Microsoft as failures but are part of a normal sign-in.",
                      empty="Every sign-in succeeded.")
    failed = interrupted = 0
    for f in sorted(_rows(snap, "sign_in_failure"), key=lambda f: -_n(f.get("signIns"))):
        error = signins.code(f.get("code"))
        kind = signins.outcome(error)
        if kind == "interrupted":
            interrupted += _n(f.get("signIns"))
        else:
            failed += _n(f.get("signIns"))
        reasons.add([str(error), signins.meaning(error, f.get("reason")),
                     "Prompt" if kind == "interrupted" else "Failure", _n(f.get("signIns")), _n(f.get("accounts")),
                     _utc(f.get("lastSeen"))], "medium" if error == signins.LOCKOUT else None)
    sections.append(reasons)

    # -- By country and app ------------------------------------------------------------------------
    countries = _rows(snap, "sign_in_country")
    successes = sum(_n(c.get("succeeded")) for c in countries)
    by_country = Section(COUNTRIES, ["Country", "Sign-ins", "Succeeded", "Failed", "Accounts", "Share of successes",
                                     "Unusual"], compare=False,
                         intro="Where sign-ins came from, by Microsoft's location for the address. Flagged: "
                               "countries the tenant rarely signs in from.", empty="No sign-ins.")
    for c in sorted(countries, key=lambda c: -_n(c.get("signIns"))):
        odd = _text(c.get("country")) in unusual
        by_country.add([signins.country(c.get("country")), _n(c.get("signIns")), _n(c.get("succeeded")),
                        _n(c.get("failed")), _n(c.get("accounts")), _share(_n(c.get("succeeded")), successes), odd],
                       "low" if odd else None)
    sections.append(by_country)

    by_app = Section(APPS, ["App", "Sign-ins", "Succeeded", "Failed", "Without MFA", "Accounts"], compare=False,
                     intro="What people signed in to. \"Without MFA\" counts successful sign-ins that needed only a "
                           "password.", empty="No sign-ins.")
    for a in sorted(_rows(snap, "sign_in_app"), key=lambda a: -_n(a.get("signIns"))):
        by_app.add([a.get("app") or "Unknown app", _n(a.get("signIns")), _n(a.get("succeeded")), _n(a.get("failed")),
                    _n(a.get("singleFactor")), _n(a.get("accounts"))])
    sections.append(by_app)

    everyone = Section(EVERYONE, ["Account", "Sign-ins", "Succeeded", "Failed", "Prompts", "With MFA", "Without MFA",
                                  "Countries", "Apps", "Last sign-in (UTC)", "Last failure reason"], compare=False,
                       intro=f"Every account that signed in interactively in the last {days} days. Open an account "
                             "for the rest of what Office Sentry knows about it.",
                       empty=f"No interactive sign-ins in the last {days} days.")
    for a in sorted(accounts, key=lambda a: _text(a.get("user")).lower()):
        everyone.add([a.get("user"), _n(a.get("signIns")), _n(a.get("succeeded")), _n(a.get("failed")),
                      _n(a.get("interrupted")), _n(a.get("mfa")), _n(a.get("singleFactor")),
                      _list(a.get("countries")), _list(a.get("apps")), _utc(a.get("lastSignIn")),
                      signins.meaning(a.get("lastFailureCode"), a.get("lastFailureReason"))
                      if a.get("lastFailure") else ""],
                     "medium" if _n(a.get("failed")) >= REPEATED_FAILURES else None)
    sections.append(everyone)
    report.sections = sections

    # -- Headline figures ----------------------------------------------------------------------------
    total = _n(status.get("signIns")) or sum(_n(a.get("signIns")) for a in accounts)
    attempts = total - interrupted
    rate = failed / attempts if attempts > 0 else 0
    report.tiles = [
        Tile("Sign-ins", total, f"Interactive, last {days} days, by {plural(len(accounts), 'account')}",
             rows=Rows(EVERYONE)),
        Tile("Failure rate", _share(failed, attempts) or "0%",
             f"{plural(failed, 'failed sign-in')}; {plural(interrupted, 'MFA or other prompt')} not counted",
             "warn" if rate >= 0.1 else None, Rows(REASONS, {"Kind": "Failure"})),
        Tile("Repeated failures", len(repeated.rows), f"Accounts with {REPEATED_FAILURES}+ failed sign-ins",
             ("bad" if repeated.flagged and "high" in repeated.flags else "warn") if repeated.rows else "ok",
             Rows(REPEATED)),
        Tile("Countries", len(countries), f"{plural(len(unusual), 'country')} unusual for this tenant",
             rows=Rows(COUNTRIES)),
        Tile("Unusual countries", len(rare_accounts), "Accounts that signed in from one",
             "warn" if rare_accounts else "ok", Rows(UNUSUAL)),
        Tile("Without MFA", single_count, f"Successful sign-ins by {plural(len(single.rows), 'account')}",
             "warn" if single_count else "ok", Rows(NO_MFA)),
    ]
    if risk_visible:
        report.tiles.append(Tile("Risky sign-ins", len(risky.rows), f"{risky_ok} succeeded",
                                 "bad" if risky_ok else ("warn" if risky.rows else "ok"), Rows(RISKY)))
    since = _utc(status.get("since"))[:10]
    report.footnote = (f"Interactive sign-ins {'since ' + since if since else f'in the last {days} days'}, from "
                       "the Entra ID sign-in log. Background sign-ins by apps and saved sessions aren't included. "
                       "Times are UTC.")
    if status.get("truncated"):
        report.notes.append(f"The tenant had more than {total:,} sign-ins in {days} days; the most recent "
                            f"{total:,} are summarised.")


REPORTS = {
    "sign-ins": ("Sign-in activity",
                 "Failed and successful sign-ins over the last week by account, country and app: password "
                 "guessing, unusual countries, sign-ins without MFA and risky sign-ins.",
                 ("sign_ins", "tenant_profile"), sign_in_report),
}
