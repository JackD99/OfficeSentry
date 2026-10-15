"""Forms answer in the portal's own words: risky actions ask in one shared dialog (app.js, data-confirm), a
pressed button shows it's working, and a message after an action carries an icon as well as its colour."""

from __future__ import annotations

import re
from pathlib import Path

from tests.conftest import login, make_user

WEB = Path(__file__).resolve().parents[1] / "officesentry" / "web"


def test_no_page_asks_with_the_browsers_own_box():
    """The shared dialog replaced a copy of the same confirm() script on eight pages (and two pages that
    carried data-confirm without the script, so never asked)."""
    found = []
    for path in [*WEB.glob("templates/**/*.html"), *WEB.glob("static/*.js")]:
        text = path.read_text(encoding="utf-8")
        if path.name != "app.js" and re.search(r"\bconfirm\(", text):
            found.append(path.name)
    assert not found
    app = (WEB / "static" / "app.js").read_text(encoding="utf-8")
    assert app.count("window.confirm(") == 1  # only where a browser has no <dialog>


def test_risky_forms_ask_and_say_how_risky(client, db):
    make_user(db, "admin")
    login(client, "admin")
    resp = client.post("/admin/groups", data={"name": "Gold"})
    page = client.get(resp.headers["Location"]).get_data(as_text=True)
    form = re.search(r'<form method="post" action="/admin/groups/\d+/delete"[^>]*>', page).group(0)
    assert 'data-confirm="Delete the group Gold? Its clients are kept."' in form
    assert "confirm(" not in page


def test_a_message_after_an_action_has_an_icon(client, db):
    make_user(db, "admin")
    login(client, "admin")
    page = client.post("/admin/groups", data={"name": "Gold"}, follow_redirects=True).get_data(as_text=True)
    assert re.search(
        r'<div class="flash success" role="status"><svg class="icon [^"]*"[^>]*>.*?</svg><span>Created', page
    )
    page = client.post("/admin/groups", data={"name": "gold"}, follow_redirects=True).get_data(as_text=True)
    assert re.search(
        r'<div class="flash error" role="status"><svg class="icon [^"]*"[^>]*><path d="M12 3l9.5 17h-19z"/>', page
    )


def test_a_download_form_is_left_out_of_busy_buttons(client, db):
    make_user(db, "admin")
    login(client, "admin")
    page = client.get("/admin/support").get_data(as_text=True)
    assert re.search(r'<form method="post" action="/admin/support/package"[^>]*data-no-busy', page)
