import pytest

from app.groceries import ParsedItem, combine_quantities, normalize, parse_line, parse_text


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("lait", ParsedItem("Lait")),
        ("lait 2 L", ParsedItem("Lait", "2 L")),
        ("lait 2", ParsedItem("Lait", "2")),
        ("3 oignons", ParsedItem("Oignons", "3")),
        ("1 lb bœuf haché", ParsedItem("Bœuf haché", "1 lb")),
        ("mozzarella 400g", ParsedItem("Mozzarella", "400 g")),
        ("2 boîtes sauce tomate", ParsedItem("Sauce tomate", "2 boîtes")),
        ("½ tasse sucre", ParsedItem("Sucre", "½ tasse")),
        ("oignons 2 #lasagne", ParsedItem("Oignons", "2", ["Lasagne"])),
        ("gruyère #soupe_à_l'oignon", ParsedItem("Gruyère", "", ["Soupe à l'oignon"])),
        ("- 3 carottes", ParsedItem("Carottes", "3")),
        ("2. pâtes à lasagne,", ParsedItem("Pâtes à lasagne")),
        ("7 up", ParsedItem("Up", "7")),
    ],
)
def test_parse_line(line, expected):
    assert parse_line(line) == expected


@pytest.mark.parametrize("line", ["", "   ", "#lasagne", "- "])
def test_parse_line_without_a_name(line):
    assert parse_line(line) is None


def test_a_number_alone_is_a_name():
    assert parse_line("2") == ParsedItem("2")


def test_parse_text_applies_the_tag_to_every_line():
    items = parse_text("3 oignons\n\n1 lb bœuf haché #souper", tag="lasagne")
    assert items == [
        ParsedItem("Oignons", "3", ["Lasagne"]),
        ParsedItem("Bœuf haché", "1 lb", ["Souper", "Lasagne"]),
    ]


def test_normalize_ignores_case_accents_ligatures_and_spaces():
    assert normalize("  Œufs  BRUNS ") == "oeufs bruns"
    assert normalize("Pâtés") == normalize("pates")


@pytest.mark.parametrize(
    ("first", "second", "expected"),
    [("2", "1", "2 + 1"), ("", "1", "1"), ("2 L", "", "2 L"), ("", "", "")],
)
def test_combine_quantities(first, second, expected):
    assert combine_quantities(first, second) == expected
