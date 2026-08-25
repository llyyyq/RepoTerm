from __future__ import annotations

import hashlib
import io
from threading import Barrier, Thread
from unittest.mock import Mock

from repoterm.ui.tui.frame_diff import compute_frame_update
from repoterm.ui.tui import screen
from repoterm.ui.tui.ui_events import (
    STREAM_COALESCE_CHAR_LIMIT,
    UiEventQueue,
    UiEventType,
)


def test_publish_side_coalesces_10000_chunks_without_losing_text() -> None:
    queue = UiEventQueue()
    source_id = "turn-stream"
    original = "".join(str(index % 10) for index in range(10_000))

    for character in original:
        assert queue.publish(
            UiEventType.ASSISTANT_STREAM_CHUNK,
            source_id,
            {"content": character},
        )

    assert queue.pending < 10_000
    events = queue.drain(max_events=100)
    reconstructed = "".join(str(event.payload["content"]) for event in events)

    assert len(events) == 2
    assert all(len(str(event.payload["content"])) <= STREAM_COALESCE_CHAR_LIMIT for event in events)
    assert reconstructed == original
    assert hashlib.sha256(reconstructed.encode()).hexdigest() == hashlib.sha256(
        original.encode()
    ).hexdigest()
    assert [event.sequence for event in events] == sorted(event.sequence for event in events)


def test_stream_coalescing_respects_source_type_and_control_boundaries() -> None:
    queue = UiEventQueue()
    queue.publish(UiEventType.ASSISTANT_STREAM_CHUNK, "a", {"content": "a1"})
    queue.publish(UiEventType.ASSISTANT_STREAM_CHUNK, "b", {"content": "b1"})
    queue.publish(UiEventType.THINKING_CHUNK, "a", {"content": "t1"})
    queue.publish(UiEventType.ASSISTANT_STREAM_CHUNK, "a", {"content": "a2"})
    queue.publish(UiEventType.RUNTIME_EVENT, "a", {"message": "phase"})
    queue.publish(UiEventType.ASSISTANT_STREAM_CHUNK, "a", {"content": "a3"})
    queue.publish(UiEventType.AGENT_COMPLETED, "a", {"messages": []})
    queue.publish(UiEventType.ASSISTANT_STREAM_CHUNK, "a", {"content": "a4"})
    queue.publish(UiEventType.AGENT_FAILED, "a", {"error": "failed"})

    events = queue.drain(max_events=100)

    assert [event.event_type for event in events] == [
        UiEventType.ASSISTANT_STREAM_CHUNK.value,
        UiEventType.ASSISTANT_STREAM_CHUNK.value,
        UiEventType.THINKING_CHUNK.value,
        UiEventType.ASSISTANT_STREAM_CHUNK.value,
        UiEventType.RUNTIME_EVENT.value,
        UiEventType.ASSISTANT_STREAM_CHUNK.value,
        UiEventType.AGENT_COMPLETED.value,
        UiEventType.ASSISTANT_STREAM_CHUNK.value,
        UiEventType.AGENT_FAILED.value,
    ]
    assert events[0].payload["content"] == "a1"
    assert events[1].payload["content"] == "b1"
    assert events[3].payload["content"] == "a2"
    assert events[5].payload["content"] == "a3"
    assert events[7].payload["content"] == "a4"


def test_concurrent_stream_producers_preserve_each_source_sequence() -> None:
    queue = UiEventQueue()
    producer_count = 4
    chunks_per_producer = 500
    barrier = Barrier(producer_count + 1)

    def produce(producer: int) -> None:
        barrier.wait()
        for index in range(chunks_per_producer):
            queue.publish(
                UiEventType.ASSISTANT_STREAM_CHUNK,
                f"producer-{producer}",
                {"content": f"{producer}:{index};"},
            )

    threads = [Thread(target=produce, args=(producer,)) for producer in range(producer_count)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(timeout=3)
        assert not thread.is_alive()

    events = queue.drain(max_events=10_000)
    observed: dict[int, list[int]] = {producer: [] for producer in range(producer_count)}
    for event in events:
        for token in str(event.payload["content"]).split(";"):
            if token:
                producer_text, index_text = token.split(":")
                observed[int(producer_text)].append(int(index_text))

    assert all(
        observed[producer] == list(range(chunks_per_producer))
        for producer in range(producer_count)
    )
    sequences = [event.sequence for event in events]
    assert sequences == sorted(sequences)
    assert len(set(sequences)) == len(sequences)


def test_frame_diff_reports_deterministic_incremental_savings() -> None:
    incremental = compute_frame_update(
        "header\nold\nfooter",
        "header\nnew\nfooter",
        terminal_size=(80, 24),
        previous_terminal_size=(80, 24),
    )
    full = compute_frame_update(
        "header\nold\nfooter",
        "header\nnew\nfooter",
        terminal_size=(80, 24),
        previous_terminal_size=(79, 24),
    )

    assert incremental.mode == "incremental"
    assert full.mode == "full"
    assert len(incremental.payload) < len(full.payload)
    assert "new" in incremental.payload
    assert "new" in full.payload


def test_write_frame_uses_one_write_and_flush_and_skips_identical_frame(
    monkeypatch,
) -> None:
    stream = io.StringIO()
    stream.isatty = lambda: True
    stream.write = Mock(wraps=stream.write)
    stream.flush = Mock(wraps=stream.flush)
    monkeypatch.setattr(screen.sys, "stdout", stream)
    monkeypatch.setattr(screen, "_cached_terminal_size", lambda: (80, 24))
    screen.invalidate_frame_cache()

    screen.write_frame("header\nold\nfooter")
    assert stream.write.call_count == 1
    assert stream.flush.call_count == 1

    screen.write_frame("header\nold\nfooter")
    assert stream.write.call_count == 1
    assert stream.flush.call_count == 1

    screen.write_frame("header\nnew\nfooter")
    assert stream.write.call_count == 2
    assert stream.flush.call_count == 2
