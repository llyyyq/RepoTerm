from __future__ import annotations

import logging

from repoterm.ui.tui.input_parser import KeyEvent, TextEvent, parse_input_chunk


PASTE_START = "\x1b[200~"
PASTE_END = "\x1b[201~"


def _parse_chunks(chunks: list[str]) -> tuple[list[object], str]:
    """Feed chunks through the parser exactly as the TTY loop does."""

    remainder = ""
    events: list[object] = []
    for chunk in chunks:
        result = parse_input_chunk(remainder + chunk, incoming_chunk=chunk)
        events.extend(result.events)
        remainder = result.rest
    return events, remainder


def test_bracketed_paste_is_one_text_event_and_never_submits() -> None:
    payload = "中文第一行\r\n第二行\n第三行"

    result = parse_input_chunk(PASTE_START + payload + PASTE_END, incoming_chunk=payload)

    assert result.rest == ""
    assert len(result.events) == 1
    event = result.events[0]
    assert isinstance(event, TextEvent)
    assert event.text == "中文第一行\n第二行\n第三行"
    assert event.ctrl is False
    assert event.meta is False
    assert not any(isinstance(item, KeyEvent) and item.name == "return" for item in result.events)


def test_bracketed_paste_waits_for_end_marker_across_chunks() -> None:
    first = parse_input_chunk(PASTE_START + "第一行\r\n第二", incoming_chunk="第一行\r\n第二")

    assert first.events == []
    assert first.rest == PASTE_START + "第一行\r\n第二"

    second = parse_input_chunk(first.rest + "行" + PASTE_END, incoming_chunk="行" + PASTE_END)

    assert second.rest == ""
    assert len(second.events) == 1
    assert isinstance(second.events[0], TextEvent)
    assert second.events[0].text == "第一行\n第二行"


def test_bracketed_paste_markers_and_body_can_each_be_split() -> None:
    chunks = [
        PASTE_START[:2],
        PASTE_START[2:] + "开\r",
        "\n中",
        "文",
        PASTE_END[:3],
        PASTE_END[3:],
    ]

    events, remainder = _parse_chunks(chunks)

    assert remainder == ""
    assert len(events) == 1
    assert isinstance(events[0], TextEvent)
    assert events[0].text == "开\n中文"
    assert not any(isinstance(item, KeyEvent) and item.name == "return" for item in events)


def test_empty_bracketed_paste_is_an_empty_text_event() -> None:
    result = parse_input_chunk(PASTE_START + PASTE_END, incoming_chunk=PASTE_START + PASTE_END)

    assert result.rest == ""
    assert len(result.events) == 1
    assert isinstance(result.events[0], TextEvent)
    assert result.events[0].text == ""
    assert not any(isinstance(item, KeyEvent) and item.name == "return" for item in result.events)


def test_bracketed_paste_handles_at_least_ten_thousand_characters() -> None:
    payload = "中文日志\r\n" * 2_000

    result = parse_input_chunk(PASTE_START + payload + PASTE_END, incoming_chunk=payload)

    assert len(payload) >= 10_000
    assert result.rest == ""
    assert len(result.events) == 1
    assert isinstance(result.events[0], TextEvent)
    assert result.events[0].text == "中文日志\n" * 2_000


def test_paste_payload_is_not_written_to_debug_logs(caplog) -> None:
    payload = "secret-paste-content-中文"

    with caplog.at_level(logging.DEBUG):
        parse_input_chunk(PASTE_START + payload + PASTE_END, incoming_chunk=payload)

    assert payload not in caplog.text
