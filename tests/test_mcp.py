import json
from contextlib import closing

import pytest
from fastapi.testclient import TestClient

from app import groceries, recipes
from app.db import connect
from app.main import create_app
from app.settings import Settings

TOKEN = "jeton-de-test"


@pytest.fixture
def mcp(tmp_path):
    settings = Settings(data_dir=tmp_path, mcp_token=TOKEN, dev_user="Jacob")
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
