"""Question sets: the forms MSP engineers fill in, answered per tenant with text they can paste.

Each set lists its questions in the form's own order and wording, by section,
and points each one at a topic in ``evidence_answers.QUESTIONS``. A topic is
answered once per tenant (from the data, or by the engineer for what
Microsoft 365 can't show) and that answer fills every set that asks it, so a
backup answer written for the insurance form is already there for Cyber
Essentials.

To add a set: a ``QuestionSet`` in ``SETS`` whose questions name existing
topics (or new ones in ``evidence_topics``); ``test_questionnaires`` checks
every topic exists. Wording is paraphrased, never copied from an insurer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .evidence_answers import BLANK, NO, NOT_KNOWN, PARTLY, YES, Answer

# What a row shows: the answer, or that the engineer has to answer it.
INPUT, ANSWERED = "Your input", "Answered"


@dataclass(frozen=True)
class Item:
    topic: str     # evidence_answers.QUESTIONS key
    question: str  # as the form asks it
    ref: str = ""  # the form's own number (Cyber Essentials A7.17)
    note: str = ""  # something about this form's version of the question
    autofail: bool = False  # the form fails outright on a "No"
    inverted: bool = False  # the form asks the topic the other way round, so Yes and No swap


@dataclass(frozen=True)
class Part:
    title: str
    items: tuple[Item, ...]


@dataclass(frozen=True)
class QuestionSet:
    key: str
    title: str
    short: str  # the switch at the top of the page
    intro: str
    parts: tuple[Part, ...]
    source: str = ""      # where the questions come from, with version and date
    source_url: str = ""

    @property
    def items(self) -> list[Item]:
        return [i for p in self.parts for i in p.items]


INVERTED = ("The form asks this the other way round, so the answer shown is already swapped (the hoped-for "
            "answer is No).")

INSURANCE = QuestionSet(
    "insurance", "Cyber insurance proposal", "Cyber insurance",
    "The questions UK cyber insurance proposal and renewal forms usually ask. Wording differs between insurers, so "
    "match each answer to the question on the form.",
    (
        Part("Multi-factor authentication and access", (
            Item("mfa_remote", "Is multi-factor authentication (MFA) enforced for all users accessing email, "
                 "including webmail and mobile?"),
            Item("remote_access", "Is MFA required for all remote access to the network, such as VPN, remote "
                 "desktop and remote support tools?"),
            Item("mfa_privileged", "Is MFA enforced for all privileged and administrator accounts?"),
            Item("legacy_auth", "Are legacy authentication protocols that can't use MFA blocked?"),
            Item("separate_admins", "Are administrator accounts separate from the accounts people use every day?"),
            Item("admin_count", "Is administrator access limited to a small number of named people?"),
            Item("leavers", "Are leavers' accounts disabled promptly and inactive accounts removed?"),
        )),
        Part("Email security", (
            Item("email_filtering", "Is incoming email filtered for spam, phishing and malicious links and "
                 "attachments?"),
            Item("email_auth", "Are SPF, DKIM and DMARC configured for your email domains?"),
            Item("forwarding", "Is automatic forwarding of email to external addresses blocked?"),
            Item("payment_verification", "Do you verify requests to change bank details or make payments by "
                 "calling a known contact?"),
        )),
        Part("Devices and patching", (
            Item("edr", "Is endpoint detection and response (EDR) installed and monitored on all computers and "
                 "servers?"),
            Item("devices_patched", "Are all operating systems supported and still receiving security updates, "
                 "with no end-of-life systems?"),
            Item("patching_14_days", "Are critical security updates installed within 14 days of release?",
                 note="Some forms ask about 30 days. Answer to the timescale on the form."),
            Item("devices_encrypted", "Are laptops and desktops encrypted?"),
            Item("devices_managed", "Are company devices centrally managed?"),
        )),
        Part("Backups and recovery", (
            Item("backups", "Are backups taken regularly, kept offline or immutable, separate from your main "
                 "systems, and tested?"),
            Item("bcp_dr", "Do you have a business continuity and disaster recovery plan, and is it tested?"),
            Item("incident_response", "Do you have a documented incident response plan?"),
        )),
        Part("Monitoring and data", (
            Item("audit_logs", "Is audit logging enabled for email and user activity?"),
            Item("risk_detection", "Do you monitor for suspicious sign-ins?"),
            Item("external_sharing", "Is sharing of files with people outside the organisation restricted?"),
            Item("third_party_apps", "Is access to your data by third-party applications controlled?"),
        )),
        Part("Network", (
            Item("firewall_boundary", "Do you have firewalls between your network and the internet?"),
            Item("rdp_exposed", "Is remote desktop (RDP) closed to the internet?"),
        )),
        Part("People", (
            Item("training", "Do staff receive regular security awareness and phishing training?"),
        )),
    ),
)

SUPPLIER = QuestionSet(
    "supplier", "Supplier security questionnaire", "Supplier questionnaire",
    "What clients ask in supplier and vendor due-diligence questionnaires. Use the text for the matching question on "
    "the client's form.",
    (
        Part("Governance", (
            Item("policies", "Do you have documented information security policies?"),
            Item("certifications", "Do you hold any security certifications, such as Cyber Essentials or ISO 27001?"),
        )),
        Part("Access control", (
            Item("mfa_remote", "Is multi-factor authentication required for all users?"),
            Item("mfa_privileged", "Is multi-factor authentication required for privileged accounts?"),
            Item("admins_phishing_resistant", "Do administrators use phishing-resistant MFA?"),
            Item("mfa_methods", "Do you avoid weaker MFA methods such as text messages?"),
            Item("separate_admins", "Are administrative accounts separate from standard user accounts?"),
            Item("admin_count", "Is privileged access restricted and tracked?"),
            Item("shared_accounts", "Does every user have a unique account, with no shared logins?"),
            Item("password_policy", "Describe your password policy."),
            Item("leavers", "How quickly is access removed when someone leaves?"),
            Item("guest_review", "Is access by guests and external users reviewed?"),
        )),
        Part("Data protection", (
            Item("data_location", "Where is our data stored?"),
            Item("encryption", "Is data encrypted at rest and in transit?"),
            Item("external_sharing", "How do you stop data being shared with unauthorised people?"),
            Item("devices_encrypted", "Are laptops and other portable devices encrypted?"),
        )),
        Part("Email security", (
            Item("email_filtering", "How is email protected against phishing and malware?"),
            Item("email_auth", "Do you use SPF, DKIM and DMARC to stop your domains being spoofed?"),
            Item("forwarding", "Is automatic forwarding of email outside the organisation blocked?"),
        )),
        Part("Devices", (
            Item("devices_managed", "Are devices centrally managed and required to meet a security baseline?"),
            Item("devices_patched", "Are all operating systems supported by their vendor?"),
            Item("patching_14_days", "How quickly are security updates applied?"),
            Item("anti_malware", "Is anti-malware software installed and kept up to date on all devices?"),
        )),
        Part("Logging and monitoring", (
            Item("audit_logs", "Are security events logged?"),
            Item("risk_detection", "Do you monitor for and respond to suspicious activity?"),
            Item("vulnerability_scanning", "Do you carry out vulnerability scanning or penetration testing?"),
        )),
        Part("Incidents, backups and continuity", (
            Item("incident_response", "Do you have an incident response plan, and would you tell us about an "
                 "incident affecting our data?"),
            Item("backups", "Is data backed up, and are restores tested?"),
            Item("bcp_dr", "Do you have business continuity and disaster recovery plans?"),
        )),
        Part("Third parties", (
            Item("third_party_apps", "How do you control third-party applications' access to data?"),
            Item("cloud_services", "Which cloud services and sub-processors do you use?"),
        )),
        Part("People and premises", (
            Item("training", "Do staff receive security awareness training?"),
            Item("staff_vetting", "Are staff vetted before they're given access?"),
            Item("physical_security", "How are your premises and equipment secured?"),
        )),
        Part("Network", (
            Item("firewall_boundary", "Is your network protected by firewalls?"),
            Item("remote_access", "Is remote access protected by multi-factor authentication?"),
        )),
    ),
)

CYBER_ESSENTIALS = QuestionSet(
    "cyber-essentials", "Cyber Essentials (Danzell)", "Cyber Essentials",
    "The Cyber Essentials self-assessment questions Microsoft 365 helps with, by their number in the IASME portal, "
    "plus the ones about firewalls, updates and processes it can't see. Questions marked auto-fail fail the whole "
    "assessment on a No.",
    (
        Part("A2 Scope of assessment", (
            Item("remote_workers", "How are home and remote workers connecting to company data and services?",
                 "A2.4.2"),
            Item("device_inventory", "List the quantities and operating systems of your laptops, desktops and virtual "
                 "desktops.", "A2.6", "Give the make and the operating system edition and version (for example "
                 "Windows 11 Pro 24H2)."),
            Item("mobile_inventory", "List the quantities and operating systems of your tablets and mobile devices.",
                 "A2.8"),
            Item("cloud_services", "List all the cloud services in use, provided by a third party.", "A2.9",
                 "Cloud services can't be left out of scope, and social media accounts count."),
        )),
        Part("A4 Firewalls", (
            Item("firewall_boundary", "Do you have firewalls at the boundaries between your networks, devices and "
                 "the internet?", "A4.1"),
            Item("software_firewall", "Do you have software firewalls turned on for all computers, laptops and "
                 "servers?", "A4.1.1"),
            Item("firewall_passwords", "Have you changed the default passwords on your boundary firewalls?", "A4.2"),
            Item("firewall_review", "Have you reviewed your firewall rules in the last 12 months?", "A4.6"),
            Item("firewall_inbound", "Is your firewall set to allow unauthenticated inbound connections?", "A4.7",
                 INVERTED, inverted=True),
            Item("firewall_remote_admin", "Can your boundary firewalls' settings be reached over the internet?",
                 "A4.9", INVERTED + " If they can, A4.10 and A4.11 ask for the business need and MFA.",
                 inverted=True),
        )),
        Part("A5 Secure configuration", (
            Item("software_removal", "Have you removed or turned off software and services you don't use?", "A5.1"),
            Item("leavers", "Do your devices and cloud services only have the user accounts you need and use?",
                 "A5.2"),
            Item("default_passwords", "Have you changed the default passwords on all user and administrator "
                 "accounts on your devices?", "A5.3"),
            Item("autorun", "Have you turned off features that run downloaded or imported files automatically?",
                 "A5.8"),
            Item("device_lock", "Do devices lock and need a PIN, password or biometric to unlock?", "A5.9"),
        )),
        Part("A6 Security update management", (
            Item("devices_patched", "Are all operating systems on your devices supported by a vendor that produces "
                 "regular security updates?", "A6.1", "Windows 10 needs Extended Security Updates after 14 October "
                 "2025. Router and firewall firmware counts as an operating system."),
            Item("patching_14_days", "Are high-risk and critical updates for operating systems, routers and "
                 "firewalls installed within 14 days of release?", "A6.4", autofail=True),
            Item("app_updates", "Are high-risk and critical updates for applications installed within 14 days of "
                 "release?", "A6.5", autofail=True),
        )),
        Part("A7 User access control", (
            Item("account_approval", "Are user accounts only created after an approval process?", "A7.1"),
            Item("shared_accounts", "Are all user and administrator accounts accessed with unique credentials?",
                 "A7.2"),
            Item("leavers", "How do you make sure leavers' accounts are deleted or disabled?", "A7.3"),
            Item("least_privilege", "Do staff only have the access they need for their current job?", "A7.4"),
            Item("admin_process", "Do you have a formal process for giving someone administrator access?", "A7.5"),
            Item("separate_admins", "How do you make sure separate accounts are used for administrative tasks?",
                 "A7.6"),
            Item("separate_admins", "How do you stop administrator accounts being used for everyday tasks like "
                 "email and browsing?", "A7.7"),
            Item("admin_count", "Do you formally track which users have administrator accounts?", "A7.8"),
            Item("admin_review", "Do you review who should have administrative access regularly?", "A7.9"),
            Item("password_policy", "How are passwords protected from brute-force attacks?", "A7.10"),
            Item("compromise_process", "Do you have a process for when passwords or accounts may be compromised?",
                 "A7.13"),
            Item("mfa_available", "Do all of your cloud services offer multi-factor authentication?", "A7.14"),
            Item("mfa_privileged", "Has MFA been applied to all administrators of your cloud services?", "A7.16",
                 "Covers every cloud service that offers MFA, not only Microsoft 365.", autofail=True),
            Item("mfa_remote", "Has MFA been applied to all users of your cloud services?", "A7.17",
                 "Covers every cloud service that offers MFA, not only Microsoft 365.", autofail=True),
        )),
        Part("A8 Malware protection", (
            Item("anti_malware", "Are all desktops, laptops, tablets and phones protected from malware?", "A8.1"),
        )),
    ),
    source="IASME Cyber Essentials question set \"Danzell\" (self-assessment preparation booklet version 16, April "
           "2026), used for assessments bought from 27 April 2026. Numbers checked 9 October 2026; wording "
           "paraphrased. Check each question in the IASME portal before submitting.",
    source_url="https://iasme.co.uk/cyber-essentials/preview-the-self-assessment-questions-for-cyber-essentials/",
)

SETS = {s.key: s for s in (INSURANCE, SUPPLIER, CYBER_ESSENTIALS)}
DEFAULT = INSURANCE.key


# -- A set answered for one tenant ---------------------------------------------------------


@dataclass
class Row:
    item: Item
    part: str
    answer: Answer

    @property
    def state(self) -> str:
        """What the row's label says: the answer, Answered (a list or description), or Your input."""
        a = self.answer
        if a.needs_input or (a.answer == NOT_KNOWN and not a.override):
            return INPUT
        if a.topic and a.topic.kind == "text":
            return ANSWERED
        if self.item.inverted:
            return {YES: NO, NO: YES}.get(a.answer, a.answer)
        return a.answer

    @property
    def tone(self) -> str:
        return {YES: "ok", ANSWERED: "ok", PARTLY: "warn", NO: "bad", INPUT: "input"}.get(self.state, "")

    @property
    def label(self) -> str:
        return f"{self.item.ref} {self.item.question}".strip()

    @property
    def beyond(self) -> str:
        """What the engineer still has to check: the topic's limits and this form's own note."""
        a = self.answer
        parts = [] if a.needs_input else [a.topic.beyond if a.topic else ""]
        return " ".join(p for p in parts + [self.item.note] if p)

    @property
    def segments(self) -> list[tuple[str, bool]]:
        """The text to paste in pieces, each [part to fill in] marked, for the page to highlight."""
        text, out, at = self.answer.text, [], 0
        for m in BLANK.finditer(text):
            out += [(text[at:m.start()], False), (m.group(0), True)]
            at = m.end()
        return [(t, blank) for t, blank in out + [(text[at:], False)] if t]

    @property
    def based_on(self) -> str:
        """The evidence behind the text, unless the text already is the evidence."""
        a = self.answer
        if a.override:
            return f"Answered by {a.override.set_by}."
        if a.needs_input:
            return a.why
        return a.why if a.text != a.why and not a.text.endswith(a.why) else ""


