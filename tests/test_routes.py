import re
from contextlib import closing
from datetime import date

from fastapi.testclient import TestClient

from app import groceries, purchases, recipes, tasks
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


def test_staples_page_adds_and_removes(client):
    response = client.post(
        "/epicerie/stock/ajouter", data={"nom": "farine"}, follow_redirects=False
    )
    assert response.headers["location"] == "/epicerie/stock"
    assert "Farine" in client.get("/epicerie/stock").text

    client.post("/epicerie/stock/retirer", data={"nom": "Farine"})
    assert "Aucun produit" in client.get("/epicerie/stock").text


def test_people_can_still_add_a_staple_themselves(client):
    client.post("/epicerie/stock/ajouter", data={"nom": "farine"})
    client.post("/epicerie/ajouter", data={"texte": "farine"})
    assert 'aria-label="Cocher Farine"' in client.get("/epicerie").text


def item_id(settings, name):
    with closing(connect(settings.data_dir / "frigo.sqlite3")) as con:
        return next(item.id for item in groceries.list_items(con) if item.name == name)


def aisle_id(settings, name):
    with closing(connect(settings.data_dir / "frigo.sqlite3")) as con:
        return groceries.find_aisle(con, name)


def test_the_list_is_shown_by_aisle(client, settings):
    client.post("/epicerie/ajouter", data={"texte": "lait\npommes"})
    client.post(
        f"/epicerie/{item_id(settings, 'Pommes')}/modifier",
        data={"nom": "Pommes", "rayon": aisle_id(settings, "Fruits et légumes")},
    )

    page = client.get("/epicerie").text
    assert page.index("Fruits et légumes · 1") < page.index("Autre · 1")
    assert page.index("Pommes") < page.index("Lait")


def test_the_item_sheet_edits_everything(client, settings):
    client.post("/epicerie/ajouter", data={"texte": "oignons 2 #lasagne"})
    oignons = item_id(settings, "Oignons")

    sheet = client.get(f"/epicerie/{oignons}?filtre=Lasagne").text
    assert 'value="Oignons"' in sheet and 'value="Lasagne"' in sheet
    assert '<a href="/epicerie?etiquette=Lasagne">' in sheet

    response = client.post(
        f"/epicerie/{oignons}/modifier",
        data={
            "nom": "oignons jaunes",
            "quantite": "3",
            "etiquettes": "Lasagne, soupe, ",
            "rayon": aisle_id(settings, "Fruits et légumes"),
            "filtre": "Lasagne",
        },
        follow_redirects=False,
    )
    assert response.headers["location"] == "/epicerie?etiquette=Lasagne"

    with closing(connect(settings.data_dir / "frigo.sqlite3")) as con:
        item = groceries.get_item(con, oignons)
    assert (item.name, item.quantity, item.tags, item.aisle) == (
        "Oignons jaunes",
        "3",
        ["Lasagne", "Soupe"],
        "Fruits et légumes",
    )


def test_saving_the_sheet_without_touching_the_aisle_remembers_nothing(client, settings):
    client.post("/epicerie/ajouter", data={"texte": "piles"})
    piles = item_id(settings, "Piles")
    client.post(
        f"/epicerie/{piles}/modifier", data={"nom": "Piles", "rayon": aisle_id(settings, "Autre")}
    )

    with closing(connect(settings.data_dir / "frigo.sqlite3")) as con:
        groceries.remove(con, piles)
        [added] = groceries.add_items(
            con,
            [
                groceries.ParsedItem(
                    "Piles", aisle_id=groceries.find_aisle(con, "Hygiène et maison")
                )
            ],
            "Claude",
        )
        assert groceries.get_item(con, added.id).aisle == "Hygiène et maison"


def test_the_sheet_refuses_a_name_already_on_the_list(client, settings):
    client.post("/epicerie/ajouter", data={"texte": "oignons\néchalotes"})
    oignons = item_id(settings, "Oignons")

    response = client.post(
        f"/epicerie/{oignons}/modifier", data={"nom": "Échalotes", "quantite": "4"}
    )
    assert response.status_code == 409
    assert "déjà sur la liste" in response.text
    assert 'value="Échalotes"' in response.text and 'value="4"' in response.text


