from datetime import UTC, datetime, timedelta

import pytest

from app import groceries, purchases, tasks
from app.purchases import Fields


def add(con, title="système de son", added_by="Jacob", **fields):
    [purchase_id] = purchases.add(con, [Fields(title, **fields)], added_by)
    return purchases.get(con, purchase_id)


def titles(found):
    return [p.title for p in found]


def bought_days_ago(con, purchase, days):
    stamp = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    con.execute("UPDATE purchases SET bought_at = ? WHERE id = ?", (stamp, purchase.id))


def test_a_new_purchase_is_a_normal_idea_without_amounts(con):
    purchase = add(con, "  système   de son ", tags=["music room", "Music Room", " "])
    assert purchase.title == "Système de son"
    assert (purchase.status, purchase.priority, purchase.budget, purchase.paid) == (
        "idea",
        1,
        None,
        None,
    )
    assert (purchase.status_label, purchase.priority_label) == ("Idée", "Normale")
    assert purchase.tags == ["Music room"]
    assert purchase.added_by == "Jacob"
    assert purchase.bought_on is None


def test_tags_are_shared_with_groceries_but_not_counted_there(con):
    groceries.add_items(con, [groceries.ParsedItem("Oignons", tags=["Lasagne"])], "Jacob")
    add(con, tags=["lasagne"])
    assert add(con, "Panneaux", tags=["LASAGNE"]).tags == ["Lasagne"]
    assert groceries.tag_counts(con) == [("Lasagne", 1)]
    assert purchases.tag_names(con) == ["Lasagne"]


def test_one_bad_purchase_adds_none(con):
    with pytest.raises(ValueError, match="titre"):
        purchases.add(con, [Fields("Divan", tags=["salon"]), Fields("  ")], "Claude")
    assert purchases.list_purchases(con) == []
    assert purchases.tag_names(con) == []


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"status": "maybe"}, "Statut inconnu"),
        ({"priority": 3}, "Priorité inconnue"),
        ({"budget": -5}, "Montant hors limites"),
        ({"paid": 10**12}, "Montant hors limites"),
    ],
)
def test_invalid_fields_are_refused(con, fields, message):
    with pytest.raises(ValueError, match=message):
        add(con, **fields)


def test_open_purchases_sort_by_priority_then_budget_then_age(con):
    add(con, "Normale sans budget")
    add(con, "Normale 300", budget=300)
    add(con, "Haute 1100", priority=2, budget=1100)
    add(con, "Normale 900", budget=900, status="ready")
    add(con, "Haute 1200", priority=2, budget=1200)
    add(con, "Basse 2000", priority=0, budget=2000)
    old = add(con, "Achat ancien", status="bought")
    add(con, "Achat récent", status="bought")
    bought_days_ago(con, old, 3)

    assert titles(purchases.list_purchases(con)) == [
        "Haute 1200",
        "Haute 1100",
        "Normale 900",
        "Normale 300",
        "Normale sans budget",
        "Basse 2000",
        "Achat récent",
        "Achat ancien",
    ]


def test_filter_by_tag_ignores_case_and_accents(con):
    add(con, "Laveuse", tags=["Buanderie"])
    add(con, "Divan", tags=["Salon"])
    assert titles(purchases.list_purchases(con, "buandérie")) == ["Laveuse"]
    assert purchases.list_purchases(con, "garage") == []


def test_totals_add_budgets_by_open_status(con):
    add(con, "Son", status="ready", budget=1200)
    add(con, "Laveuse", status="researching", budget=1100)
    add(con, "Panneaux", status="researching", budget=350)
    add(con, "Divan", budget=2000)
    add(con, "Vélos", budget=150)
    add(con, "Sans budget")
    add(con, "Aspirateur", status="bought", budget=550, paid=480)
    assert purchases.totals(purchases.list_purchases(con)) == {
        "ready": 1200,
        "researching": 1450,
        "idea": 2150,
    }


