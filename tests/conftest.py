from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _repository_root(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests name configs relative to the repository, wherever pytest was started."""
    monkeypatch.chdir(ROOT)


@pytest.fixture(autouse=True)
def _no_release_lookups(monkeypatch: pytest.MonkeyPatch) -> None:
    """Doctor asks the clone's remote for newer releases: never from a test."""
    monkeypatch.setattr("tbot.live.doctor.newest_release", lambda *args, **kwargs: None)


@pytest.fixture(autouse=True)
def _private_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Doctor's check of who can read the developer's own .env is no part of a test."""
    monkeypatch.setattr("tbot.live.doctor.env_readers", lambda path: [])