def test_the_sheet_of_a_removed_item_goes_back_to_the_list(client):
    response = client.get("/epicerie/999", follow_redirects=False)
    assert response.headers["location"] == "/epicerie"


def test_static_files_are_linked_with_a_content_version(client):
    page = client.get("/epicerie").text
    href = re.search(r'href="(/static/app\.css\?v=[0-9a-f]{10})"', page)[1]
    assert client.get(href).status_code == 200


def save_lasagne(settings, url="https://www.ricardocuisine.com/recettes/lasagne"):
    with closing(connect(settings.data_dir / "frigo.sqlite3")) as con:
        return recipes.save(
            con,
            "Lasagne",
            url=url,
            servings="6 portions",
            ingredients=[recipes.Ingredient("2", "oignons émincés")],
            steps=["Dorer le bœuf.", "Monter la lasagne."],
            added_by="Claude",
        ).id


def test_recipes_tab_lists_the_cards(client, settings):
    assert "Aucune recette" in client.get("/recettes").text
    recipe_id = save_lasagne(settings)
    page = client.get("/recettes").text
    assert f'href="/recettes/{recipe_id}"' in page
    assert "6 portions · ricardocuisine.com" in page
    assert 'href="/recettes" class="on"' in page


def test_the_recipe_card_shows_ingredients_steps_and_source(client, settings):
    page = client.get(f"/recettes/{save_lasagne(settings)}").text
    assert '<li><span class="q">2</span> oignons émincés</li>' in page
    assert "<li>Monter la lasagne.</li>" in page
    assert 'href="https://www.ricardocuisine.com/recettes/lasagne" target="_blank"' in page
    assert "Recette originale · ricardocuisine.com" in page


def test_a_missing_recipe_goes_back_to_the_list(client):
    response = client.get("/recettes/999", follow_redirects=False)
    assert response.headers["location"] == "/recettes"


def test_the_grocery_filter_and_the_item_sheet_link_to_the_recipe(client, settings):
    recipe_id = save_lasagne(settings)
    with closing(connect(settings.data_dir / "frigo.sqlite3")) as con:
        [added] = groceries.add_items(con, groceries.parse_text("oignons", "lasagne"), "Jacob")
    link = f'href="/recettes/{recipe_id}"'
    assert link in client.get("/epicerie?etiquette=Lasagne").text
    assert link not in client.get("/epicerie").text
    assert link in client.get(f"/epicerie/{added.id}").text


def test_deleting_a_recipe_keeps_the_groceries(client, settings):
    recipe_id = save_lasagne(settings)
    with closing(connect(settings.data_dir / "frigo.sqlite3")) as con:
        groceries.add_items(con, groceries.parse_text("oignons", "lasagne"), "Jacob")
    response = client.post(
        f"/recettes/{recipe_id}/supprimer",
        headers={"Origin": "http://testserver"},
        follow_redirects=False,
    )
    assert response.headers["location"] == "/recettes"
    assert "Aucune recette" in client.get("/recettes").text
    assert "Oignons" in client.get("/epicerie?etiquette=Lasagne").text


def task_ids(settings):
    with closing(connect(settings.data_dir / "frigo.sqlite3")) as con:
        return {t.title: t.id for t in tasks.list_tasks(con)}


def new_task(client, titre, **fields):
    return client.post("/taches/ajouter", data={"titre": titre, **fields}, follow_redirects=False)


def test_tasks_tab_starts_empty(client):
    page = client.get("/taches").text
    assert 'href="/taches" class="on"' in page
    assert "Rien à faire." in page
    assert 'href="/taches/nouvelle?colonne=a-faire&amp;pour="' in page


def test_a_new_task_defaults_to_me_and_the_current_column(client):
    sheet = client.get("/taches/nouvelle?colonne=en-cours").text
    assert 'value="Jacob" checked' in sheet
    assert '<option value="en-cours" selected>' in sheet
    assert 'value="1" checked' in sheet


