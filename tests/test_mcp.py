import json
from contextlib import closing

import pytest
from fastapi.testclient import TestClient

from app import groceries
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
