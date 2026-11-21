from contextlib import closing

import pytest
from fastapi.testclient import TestClient

from app.db import connect, migrate
from app.main import create_app
from app.settings import Settings

PEOPLE = {"jacob@example.com": "Jacob", "catherine@example.com": "Catherine"}


@pytest.fixture
def con(tmp_path):
    with closing(connect(tmp_path / "test.sqlite3")) as con:
        migrate(con)
        yield con


@pytest.fixture
def settings(tmp_path):
    return Settings(data_dir=tmp_path, people=PEOPLE, trusted_proxies=frozenset({"testclient"}))


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings), headers={"X-Email": "jacob@example.com"}) as client:
        yield client