def test_adding_a_task_goes_to_its_column(client, settings):
    response = new_task(
        client,
        "appeler le plombier",
        priorite="2",
        personne="Catherine",
        etat="en-cours",
        echeance="2026-12-05",
        note="Fuite sous l'évier",
        pour="moi",
    )
    assert response.headers["location"] == "/taches?colonne=en-cours&pour=moi"

    page = client.get("/taches?colonne=en-cours").text
    assert "Appeler le plombier" in page
    assert '<span class="pill p-haute">Haute</span>' in page
    assert 'class="who who-1" title="Catherine">C<' in page
    assert "sam. 5 déc." in page
    assert "Fuite" not in page
    assert "Fait →" in page

    sheet = client.get(f"/taches/{task_ids(settings)['Appeler le plombier']}").text
    assert "Fuite sous l&#39;évier</textarea>" in sheet
    assert 'value="2026-12-05"' in sheet
    assert "Ajoutée par Jacob" in sheet


def test_the_sheet_shows_errors_without_losing_the_draft(client):
    response = new_task(client, "Tondre", personne="Paul", note="Avant la pluie")
    assert response.status_code == 422
    assert "Personne inconnue" in response.text
    assert 'value="Tondre"' in response.text and "Avant la pluie</textarea>" in response.text


def test_column_counts_and_the_person_filter(client, settings):
    new_task(client, "Pour moi", personne="Jacob")
    new_task(client, "Pour Catherine", personne="Catherine")
    new_task(client, "Pour personne", etat="en-cours")

    page = client.get("/taches").text
    assert re.search(r'À faire <span class="n">2</span>', page)
    assert re.search(r'En cours <span class="n">1</span>', page)
    chips = re.findall(r'<a class="chip[^"]*"[^>]*>([^<]+)<', page)
    assert chips == ["Tout le monde", "Moi", "Catherine", "Personne"]

    mine = client.get("/taches?pour=moi").text
    assert "Pour moi" in mine and "Pour Catherine" not in mine
    assert re.search(r'En cours <span class="n">0</span>', mine)
    assert "Pour Catherine" in client.get("/taches?pour=catherine").text
    nobody = client.get("/taches?colonne=en-cours&pour=personne").text
    assert "Pour personne" in nobody

    catherine = client.get("/taches", headers={"X-Email": "catherine@example.com"}).text
    chips = re.findall(r'<a class="chip[^"]*"[^>]*>([^<]+)<', catherine)
    assert chips == ["Tout le monde", "Moi", "Jacob", "Personne"]


def test_the_card_button_advances_once(client, settings):
    new_task(client, "Peindre la music room")
    task_id = task_ids(settings)["Peindre la music room"]
    for _ in range(2):
        response = client.post(
            f"/taches/{task_id}/avancer", data={"de": "todo"}, headers={"X-Frigo-Async": "1"}
        )
        assert response.status_code == 204
    assert "Peindre la music room" in client.get("/taches?colonne=en-cours").text

    client.post(f"/taches/{task_id}/avancer", data={"de": "doing"})
    done = client.get("/taches?colonne=fait").text
    assert "Fait aujourd&#39;hui" in done and "→</button>" not in done


def test_editing_moves_back_and_returns_to_the_same_view(client, settings):
    new_task(client, "Payer Hydro", etat="fait")
    task_id = task_ids(settings)["Payer Hydro"]
    response = client.post(
        f"/taches/{task_id}/modifier",
        data={"titre": "Payer Hydro", "etat": "a-faire", "colonne": "fait", "pour": "personne"},
        follow_redirects=False,
    )
    assert response.headers["location"] == "/taches?colonne=fait&pour=personne"
    assert "Payer Hydro" in client.get("/taches").text


def test_overdue_tasks_are_flagged(client, settings):
    with closing(connect(settings.data_dir / "frigo.sqlite3")) as con:
        tasks.add(con, [tasks.Fields("Appeler", due=date(2020, 1, 6))], "Jacob", ["Jacob"])
    assert '<span class="late">lun. 6 janv. 2020 · en retard</span>' in client.get("/taches").text


