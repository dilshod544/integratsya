"""Сборщик: lock только на fetch+ingest; постобработка не блокирует следующий опрос."""
from __future__ import annotations

import threading
import time

from atm_monitor import scheduler


def test_run_once_releases_lock_before_post_poll(monkeypatch):
    order = []
    post_started = threading.Event()
    release_post = threading.Event()

    monkeypatch.setattr(scheduler, "fetch_all_atms", lambda: [{"id": 1}])
    monkeypatch.setattr(scheduler, "ingest", lambda items: 42)

    def slow_post():
        order.append("post_start")
        post_started.set()
        release_post.wait(timeout=2)
        order.append("post_end")

    monkeypatch.setattr(scheduler, "_post_poll", slow_post)

    t = threading.Thread(target=lambda: order.append(("result", scheduler.run_once())))
    t.start()
    assert post_started.wait(timeout=2)

    # Пока постобработка ещё идёт — lock уже свободен, второй опрос может стартовать.
    second = {"ok": None}

    def second_fetch():
        order.append("second_fetch")
        return [{"id": 2}]

    monkeypatch.setattr(scheduler, "fetch_all_atms", second_fetch)
    monkeypatch.setattr(scheduler, "ingest", lambda items: 43)
    monkeypatch.setattr(scheduler, "_post_poll", lambda: order.append("second_post"))

    second["ok"] = scheduler.run_once()
    release_post.set()
    t.join(timeout=2)

    assert second["ok"] is True
    assert order[0] == "post_start"
    assert "second_fetch" in order
    assert ("result", True) in order


def test_run_once_skips_when_locked(monkeypatch):
    monkeypatch.setattr(scheduler, "fetch_all_atms", lambda: (_ for _ in ()).throw(AssertionError("should not fetch")))
    assert scheduler._run_lock.acquire(blocking=False)
    try:
        assert scheduler.run_once() is False
    finally:
        scheduler._run_lock.release()
