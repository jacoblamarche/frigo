import pytest

from app import recipes
from app.recipes import Ingredient


def save(con, name="lasagne", **fields):
    return recipes.save(
        con,
        name,
        **{
            "url": "https://www.ricardocuisine.com/recettes/lasagne",
            "ingredients": [Ingredient("2", "oignons émincés"), Ingredient("", " ")],
            "steps": ["Dorer le bœuf.", "  "],
            "added_by": "Claude",
            **fields,
        },
    )


def test_a_recipe_is_saved_under_its_tag_name(con):
    recipe = save(con, servings="6 portions", time="1 h 30")
    assert recipe.name == "Lasagne"
    assert recipe.site == "ricardocuisine.com"
    assert recipe.ingredients == [Ingredient("2", "oignons émincés")]
    assert recipe.steps == ["Dorer le bœuf."]
    assert recipes.by_name(con, "LASAGNE").id == recipe.id


def test_importing_again_replaces_the_card_and_moves_it_first(con):
    first = save(con)
    save(con, "soupe")
    again = save(con, "Lasagne", url="", steps=["Tout mélanger."])
    assert again.id == first.id
    assert (again.url, again.site, again.steps) == ("", "", ["Tout mélanger."])
    assert [r.name for r in recipes.list_recipes(con)] == ["Lasagne", "Soupe"]


@pytest.mark.parametrize("url", ["javascript:alert(1)", "ftp://example.com/x", "https://"])
def test_only_web_links_are_accepted(con, url):
    with pytest.raises(ValueError, match="Lien invalide"):
        save(con, url=url)
    assert recipes.list_recipes(con) == []


def test_recipes_for_tags_and_delete(con):
    lasagne = save(con)
    assert [r.name for r in recipes.for_tags(con, ["Souper", "Lasagne"])] == ["Lasagne"]
    assert recipes.delete(con, lasagne.id)
    assert not recipes.delete(con, lasagne.id)
    assert recipes.get(con, lasagne.id) is None