def test_deleting_a_task(client, settings):
    new_task(client, "Trier le garde-robe")
    task_id = task_ids(settings)["Trier le garde-robe"]
    assert "data-confirm=" in client.get(f"/taches/{task_id}").text
    response = client.post(
        f"/taches/{task_id}/supprimer", data={"colonne": "a-faire"}, follow_redirects=False
    )
    assert response.headers["location"] == "/taches?colonne=a-faire"
    assert "Rien à faire." in client.get("/taches").text
    assert client.get(f"/taches/{task_id}", follow_redirects=False).status_code == 303


def test_the_task_fragment_answers_304_until_something_changes(client):
    etag = client.get("/taches/liste").headers["etag"]
    assert client.get("/taches/liste", headers={"If-None-Match": etag}).status_code == 304
    new_task(client, "Tondre")
    changed = client.get("/taches/liste", headers={"If-None-Match": etag})
    assert changed.status_code == 200 and "Tondre" in changed.text


def purchase_ids(settings):
    with closing(connect(settings.data_dir / "frigo.sqlite3")) as con:
        return {p.title: p.id for p in purchases.list_purchases(con)}


def new_purchase(client, titre, **fields):
    return client.post("/achats/ajouter", data={"titre": titre, **fields}, follow_redirects=False)


def test_purchases_tab_starts_empty(client):
    page = client.get("/achats").text
    assert 'href="/achats" class="on"' in page
    assert "Aucun gros achat en vue." in page
    assert page.count('<div class="amt">0\xa0$</div>') == 3
    assert 'href="/achats/nouveau?statut=&amp;etiquette="' in page


def test_adding_a_purchase_shows_its_card(client, settings):
    response = new_purchase(
        client,
        "système de son",
        etiquettes="music room, Music Room",
        priorite="2",
        etat="pret",
        budget="1 200 $",
        details="HS8 : https://www.steves.ca/hs8",
    )
    assert response.headers["location"] == "/achats"

    page = client.get("/achats").text
    assert "Système de son" in page
    assert '<span class="pill p-haute">Haute</span>' in page
    assert '<span class="tag">Music room</span>' in page
    assert "Prêt à acheter</span>" in page
    assert '<span class="budget">≈ 1\xa0200\xa0$</span>' in page
    assert "HS8" not in page
    assert re.search(r'total ready"><div class="lbl">Prêt à acheter</div>.*1\xa0200\xa0\$', page)

    sheet = client.get(f"/achats/{purchase_ids(settings)['Système de son']}").text
    assert 'value="Music room"' in sheet
    assert 'value="1\xa0200\xa0$"' in sheet
    assert 'value="pret" checked' in sheet
    assert '<a href="https://www.steves.ca/hs8" target="_blank"' in sheet
    assert ">steves.ca/hs8</a>" in sheet
    assert "Ajouté par Jacob" in sheet


def test_a_new_purchase_takes_the_current_filter(client):
    sheet = client.get("/achats/nouveau?statut=a-l-etude&etiquette=garage").text
    assert 'value="a-l-etude" checked' in sheet
    assert 'name="etiquettes" value="garage"' in sheet
    assert 'value="1" checked' in sheet
    assert 'value="idee" checked' in client.get("/achats/nouveau").text


def test_adding_goes_where_the_new_purchase_shows(client):
    response = new_purchase(client, "Divan", etat="idee", statut="pret", etiquette="salon")
    assert response.headers["location"] == "/achats?statut=idee"
    response = new_purchase(
        client, "Laveuse", etat="pret", etiquettes="Buanderie", statut="pret", etiquette="buanderie"
    )
    assert response.headers["location"] == "/achats?statut=pret&etiquette=buanderie"


