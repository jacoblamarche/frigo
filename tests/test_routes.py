from contextlib import closing

from fastapi.testclient import TestClient

from app import groceries
from app.db import connect
from app.main import create_app
from app.settings import Settings


def test_healthz_needs_no_identity(settings):
    with TestClient(create_app(settings)) as anonymous:
        assert anonymous.get("/healthz").json() == {"status": "ok"}


def test_identity_comes_from_x_email(client):
    page = client.get("/epicerie")
    assert page.status_code == 200
    assert 'title="Jacob"' in page.text


def test_unknown_or_missing_email_is_refused(client):
    assert client.get("/epicerie", headers={"X-Email": "inconnu@example.com"}).status_code == 403
    assert client.get("/epicerie", headers={"X-Email": ""}).status_code == 403


def test_x_email_from_an_untrusted_address_is_refused(settings):
    app = create_app(settings)
    with TestClient(app, client=("192.168.0.50", 50000)) as lan:
        response = lan.get("/epicerie", headers={"X-Email": "jacob@example.com"})
    assert response.status_code == 403


def test_dev_user_skips_authentication(tmp_path):
    with TestClient(create_app(Settings(data_dir=tmp_path, dev_user="Dev"))) as dev:
        assert 'title="Dev"' in dev.get("/epicerie").text


def test_post_from_another_site_is_refused(client):
    response = client.post(
        "/epicerie/ajouter", data={"texte": "lait"}, headers={"Origin": "https://evil.example"}
    )
    assert response.status_code == 403


def test_add_redirects_back_to_the_current_filter(client):
    response = client.post(
        "/epicerie/ajouter",
        data={"texte": "3 oignons\npâtes à lasagne", "etiquette": "Lasagne", "filtre": "Lasagne"},
        headers={"Origin": "http://testserver"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/epicerie?etiquette=Lasagne"

    page = client.get("/epicerie?etiquette=Lasagne").text
    assert "Oignons" in page and "Pâtes à lasagne" in page
    assert "2 à acheter" in page


def test_async_requests_get_no_redirect(client):
    response = client.post(
        "/epicerie/ajouter", data={"texte": "lait"}, headers={"X-Frigo-Async": "1"}
    )
    assert response.status_code == 204


def test_fragment_answers_304_until_something_changes(client):
    first = client.get("/epicerie/liste")
    etag = first.headers["etag"]
    assert first.headers["cache-control"] == "no-store"
    assert client.get("/epicerie/liste", headers={"If-None-Match": etag}).status_code == 304

    client.post("/epicerie/ajouter", data={"texte": "lait"})

    changed = client.get("/epicerie/liste", headers={"If-None-Match": etag})
    assert changed.status_code == 200
    assert "Lait" in changed.text


def test_check_clear_and_remove(client, settings):
    client.post("/epicerie/ajouter", data={"texte": "lait\nœufs\ncafé"})
    with closing(connect(settings.data_dir / "frigo.sqlite3")) as con:
        ids = {item.name: item.id for item in groceries.list_items(con)}

    client.post(f"/epicerie/{ids['Lait']}/cocher")
    client.post(f"/epicerie/{ids['Café']}/supprimer")
    page = client.get("/epicerie").text
    assert "Achetés · 1" in page and "Café" not in page

    client.post("/epicerie/vider")
    page = client.get("/epicerie").text
    assert "Achetés" not in page and "Lait" not in page and "Œufs" in page


def test_root_redirects_to_groceries(client):
    response = client.get("/", follow_redirects=False)
    assert response.headers["location"] == "/epicerie"
