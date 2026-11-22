import pytest

from app import groceries
from app.db import data_version
from app.groceries import ParsedItem


def add(con, *items, who="Jacob"):
    return [added.id for added in groceries.add_items(con, list(items), who)]


def names(items):
    return [(item.name, item.quantity, item.tags) for item in items]


def test_adding_an_item_already_on_the_list_merges_it(con):
    add(con, ParsedItem("Oignons", "2", ["Lasagne"]))
    add(con, ParsedItem("oignons", "1", ["Soupe à l'oignon"]))

    assert names(groceries.list_items(con)) == [
        ("Oignons", "2 + 1", ["Lasagne", "Soupe à l'oignon"])
    ]


def test_a_bought_item_added_again_is_a_new_line(con):
    [item_id] = add(con, ParsedItem("Lait", "2 L"))
    groceries.toggle(con, item_id)
    add(con, ParsedItem("Lait", "4 L"))

    assert names(groceries.list_items(con)) == [("Lait", "4 L", [])]
    assert names(groceries.list_items(con, checked=True)) == [("Lait", "2 L", [])]


def test_unchecking_merges_into_the_line_already_back_on_the_list(con):
    [bought] = add(con, ParsedItem("Lait", "2 L", ["Crêpes"]))
    groceries.toggle(con, bought)
    add(con, ParsedItem("Lait", "1 L"))

    groceries.toggle(con, bought)

    assert names(groceries.list_items(con)) == [("Lait", "1 L + 2 L", ["Crêpes"])]
    assert groceries.list_items(con, checked=True) == []


def test_add_reports_which_items_were_merged(con):
    added = groceries.add_items(
        con, [ParsedItem("Oignons", "2"), ParsedItem("Lait"), ParsedItem("oignons", "1")], "Jacob"
    )
    assert [a.merged for a in added] == [False, False, True]
    assert added[0].id == added[2].id


def test_update_changes_only_what_is_given(con):
    [item_id] = add(con, ParsedItem("Oignons", "2", ["Lasagne"]))

    item = groceries.update_item(con, item_id, quantity="3", tags=["soupe", " "])

    assert (item.name, item.quantity, item.tags) == ("Oignons", "3", ["Soupe"])
    assert groceries.update_item(con, item_id, name="oignons rouges").name == "Oignons rouges"


def test_update_refuses_a_name_already_on_the_list(con):
    oignons, _ = add(con, ParsedItem("Oignons"), ParsedItem("Échalotes"))

    with pytest.raises(ValueError, match="Échalotes"):
        groceries.update_item(con, oignons, name="echalotes")
    with pytest.raises(LookupError):
        groceries.update_item(con, 999, quantity="1")


def test_mark_bought_skips_unknown_and_already_bought(con):
    lait, oeufs = add(con, ParsedItem("Lait"), ParsedItem("Œufs"))
    groceries.toggle(con, oeufs)

    assert groceries.mark_bought(con, [lait, oeufs, 999]) == [lait]
    assert groceries.list_items(con) == []


def test_toggle_and_remove_report_unknown_items(con):
    assert groceries.toggle(con, 999) is False
    assert groceries.remove(con, 999) is False


def test_clear_checked_keeps_what_is_left_to_buy(con):
    lait, oeufs = add(con, ParsedItem("Lait"), ParsedItem("Œufs"))
    groceries.toggle(con, lait)

    assert groceries.clear_checked(con) == 1
    assert names(groceries.list_items(con)) == [("Œufs", "", [])]
    assert groceries.list_items(con, checked=True) == []


def test_filter_by_tag_or_untagged(con):
    add(
        con,
        ParsedItem("Oignons", tags=["Lasagne", "Soupe"]),
        ParsedItem("Gruyère", tags=["Soupe"]),
        ParsedItem("Lait"),
    )

    assert [i.name for i in groceries.list_items(con, "soupe")] == ["Oignons", "Gruyère"]
    assert [i.name for i in groceries.list_items(con, "-")] == ["Lait"]
    assert groceries.tag_counts(con) == [("Lasagne", 1), ("Soupe", 2)]


def test_tag_counts_ignore_bought_items(con):
    [item_id] = add(con, ParsedItem("Oignons", tags=["Lasagne"]))
    groceries.toggle(con, item_id)

    assert groceries.tag_counts(con) == []


def test_frequent_items_are_those_added_more_than_once_and_not_on_the_list(con):
    for _ in range(3):
        [item_id] = add(con, ParsedItem("Lait"))
        groceries.toggle(con, item_id)
    [pain] = add(con, ParsedItem("Pain"))
    groceries.toggle(con, pain)
    add(con, ParsedItem("Pain"))
    add(con, ParsedItem("Café"))

    assert groceries.frequent(con) == ["Lait"]


def test_every_change_bumps_the_data_version(con):
    start = data_version(con)
    [item_id] = add(con, ParsedItem("Lait"))
    groceries.toggle(con, item_id)
    groceries.clear_checked(con)

    assert data_version(con) == start + 3