def test_a_bad_amount_keeps_the_draft(client):
    response = new_purchase(client, "Divan", budget="beaucoup", paye="12", details="Cuir")
    assert response.status_code == 422
    assert "Montant invalide : « beaucoup »" in response.text
    assert 'value="Divan"' in response.text and "Cuir</textarea>" in response.text
    assert 'name="budget" value="beaucoup"' in response.text
    assert 'name="paye" value="12"' in response.text
    assert "Aucun gros achat en vue." in client.get("/achats").text


def test_status_and_tag_filters_combine(client):
    new_purchase(client, "Son", etiquettes="Music room", etat="pret", budget="1200")
    new_purchase(client, "Panneaux", etiquettes="Music room", etat="a-l-etude", budget="350")
    new_purchase(client, "Laveuse", etiquettes="Buanderie", etat="a-l-etude", budget="1100")
    new_purchase(client, "Aspirateur", etat="achete", paye="480")

    page = client.get("/achats").text
    assert re.search(r'En cours <span class="n">3</span>', page)
    assert re.search(r'Acheté <span class="n">1</span>', page)
    assert "Acheté récemment" in page and "Aspirateur" in page
    assert re.findall(r'class="chip[^"]*" href="/achats\?statut=&amp;etiquette=([^"]+)"', page) == [
        "Buanderie",
        "Music+room",
    ]

    studied = client.get("/achats?statut=a-l-etude").text
    assert "Panneaux" in studied and "Laveuse" in studied and "Son" not in studied
    assert "Acheté récemment" not in studied

    room = client.get("/achats?statut=a-l-etude&etiquette=music%20room").text
    assert "Panneaux" in room and "Laveuse" not in room
    assert re.search(r'En cours <span class="n">2</span>', room)
    assert (
        '<a class="chip on" href="/achats?statut=a-l-etude&amp;etiquette=">Music room</a>' in room
    )
    assert re.search(r"À l&#39;étude</div><div class=\"amt\">350\xa0\$", room)

    assert "Rien dans « Idée »." in client.get("/achats?statut=idee").text


def test_bought_cards_compare_the_price_to_the_budget(client):
    new_purchase(client, "Aspirateur robot", etat="achete", budget="550", paye="479,99")
    new_purchase(client, "Laveuse", etat="achete", budget="1 100")
    page = client.get("/achats?statut=achete").text
    assert (
        '<span class="paid">Payé <b>480\xa0$</b> · budget 550\xa0$ · 70\xa0$ sous le budget</span>'
        in page
    )
    assert '<span class="paid">Acheté · budget 1\xa0100\xa0$</span>' in page
    assert 'class="budget"' not in page


def test_editing_a_purchase_returns_to_the_same_view(client, settings):
    new_purchase(client, "Son", etat="pret", budget="1200")
    purchase_id = purchase_ids(settings)["Son"]
    response = client.post(
        f"/achats/{purchase_id}/modifier",
        data={"titre": "Son", "etat": "achete", "paye": "1150", "statut": "pret"},
        follow_redirects=False,
    )
    assert response.headers["location"] == "/achats?statut=pret"
    sheet = client.get(f"/achats/{purchase_id}").text
    assert "acheté aujourd&#39;hui" in sheet
    assert 'name="budget" value=""' in sheet


def test_deleting_a_purchase(client, settings):
    new_purchase(client, "Divan")
    purchase_id = purchase_ids(settings)["Divan"]
    assert "data-confirm=" in client.get(f"/achats/{purchase_id}").text
    response = client.post(
        f"/achats/{purchase_id}/supprimer", data={"statut": "idee"}, follow_redirects=False
    )
    assert response.headers["location"] == "/achats?statut=idee"
    assert "Aucun gros achat en vue." in client.get("/achats").text
    assert client.get(f"/achats/{purchase_id}", follow_redirects=False).status_code == 303


def test_the_purchase_fragment_answers_304_until_something_changes(client):
    etag = client.get("/achats/liste").headers["etag"]
    assert client.get("/achats/liste", headers={"If-None-Match": etag}).status_code == 304
    new_purchase(client, "Divan")
    changed = client.get("/achats/liste", headers={"If-None-Match": etag})
    assert changed.status_code == 200 and "Divan" in changed.text
