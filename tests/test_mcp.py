import json
from contextlib import closing

import pytest
from fastapi.testclient import TestClient

from app import groceries, recipes, tasks
from app.db import connect
from app.main import create_app
from app.settings import Settings
from tests.conftest import PEOPLE

TOKEN = "jeton-de-test"


@pytest.fixture
def mcp(tmp_path):
    settings = Settings(data_dir=tmp_path, mcp_token=TOKEN, dev_user="Jacob", people=PEOPLE)
    with TestClient(create_app(settings)) as client:
        yield client


def rpc(client, method, params=None, token=TOKEN):
    response = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json, text/event-stream",
        },
    )
    return response


def call(client, tool, **arguments):
    response = rpc(client, "tools/call", {"name": tool, "arguments": arguments})
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    assert not result.get("isError"), result
    structured = result["structuredContent"]
    return structured.get("result", structured)


def test_mcp_requires_the_token(mcp):
    assert rpc(mcp, "tools/list", token="mauvais").status_code == 401
    assert mcp.post("/mcp", json={}).status_code == 401


def test_mcp_is_off_without_a_token(tmp_path):
    with TestClient(create_app(Settings(data_dir=tmp_path, dev_user="Jacob"))) as client:
        assert rpc(client, "tools/list", token="").status_code == 401


def test_tools_are_listed(mcp):
    tools = {tool["name"] for tool in rpc(mcp, "tools/list").json()["result"]["tools"]}
    assert tools == {
        "list_groceries",
        "add_groceries",
        "update_grocery",
        "mark_bought",
        "remove_groceries",
        "clear_bought",
        "list_tasks",
        "add_tasks",
        "update_task",
        "remove_tasks",
    }


def test_recipe_ingredients_land_with_the_recipe_tag(mcp):
    added = call(
        mcp,
        "add_groceries",
        items=[
            {"name": "oignons", "quantity": "2"},
            {"name": "bœuf haché", "quantity": "1 lb", "tags": ["souper"]},
        ],
        tag="lasagne",
    )["added"]
    assert [(a["name"], a["quantity"], a["tags"], a["merged"]) for a in added] == [
        ("Oignons", "2", ["Lasagne"], False),
        ("Bœuf haché", "1 lb", ["Lasagne", "Souper"], False),
    ]
    assert {a["added_by"] for a in added} == {"Claude"}

    result = call(mcp, "add_groceries", items=[{"name": "Oignons", "quantity": "1"}], tag="soupe")
    again = result["added"]
    assert (again[0]["quantity"], again[0]["tags"], again[0]["merged"]) == (
        "2 + 1",
        ["Lasagne", "Soupe"],
        True,
    )


def test_list_mark_remove_and_clear(mcp):
    result = call(mcp, "add_groceries", items=[{"name": n} for n in ("lait", "œufs", "café")])
    lait, oeufs, cafe = (a["id"] for a in result["added"])

    assert call(mcp, "mark_bought", ids=[lait, 999]) == {"done": [lait], "skipped": [999]}
    assert call(mcp, "remove_groceries", ids=[cafe]) == {"done": [cafe], "skipped": []}

    listing = call(mcp, "list_groceries", include_bought=True)
    assert [g["id"] for g in listing["to_buy"]] == [oeufs]
    assert [g["id"] for g in listing["bought"]] == [lait]

    assert call(mcp, "clear_bought") == 1
    assert call(mcp, "list_groceries", include_bought=True)["bought"] == []


def test_update_errors_come_back_as_tool_errors(mcp):
    [oignons, echalotes] = call(
        mcp, "add_groceries", items=[{"name": "oignons"}, {"name": "échalotes"}]
    )["added"]
    response = rpc(
        mcp,
        "tools/call",
        {"name": "update_grocery", "arguments": {"id": oignons["id"], "name": "Échalotes"}},
    )
    result = response.json()["result"]
    assert result["isError"]
    assert "déjà sur la liste" in json.dumps(result, ensure_ascii=False)

    updated = call(mcp, "update_grocery", id=echalotes["id"], quantity="3")
    assert updated["quantity"] == "3"


