from datetime import UTC, date, datetime, timedelta

import pytest

from app import tasks
from app.tasks import Fields

PEOPLE = ["Jacob", "Catherine"]


def add(con, title="appeler le plombier", added_by="Jacob", **fields):
    [task_id] = tasks.add(con, [Fields(title, **fields)], added_by, PEOPLE)
    return tasks.get(con, task_id)


def titles(found):
    return [t.title for t in found]


def test_a_new_task_is_normal_unassigned_and_to_do(con):
    task = add(con, "  appeler   le plombier ")
    assert task.title == "Appeler le plombier"
    assert (task.status, task.priority, task.assignee, task.due) == ("todo", 1, "", None)
    assert task.priority_label == "Normale"
    assert task.next_column.label == "En cours"
    assert task.added_by == "Jacob"


def test_the_assignee_must_be_one_of_us(con):
    assert add(con, assignee="catherine").assignee == "Catherine"
    with pytest.raises(ValueError, match="Personne inconnue : « Paul ». Choix : Jacob, Catherine"):
        add(con, assignee="Paul")


def test_one_bad_task_adds_none(con):
    with pytest.raises(ValueError, match="titre"):
        tasks.add(con, [Fields("Tondre"), Fields("  ")], "Claude", PEOPLE)
    assert tasks.list_tasks(con) == []


def test_columns_sort_by_priority_then_due_date_then_age(con):
    soon = tasks.today() + timedelta(days=3)
    later = tasks.today() + timedelta(days=9)
    add(con, "Normale sans échéance")
    add(con, "Basse", priority=0)
    add(con, "Normale plus tard", due=later)
    add(con, "Haute", priority=2)
    add(con, "Normale bientôt", due=soon)
    add(con, "En cours", status="doing")

    board = tasks.board(con)
    assert titles(board["todo"]) == [
        "Haute",
        "Normale bientôt",
        "Normale plus tard",
        "Normale sans échéance",
        "Basse",
    ]
    assert titles(board["doing"]) == ["En cours"]
    assert board["done"] == []


def test_done_shows_the_last_two_weeks_newest_first(con):
    old = add(con, "Vieille", status="done")
    first = add(con, "Première", status="done", priority=2)
    second = add(con, "Deuxième", status="done", priority=0)
    stamps = {old.id: 15, first.id: 2, second.id: 1}
    for task_id, days in stamps.items():
        done_at = datetime.now(UTC) - timedelta(days=days)
        con.execute("UPDATE tasks SET done_at = ? WHERE id = ?", (done_at.isoformat(), task_id))

    assert titles(tasks.list_tasks(con, "done")) == ["Deuxième", "Première"]
    assert tasks.get(con, old.id) is not None


def test_filter_by_person_or_nobody(con):
    add(con, "Pour Jacob", assignee="Jacob")
    add(con, "Pour Catherine", assignee="Catherine")
    add(con, "Pour personne")
    assert titles(tasks.list_tasks(con, assignee="Catherine")) == ["Pour Catherine"]
    assert titles(tasks.list_tasks(con, assignee="")) == ["Pour personne"]
    assert len(tasks.list_tasks(con)) == 3


def test_moving_into_done_stamps_the_end_and_leaving_clears_it(con):
    task = add(con)
    done = tasks.update(con, task.id, Fields(task.title, status="done"), PEOPLE)
    assert done.done_on == tasks.today()
    assert done.next_column is None

    stamp = con.execute("SELECT done_at FROM tasks WHERE id = ?", (task.id,)).fetchone()[0]
    tasks.update(con, task.id, Fields("Renommée", status="done"), PEOPLE)
    assert con.execute("SELECT done_at FROM tasks WHERE id = ?", (task.id,)).fetchone()[0] == stamp

    back = tasks.update(con, task.id, Fields(task.title, status="todo"), PEOPLE)
    assert back.done_on is None


def test_update_replaces_every_field(con):
    task = add(con, note="Fuite", assignee="Jacob", due=date(2026, 12, 1), priority=2)
    updated = tasks.update(con, task.id, Fields("Rappeler le plombier"), PEOPLE)
    assert (updated.title, updated.note, updated.assignee, updated.due, updated.priority) == (
        "Rappeler le plombier",
        "",
        "",
        None,
        1,
    )
    with pytest.raises(LookupError):
        tasks.update(con, 999, Fields("x"), PEOPLE)


def test_advance_only_moves_from_the_column_the_card_was_in(con):
    task = add(con)
    assert tasks.advance(con, task.id, "todo")
    # Double toucher : la tâche est déjà en cours, elle ne saute pas à Fait.
    assert not tasks.advance(con, task.id, "todo")
    assert tasks.get(con, task.id).status == "doing"

    assert tasks.advance(con, task.id, "doing")
    assert tasks.get(con, task.id).done_on == tasks.today()
    assert not tasks.advance(con, task.id, "done")


def test_remove(con):
    task = add(con)
    assert tasks.remove(con, task.id)
    assert not tasks.remove(con, task.id)


def test_late_only_when_past_due_and_not_done(con):
    yesterday = tasks.today() - timedelta(days=1)
    assert add(con, due=yesterday).late
    assert not add(con, due=tasks.today()).late
    assert not add(con, due=yesterday, status="done").late
    assert not add(con).late


@pytest.mark.parametrize(
    ("day", "label"),
    [
        (date(2026, 11, 23), "aujourd'hui"),
        (date(2026, 11, 24), "demain"),
        (date(2026, 11, 22), "hier"),
        (date(2026, 11, 20), "ven. 20 nov."),
        (date(2026, 12, 5), "sam. 5 déc."),
        (date(2027, 2, 1), "lun. 1 févr. 2027"),
    ],
)
def test_day_labels_are_in_french(day, label):
    assert tasks.day_label(day, date(2026, 11, 23)) == label


def test_due_dates_are_iso(con):
    assert tasks.parse_due(" 2026-11-28 ") == date(2026, 11, 28)
    assert tasks.parse_due("") is None
    with pytest.raises(ValueError, match="AAAA-MM-JJ"):
        tasks.parse_due("vendredi")
