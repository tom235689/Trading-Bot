from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _repository_root(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests name configs relative to the repository, wherever pytest was started."""
    monkeypatch.chdir(ROOT)