@dataclass
class Sheet:
    qset: QuestionSet
    tenant: dict
    generated_at: datetime
    oldest_data: str | None
    rows: list[Row] = field(default_factory=list)

    @property
    def parts(self) -> list[tuple[str, list[Row]]]:
        out: dict[str, list[Row]] = {}
        for r in self.rows:
            out.setdefault(r.part, []).append(r)
        return list(out.items())

    def showing(self, show: str = "") -> list[tuple[str, list[Row]]]:
        """``parts`` with only the rows needing input (``show="input"``) or answered Partly or No ("attention")."""
        keep = {"input": lambda r: r.state == INPUT, "attention": lambda r: r.state in (PARTLY, NO)}.get(show)
        if keep is None:
            return self.parts
        return [(part, kept) for part, rows in self.parts if (kept := [r for r in rows if keep(r)])]

    @property
    def counts(self) -> dict[str, int]:
        """The headline: answered from Microsoft 365, answered by the team, needing attention, needing input."""
        out = {"data": 0, "team": 0, "attention": 0, "input": 0}
        for r in self.rows:
            if r.state == INPUT:
                out["input"] += 1
            elif r.answer.override:
                out["team"] += 1
            else:
                out["data"] += 1
            if r.state in (PARTLY, NO):
                out["attention"] += 1
        return out

    @property
    def breakdown(self) -> list[tuple[str, int, str]]:
        """Every row once, by what it shows, for the PDF's bar: (label, count, report.css tone)."""
        n = {state: sum(1 for r in self.rows if r.state == state) for state in (YES, ANSWERED, PARTLY, NO, INPUT)}
        return [("Yes or answered", n[YES] + n[ANSWERED], "ok"), ("Partly", n[PARTLY], "medium"),
                ("No", n[NO], "critical"), (INPUT, n[INPUT], "none")]

    @property
    def blanks(self) -> int:
        """Rows whose text still has [parts] to fill in."""
        return sum(1 for r in self.rows if r.answer.blanks)