def test_items_added_by_claude_show_a_badge(mcp):
    call(mcp, "add_groceries", items=[{"name": "lait"}])
    page = mcp.get("/epicerie").text
    assert 'class="by"' in page and "Ajouté par Claude" in page


def test_staples_are_left_out_unless_asked_for(mcp, tmp_path):
    with closing(connect(tmp_path / "frigo.sqlite3")) as con:
        groceries.add_staple(con, "Farine", "Jacob")

    result = call(mcp, "add_groceries", items=[{"name": "farine"}, {"name": "sucre"}], tag="Gâteau")
    assert [a["name"] for a in result["added"]] == ["Sucre"]
    assert result["skipped_staples"] == ["Farine"]
    assert call(mcp, "list_groceries")["staples"] == ["Farine"]

    forced = call(mcp, "add_groceries", items=[{"name": "Farine"}], include_staples=True)
    assert [a["name"] for a in forced["added"]] == ["Farine"]
    assert forced["skipped_staples"] == []


def test_claude_files_items_in_aisles(mcp):
    assert call(mcp, "list_groceries")["aisles"][:2] == ["Fruits et légumes", "Boulangerie"]

    added = call(
        mcp,
        "add_groceries",
        items=[{"name": "gruyère", "aisle": "fromages"}, {"name": "piles"}],
    )["added"]
    assert [a["aisle"] for a in added] == ["Fromages", "Autre"]

    moved = call(mcp, "update_grocery", id=added[0]["id"], aisle="Produits laitiers et œufs")
    assert moved["aisle"] == "Produits laitiers et œufs"


def test_an_unknown_aisle_adds_nothing(mcp):
    response = rpc(
        mcp,
        "tools/call",
        {
            "name": "add_groceries",
            "arguments": {"items": [{"name": "lait"}, {"name": "gruyère", "aisle": "Crèmerie"}]},
        },
    )
    result = response.json()["result"]
    assert result["isError"]
    assert "Rayon inconnu" in json.dumps(result, ensure_ascii=False)
    assert call(mcp, "list_groceries")["to_buy"] == []


def test_a_recipe_card_is_kept_with_every_ingredient(mcp, tmp_path):
    with closing(connect(tmp_path / "frigo.sqlite3")) as con:
        groceries.add_staple(con, "Farine", "Jacob")

    result = call(
        mcp,
        "add_groceries",
        items=[{"name": "oignons", "quantity": "2"}, {"name": "farine", "quantity": "1 tasse"}],
        tag="Pain aux oignons",
        recipe={
            "url": "https://example.com/pain",
            "servings": "1 pain",
            "ingredients": [
                {"name": "oignons émincés", "quantity": "2"},
                {"name": "farine tout usage", "quantity": "1 tasse"},
            ],
            "steps": ["Pétrir.", "Cuire 40 min."],
        },
    )
    assert result["recipe"] == "Pain aux oignons"
    assert result["skipped_staples"] == ["Farine"]

    with closing(connect(tmp_path / "frigo.sqlite3")) as con:
        card = recipes.by_name(con, "pain aux oignons")
    assert [i.name for i in card.ingredients] == ["oignons émincés", "farine tout usage"]
    assert (card.url, card.servings, card.steps, card.added_by) == (
        "https://example.com/pain",
        "1 pain",
        ["Pétrir.", "Cuire 40 min."],
        "Claude",
    )


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"recipe": {"ingredients": [], "steps": []}}, "`tag` est obligatoire"),
        (
            {"tag": "Pain", "recipe": {"url": "javascript:x", "ingredients": [], "steps": []}},
            "Lien invalide",
        ),
    ],
)
def test_a_bad_recipe_adds_nothing(mcp, arguments, message):
    response = rpc(
        mcp,
        "tools/call",
        {"name": "add_groceries", "arguments": {"items": [{"name": "lait"}], **arguments}},
    )
    result = response.json()["result"]
    assert result["isError"]
    assert message in json.dumps(result, ensure_ascii=False)
    assert call(mcp, "list_groceries")["to_buy"] == []


