"""Passwords people may not choose: the commonly used ones, and ones built from their own username.

Wrong guesses online are slowed down (``throttle.py``), but a copy of the database (a lost backup, say)
can be guessed against offline, and the passwords people reuse are the first thing tried. ``problem``
refuses a password that is on the list of commonly used passwords (``common_passwords.txt``: the 20,000
most used passwords of 12 characters or more, from the public "xato-net" list of 10 million leaked
passwords; shorter ones already fail the length rule), or that contains the person's username or the
product's name. The list is read once, on first use.
"""

from __future__ import annotations

from functools import cache
from pathlib import Path

MIN_LENGTH = 12
LIST_PATH = Path(__file__).parent / "common_passwords.txt"
PRODUCT_WORDS = ("officesentry", "office sentry")
COMMON = (
    "That password is on lists of commonly used passwords, so it would be guessed. "
    "Choose something that isn't a word, a name or a pattern."
)
CONTAINS_NAME = "Choose a password that doesn't contain your username."
CONTAINS_PRODUCT = "Choose a password that doesn't contain the product's name."


@cache
def common_passwords() -> frozenset[str]:
    return frozenset(
        line.strip().lower()
        for line in LIST_PATH.read_text(encoding="utf-8", errors="ignore").splitlines()
        if line.strip()
    )


def problem(password: str, username: str | None = None) -> str | None:
    """Why this password may not be used, or None. Length is checked by the caller (``auth.password_problem``)."""
    lowered = password.lower()
    if lowered in common_passwords() or lowered.strip() in common_passwords():
        return COMMON
    name = (username or "").strip().lower()
    if name:
        # The whole username, and the part before an @ for an email-style one ("ann" in "ann@harbour.example").
        parts = {name, name.split("@", 1)[0]}
        if any(len(p) >= 4 and p in lowered for p in parts):
            return CONTAINS_NAME
    if any(word in lowered for word in PRODUCT_WORDS):
        return CONTAINS_PRODUCT
    return None