def sheet(pack, key: str) -> Sheet:
    """Question set ``key`` answered from an ``evidence.EvidencePack`` (its answers, with overrides)."""
    qset = SETS[key]
    by_key = {a.key: a for a in pack.answers}
    rows = [Row(i, p.title, by_key[i.topic]) for p in qset.parts for i in p.items]
    return Sheet(qset, pack.tenant, pack.generated_at, pack.oldest_data, rows)


# -- Exports -------------------------------------------------------------------------------

COLUMNS = ["Ref", "Section", "Question", "Answer", "Text to paste", "Based on", "Still to check", "Answered by",
           "Data date", "Evidence"]


def _day(value: str | None) -> str:
    if not value:
        return ""
    when = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return f"{when.day} {when:%b %Y}"


def answered_by(a: Answer) -> str:
    if a.override:
        return f"{a.override.set_by}, {_day(a.override.set_at)}"
    return "Needs your input" if a.needs_input else "Suggested from Microsoft 365 data"


def rows(s: Sheet) -> list[list[str]]:
    out = []
    for r in s.rows:
        a = r.answer
        source = (a.source.label + (" report" if a.source.report else "")) if a.source and not a.needs_input else ""
        out.append([r.item.ref, r.part, r.item.question, r.state, a.text, r.based_on, r.beyond, answered_by(a),
                    "" if a.needs_input else _day(a.override.set_at if a.override else a.as_of), source])
    return out


