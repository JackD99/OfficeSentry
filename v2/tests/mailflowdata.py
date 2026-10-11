"""Mail flow fixtures: message trace rows for mail in, out and between colleagues, and Microsoft's top spam and
phishing recipients summary, shaped like the Exchange admin API's answers.

``message_trace()`` answers Get-MessageTraceV2 as Microsoft does: a distribution list query (Status
"Expanded") gets the list expansions, any other query the rows received between its StartDate and EndDate.
"""

from datetime import UTC, datetime, timedelta


def _ts(when: datetime) -> str:
    return when.strftime("%Y-%m-%dT%H:%M:%S.0000000Z")


def ago(hours: float) -> str:
    return _ts(datetime.now(UTC) - timedelta(hours=hours))


def row(n: int, sender: str, recipient: str, status: str, received: str) -> dict:
    return {
        "Organization": "x.onmicrosoft.com",
        "MessageId": f"<flow{n}@mail.example>",
        "Received": received,
        "SenderAddress": sender,
        "RecipientAddress": recipient,
        "Subject": f"Private subject {n}",
        "Status": status,
        "Size": 1000 + n,
        "MessageTraceId": f"f000000{n}-aaaa-bbbb-cccc-000000000000",
    }


TRACE = [
    row(1, "news@outside.example", "ann@x.com", "Delivered", ago(3)),
    row(2, "news@outside.example", "bob@x.com", "Delivered", ago(3)),
    row(3, "offers@spam.example", "ann@x.com", "FilteredAsSpam", ago(5)),
    row(4, "bank@phish.example", "ann@x.com", "Quarantined", ago(30)),
    row(5, "ann@x.com", "client@partner.example", "Delivered", ago(30)),
    row(6, "ann@x.com", "nobody@gone.example", "Failed", ago(50)),
    row(7, "ann@x.com", "bob@x.com", "Delivered", ago(50)),
    row(8, "news@outside.example", "everyone@x.com", "Expanded", ago(50)),  # a list handing it on: not counted
    row(9, "relay@outside.example", "someone@else.example", "Delivered", ago(50)),  # not one of ours
    row(10, "ann@x.com", "bob@x.com", "Delivered", ago(0.5)),  # within the hour the trace runs behind
]

TOP = {
    "TopSpamRecipient": [{"C1": "ann@x.com", "C2": 41}, {"C1": "bob@x.com", "C2": "7"}],
    "TopPhishRecipient": [{"C1": "ann@x.com", "C2": 3}],
}


def _when(text) -> datetime | None:
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00")[:19]).replace(tzinfo=UTC)
    except ValueError:
        return None


def message_trace(expanded: list[dict], flow: list[dict]):
    def answer(params: dict) -> list[dict]:
        if params.get("Status") == ["Expanded"]:
            return expanded
        start, end = _when(params.get("StartDate")), _when(params.get("EndDate"))
        return [r for r in flow if start <= _when(r["Received"]) <= end]

    return answer


def traffic_summary(top: dict):
    return lambda params: top.get(params.get("Category"), [])