def test_buying_stamps_the_day_and_going_back_clears_it(con):
    purchase = add(con, budget=1200)
    bought = purchases.update(con, purchase.id, Fields(purchase.title, status="bought", paid=1150))
    assert bought.bought_on == tasks.today()
    assert bought.recently_bought

    stamp = con.execute("SELECT bought_at FROM purchases WHERE id = ?", (purchase.id,)).fetchone()
    purchases.update(con, purchase.id, Fields("Renommé", status="bought"))
    assert (
        con.execute("SELECT bought_at FROM purchases WHERE id = ?", (purchase.id,)).fetchone()
        == stamp
    )

    back = purchases.update(con, purchase.id, Fields(purchase.title, status="ready"))
    assert back.bought_on is None


def test_update_replaces_every_field_and_the_tags(con):
    purchase = add(con, details="HS8", priority=2, budget=1200, paid=900, tags=["Music room"])
    updated = purchases.update(con, purchase.id, Fields("Enceintes", tags=["Salon"]))
    assert (updated.title, updated.details, updated.priority, updated.budget, updated.paid) == (
        "Enceintes",
        "",
        1,
        None,
        None,
    )
    assert updated.tags == ["Salon"]
    with pytest.raises(LookupError, match="Aucun gros achat 999"):
        purchases.update(con, 999, Fields("x"))


def test_recently_bought_covers_thirty_days(con):
    purchase = add(con, status="bought")
    bought_days_ago(con, purchase, 29)
    assert purchases.get(con, purchase.id).recently_bought
    bought_days_ago(con, purchase, 31)
    assert not purchases.get(con, purchase.id).recently_bought


def test_remove_takes_the_tags_along(con):
    purchase = add(con, tags=["Salon"])
    assert purchases.remove(con, purchase.id)
    assert not purchases.remove(con, purchase.id)
    assert purchases.tag_names(con) == []


@pytest.mark.parametrize(
    ("text", "amount"),
    [
        ("", None),
        ("  ", None),
        ("0", None),
        ("1200", 1200),
        ("1 200 $", 1200),
        ("1 200 $", 1200),
        ("~1200", 1200),
        ("≈ 1 200 $", 1200),
        ("1,200", 1200),
        ("1.200", 1200),
        ("1,200.50", 1201),
        ("1 200,49", 1200),
        ("479,99", 480),
        ("12.5", 13),
        ("1 234 567", 1234567),
    ],
)
def test_amounts_are_read_in_whole_dollars(text, amount):
    assert purchases.parse_amount(text) == amount


@pytest.mark.parametrize("text", ["abc", "12 piasses", "-50", "1,2345", "1.5k"])
def test_unreadable_amounts_are_refused(text):
    with pytest.raises(ValueError, match="Montant invalide"):
        purchases.parse_amount(text)


def test_amounts_are_written_with_non_breaking_spaces():
    assert purchases.dollars(480) == "480 $"
    assert purchases.dollars(1234567) == "1 234 567 $"


@pytest.mark.parametrize(
    ("budget", "paid", "gap"),
    [
        (550, 480, "70 $ sous le budget"),
        (1200, 1230, "30 $ au-dessus du budget"),
        (500, 500, "pile dans le budget"),
        (None, 480, ""),
        (550, None, ""),
    ],
)
def test_the_gap_between_budget_and_price(con, budget, paid, gap):
    assert add(con, status="bought", budget=budget, paid=paid).gap == gap


def test_links_are_found_in_the_details():
    details = (
        "Yamaha HS8 (https://www.steves.ca/hs8).\n"
        "Caisson : https://amazon.ca/dp/B0?ref=x, ou https://www.steves.ca/hs8 encore.\n"
        "Pas un lien : ftp://exemple.com"
    )
    assert Fields("Son", details=details).links == [
        ("https://www.steves.ca/hs8", "steves.ca/hs8"),
        ("https://amazon.ca/dp/B0?ref=x", "amazon.ca/dp/B0?ref=x"),
    ]
