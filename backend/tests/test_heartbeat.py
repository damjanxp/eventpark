import os
import time
from pathlib import Path

import pytest

from eventpark.core import heartbeat


@pytest.fixture(autouse=True)
def heartbeat_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "hb"
    monkeypatch.setenv("HEARTBEAT_FILE", str(path))
    return path


def test_no_beat_yet_is_not_alive() -> None:
    assert heartbeat.age_seconds() is None
    assert not heartbeat.is_alive()


def test_fresh_beat_is_alive() -> None:
    heartbeat.beat()
    assert heartbeat.is_alive(max_age=5)


def test_stale_beat_is_not_alive(heartbeat_file: Path) -> None:
    heartbeat.beat()
    old = time.time() - 120
    os.utime(heartbeat_file, (old, old))
    assert not heartbeat.is_alive(max_age=90)
    assert heartbeat.is_alive(max_age=200)


def test_max_age_from_env(heartbeat_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    heartbeat.beat()
    old = time.time() - 100
    os.utime(heartbeat_file, (old, old))
    monkeypatch.setenv("HEARTBEAT_MAX_AGE", "150")
    assert heartbeat.is_alive()


def test_beat_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HEARTBEAT_FILE", "/nonexistent-dir/hb")
    heartbeat.beat()  # must not raise
