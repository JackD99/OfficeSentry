from officesentry import store
from tests.conftest import login, make_user


def test_admin_pages_render_with_data(client, db, two_tenants):
    alpha, _ = two_tenants
    uid = make_user(db, "admin")
    run_id = store.enqueue_run(db, alpha, "users")
    store.claim_next_run(db)
    store.add_objects(db, run_id, "user", [("u1", {"displayName": "<script>x</script>", "licensed": True})])
    store.add_metric(db, run_id, alpha, "users.total", 1)
    store.log_event(db, run_id, "info", "hello")
    store.finish_run(db, run_id, "partial", "Sign-in activity needs Entra ID P1")
    store.set_capabilities(db, alpha, {"entra_p1": True, "intune": False})
    login(client, "admin")
    for url in ["/", f"/tenants/{alpha}", f"/tenants/{alpha}/collection", f"/runs/{run_id}",
                f"/runs/{run_id}?kind=user", "/tenants/new",
                "/admin/users", f"/admin/users/{uid}", "/admin/connections", "/account/password"]:
        resp = client.get(url)
        assert resp.status_code == 200, url
    page = client.get(f"/runs/{run_id}?kind=user").get_data(as_text=True)
    assert "<script>x</script>" not in page and "&lt;script&gt;" in page


def test_friendly_times_and_labels():
    from datetime import UTC, datetime

    from officesentry.web.filters import ago, capability, money

    now = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
    assert ago("2026-10-02T11:59:30Z", now) == "just now"
    assert ago("2026-10-02T09:00:00Z", now) == "3 hours ago"
    assert ago("2026-10-01T09:00:00Z", now) == "yesterday"
    assert ago("2026-09-02T12:00:00Z", now) == "30 days ago"
    assert ago(None, now) == ""
    assert capability("entra_p1") == "Entra ID P1"
    assert money(1234.5, "GBP") == "£1,234"


def test_no_platform_specific_date_formats():
    """'%-d' crashes strftime on Windows; dates go through filters._strftime instead."""
    import pathlib
    import re
    from datetime import datetime

    from officesentry.web.filters import _strftime

    assert _strftime(datetime(2026, 10, 2, 9, 5), "%-d %b %Y, %H:%M") == "2 Oct 2026, 09:05"
    root = pathlib.Path(__file__).parent.parent / "officesentry"
    for path in [*root.rglob("*.py"), *root.rglob("*.html")]:
        if path.name == "filters.py":
            continue
        assert not re.search(r"strftime\([^)]*%-", path.read_text(encoding="utf-8")), path


def test_refresh_on_unconnected_tenant_explains_once(client, db, two_tenants):
    alpha, _ = two_tenants
    db.execute("UPDATE tenants SET consented_at = NULL")  # an app connection but no admin consent yet
    make_user(db, "admin")
    login(client, "admin")
    page = client.get(f"/tenants/{alpha}/collection").get_data(as_text=True)
    assert "isn&#39;t connected yet" in page and "disabled" in page
    resp = client.post(f"/tenants/{alpha}/collect", follow_redirects=True)
    assert resp.get_data(as_text=True).count("isn&#39;t connected yet") >= 1
    assert db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
    db.execute("UPDATE tenants SET consented_at = '2026-10-01T00:00:00Z' WHERE id = ?", (alpha,))
    client.post(f"/tenants/{alpha}/collect")
    assert db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] > 0