def to_xlsx_tables(sets: list[Sheet]) -> list[tuple[str, list[list[str]], str]]:
    """(sheet name, rows, description) for each set, for ``evidence.to_xlsx``."""
    return [(s.qset.short, rows(s), s.qset.title + ". " + (s.qset.source or s.qset.intro)) for s in sets]


def to_text(s: Sheet, brand_name: str = "") -> str:
    """The whole set as plain text, ready to paste from: what "Copy all answers" copies and the .txt holds."""
    lines = [f"{s.qset.title}: {s.tenant['name']}",
             f"Prepared {s.generated_at.day} {s.generated_at:%B %Y}" + (f" by {brand_name}" if brand_name else "")
             + ". Answers marked [Your input] need filling in.", ""]
    if s.qset.source:
        lines += [f"Questions: {s.qset.source}", ""]
    for part, part_rows in s.parts:
        lines += [part.upper(), ""]
        for r in part_rows:
            a = r.answer
            lines.append(r.label + (" (auto-fail)" if r.item.autofail else ""))
            lines.append(f"Answer: {r.state}")
            lines.append(a.text)
            if r.based_on and not a.override:
                lines.append(f"Based on: {r.based_on}")
            if r.beyond:
                lines.append(f"Still to check: {r.beyond}")
            date = a.override.set_at if a.override else (None if a.needs_input else a.as_of)
            if date:
                lines.append(f"{'Answered' if a.override else 'Microsoft 365 data from'} {_day(date)}"
                             + (f" by {a.override.set_by}" if a.override else ""))
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"
