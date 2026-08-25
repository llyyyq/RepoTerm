from repoterm.ui.tui.renderer import _get_transcript_snapshot
from repoterm.ui.tui.state import ScreenState
from repoterm.ui.tui.tool_lifecycle import _push_transcript_entry
from repoterm.ui.tui.runtime_control import RENDER_INTERVAL_SECONDS, _ThrottledRenderer


def test_transcript_snapshot_reuses_list_until_revision_changes() -> None:
    state = ScreenState()
    _push_transcript_entry(state, kind="assistant", body="hello")

    first = _get_transcript_snapshot(state)
    second = _get_transcript_snapshot(state)

    assert second is first

    _push_transcript_entry(state, kind="assistant", body="world")
    third = _get_transcript_snapshot(state)

    assert third is not first
    assert len(third) == 2


def test_one_refresh_cycle_coalesces_many_requests(monkeypatch) -> None:
    clock = [10.0]
    calls: list[str] = []
    monkeypatch.setattr("repoterm.ui.tui.runtime_control.time.monotonic", lambda: clock[0])
    throttled = _ThrottledRenderer(lambda: calls.append("render"))

    for _ in range(100):
        throttled.request()
    throttled.flush()

    assert calls == ["render"]

    for _ in range(100):
        throttled.request()
    throttled.flush()
    assert calls == ["render"]

    clock[0] += RENDER_INTERVAL_SECONDS * 2
    throttled.flush()
    assert calls == ["render", "render"]


def test_force_renders_immediately_and_clears_pending(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr("repoterm.ui.tui.runtime_control.time.monotonic", lambda: 20.0)
    throttled = _ThrottledRenderer(lambda: calls.append("render"))

    throttled.request()
    throttled.force()
    throttled.flush()

    assert calls == ["render"]