def tool_error(client, tool, **arguments):
    result = rpc(client, "tools/call", {"name": tool, "arguments": arguments}).json()["result"]
    assert result["isError"]
    return json.dumps(result, ensure_ascii=False)


def test_claude_adds_tasks_to_nobody_by_default(mcp):
    board = call(mcp, "list_tasks")
    assert board["tasks"] == []
    assert board["people"] == ["Jacob", "Catherine"]
    assert board["today"] == tasks.today().isoformat()

    added = call(
        mcp,
        "add_tasks",
        items=[
            {"title": "changer le filtre de la fournaise"},
            {
                "title": "Réserver le resto",
                "assignee": "catherine",
                "priority": "high",
                "due": "2026-12-05",
                "note": "Fête de maman",
            },
        ],
    )
    assert [(t["title"], t["assignee"], t["priority"], t["due"], t["added_by"]) for t in added] == [
        ("Changer le filtre de la fournaise", "", "normal", "", "Claude"),
        ("Réserver le resto", "Catherine", "high", "2026-12-05", "Claude"),
    ]
    assert [t["title"] for t in call(mcp, "list_tasks")["tasks"]] == [
        "Réserver le resto",
        "Changer le filtre de la fournaise",
    ]
    assert [t["title"] for t in call(mcp, "list_tasks", assignee="-")["tasks"]] == [
        "Changer le filtre de la fournaise"
    ]
    assert [t["title"] for t in call(mcp, "list_tasks", assignee="CATHERINE")["tasks"]] == [
        "Réserver le resto"
    ]
    assert "Personne inconnue" in tool_error(mcp, "list_tasks", assignee="Paul")
    page = mcp.get("/taches").text
    assert "Ajoutée par Claude" in page


def test_a_bad_task_adds_nothing(mcp):
    message = tool_error(
        mcp, "add_tasks", items=[{"title": "Tondre"}, {"title": "Peindre", "assignee": "Paul"}]
    )
    assert "Personne inconnue : « Paul ». Choix : Jacob, Catherine." in message
    assert "AAAA-MM-JJ" in tool_error(mcp, "add_tasks", items=[{"title": "x", "due": "vendredi"}])
    assert call(mcp, "list_tasks")["tasks"] == []


def test_update_task_changes_only_what_is_given(mcp):
    [task] = call(
        mcp,
        "add_tasks",
        items=[
            {
                "title": "Appeler le plombier",
                "assignee": "Jacob",
                "due": "2026-12-01",
                "note": "418 555-0123",
            }
        ],
    )
    moved = call(mcp, "update_task", id=task["id"], status="doing", priority="low")
    assert (moved["status"], moved["priority"], moved["assignee"], moved["due"], moved["note"]) == (
        "doing",
        "low",
        "Jacob",
        "2026-12-01",
        "418 555-0123",
    )

    cleared = call(mcp, "update_task", id=task["id"], assignee="", due="", status="done")
    assert (cleared["assignee"], cleared["due"]) == ("", "")
    assert cleared["done_on"] == tasks.today().isoformat()
    assert call(mcp, "list_tasks")["tasks"] == []
    assert len(call(mcp, "list_tasks", include_done=True)["tasks"]) == 1
    assert len(call(mcp, "list_tasks", status="done")["tasks"]) == 1

    assert "Aucune tâche 999" in tool_error(mcp, "update_task", id=999, status="done")
    assert "Personne inconnue" in tool_error(mcp, "update_task", id=task["id"], assignee="Paul")


def test_remove_tasks(mcp):
    [task] = call(mcp, "add_tasks", items=[{"title": "Tondre"}])
    removed = call(mcp, "remove_tasks", ids=[task["id"], 999])
    assert removed == {"done": [task["id"]], "skipped": [999]}
