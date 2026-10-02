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
    for url in ["/", f"/tenants/{alpha}", f"/runs/{run_id}", f"/runs/{run_id}?kind=user", "/tenants/new",
                "/admin/users", f"/admin/users/{uid}", "/admin/connections", "/account/password"]:
        resp = client.get(url)
        assert resp.status_code == 200, url
    page = client.get(f"/runs/{run_id}?kind=user").get_data(as_text=True)
    assert "<script>x</script>" not in page and "&lt;script&gt;" in page
