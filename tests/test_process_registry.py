from __future__ import annotations

import signal

from harness.adapters import process_registry


def test_kill_process_tree_kills_process_group_even_when_parent_exited(monkeypatch) -> None:
    calls: list[tuple[int, int]] = []
    direct_signals: list[int] = []

    class FakeProcess:
        pid = 12345

        def poll(self):
            return 0

        def send_signal(self, sig: int):
            direct_signals.append(sig)

    monkeypatch.setattr(process_registry, "supports_process_groups", lambda: True)
    monkeypatch.setattr(process_registry.os, "killpg", lambda pid, sig: calls.append((pid, sig)))

    process_registry.kill_process_tree(FakeProcess())

    assert calls == [(12345, signal.SIGKILL)]
    assert direct_signals == []


def test_kill_process_tree_falls_back_to_direct_signal_without_process_groups(monkeypatch) -> None:
    direct_signals: list[int] = []

    class FakeProcess:
        pid = 12345

        def poll(self):
            return None

        def send_signal(self, sig: int):
            direct_signals.append(sig)

    monkeypatch.setattr(process_registry, "supports_process_groups", lambda: False)

    process_registry.kill_process_tree(FakeProcess(), signal.SIGTERM)

    assert direct_signals == [signal.SIGTERM]