def test_nothing_to_do_leaves_the_version_alone(con):
    start = data_version(con)
    add(con)
    groceries.clear_checked(con)

    assert data_version(con) == start


def test_staples_are_matched_on_the_normalized_name(con):
    assert groceries.add_staple(con, "farine", "Jacob")
    assert not groceries.add_staple(con, "FARINE ", "Catherine")
    assert not groceries.add_staple(con, "  ", "Jacob")
    groceries.add_staple(con, "Pâtes", "Jacob")

    kept, skipped = groceries.split_staples(
        con, [ParsedItem("Farine"), ParsedItem("Pates"), ParsedItem("Farine de riz")]
    )

    assert groceries.list_staples(con) == ["Farine", "Pâtes"]
    assert [i.name for i in skipped] == ["Farine", "Pates"]
    assert [i.name for i in kept] == ["Farine de riz"]
    assert groceries.remove_staple(con, "PATES")
    assert groceries.list_staples(con) == ["Farine"]


def aisle(con, name):
    return groceries.find_aisle(con, name)


def test_aisles_come_in_store_order_and_are_found_loosely(con):
    names = [a.name for a in groceries.list_aisles(con)]
    assert names[0] == "Fruits et légumes" and names[-1] == "Autre" and len(names) == 12
    assert aisle(con, "produits laitiers et oeufs") == aisle(con, "Produits laitiers et œufs")
    with pytest.raises(ValueError, match="Rayon inconnu"):
        aisle(con, "Rayon 12")


def test_an_unclassified_item_goes_to_other(con):
    add(con, ParsedItem("Lait"))
    assert groceries.list_items(con)[0].aisle == "Autre"


def test_the_suggested_aisle_is_remembered(con):
    [gruyere] = add(con, ParsedItem("Gruyère", aisle_id=aisle(con, "Fromages")))
    groceries.toggle(con, gruyere)
    groceries.clear_checked(con)

    add(con, ParsedItem("gruyère"))

    assert groceries.list_items(con)[0].aisle == "Fromages"


def test_a_remembered_aisle_wins_over_a_suggestion(con):
    [lait] = add(con, ParsedItem("Lait"))
    groceries.update_item(con, lait, aisle_id=aisle(con, "Boissons"))
    groceries.remove(con, lait)

    add(con, ParsedItem("Lait", aisle_id=aisle(con, "Produits laitiers et œufs")))

    assert groceries.list_items(con)[0].aisle == "Boissons"


def test_a_suggestion_classifies_an_unclassified_item_already_on_the_list(con):
    add(con, ParsedItem("Gruyère"))
    add(con, ParsedItem("Gruyère", "200 g", aisle_id=aisle(con, "Fromages")))

    assert [(i.quantity, i.aisle) for i in groceries.list_items(con)] == [("200 g", "Fromages")]


def test_reclassifying_is_remembered(con):
    [lait] = add(con, ParsedItem("Lait", aisle_id=aisle(con, "Produits laitiers et œufs")))
    assert groceries.update_item(con, lait, aisle_id=aisle(con, "Boissons")).aisle == "Boissons"
    groceries.remove(con, lait)

    add(con, ParsedItem("Lait"))

    assert groceries.list_items(con)[0].aisle == "Boissons"


def test_a_renamed_item_takes_the_aisle_remembered_for_its_new_name(con):
    [gruyere] = add(con, ParsedItem("Gruyère", aisle_id=aisle(con, "Fromages")))
    groceries.remove(con, gruyere)
    [fromage] = add(con, ParsedItem("Fromage"))

    assert groceries.update_item(con, fromage, name="gruyère").aisle == "Fromages"


def test_an_unknown_aisle_is_refused(con):
    [lait] = add(con, ParsedItem("Lait"))
    with pytest.raises(ValueError, match="Aucun rayon"):
        groceries.update_item(con, lait, aisle_id=999)


def test_the_list_is_grouped_by_aisle_in_store_order(con):
    add(
        con,
        ParsedItem("Lait", aisle_id=aisle(con, "Produits laitiers et œufs")),
        ParsedItem("Piles"),
        ParsedItem("Pommes", tags=["Tarte"], aisle_id=aisle(con, "Fruits et légumes")),
        ParsedItem("Beurre", tags=["Tarte"], aisle_id=aisle(con, "Produits laitiers et œufs")),
    )

    grouped = groceries.by_aisle(groceries.list_items(con))
    assert [(a, [i.name for i in items]) for a, items in grouped] == [
        ("Fruits et légumes", ["Pommes"]),
        ("Produits laitiers et œufs", ["Lait", "Beurre"]),
        ("Autre", ["Piles"]),
    ]
    tarte = groceries.by_aisle(groceries.list_items(con, "Tarte"))
    assert [a for a, _ in tarte] == ["Fruits et légumes", "Produits laitiers et œufs"]
