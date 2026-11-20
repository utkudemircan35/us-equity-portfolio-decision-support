import sys
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = PROJECT_ROOT / "backend"

if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


import database
import database_setup
import portfolio_refresh_service
import transaction_service


@pytest.fixture
def isolated_database(
    tmp_path,
    monkeypatch,
):
    """
    Create a fresh temporary portfolio database
    for one test.
    """

    test_database_path = (
        tmp_path
        / "portfolio_test.db"
    )

    monkeypatch.setattr(
        database,
        "DATABASE_PATH",
        test_database_path,
    )

    monkeypatch.setattr(
        database_setup,
        "DATABASE_PATH",
        test_database_path,
    )

    monkeypatch.setattr(
        portfolio_refresh_service,
        "DATABASE_PATH",
        test_database_path,
    )

    database_setup.create_database()

    yield test_database_path


@pytest.fixture
def no_split_history(
    monkeypatch,
):
    """
    Prevent unit tests from making stock-split
    network requests.
    """

    monkeypatch.setattr(
        transaction_service,
        "download_split_history_strict",
        lambda ticker: [],
    )
