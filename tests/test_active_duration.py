from datetime import datetime, timedelta, timezone

from core.analysis import assign_active_durations
from core.types import ActivityCard, VideoChunk
from database.storage import StorageManager


def test_active_duration_excludes_recording_gaps():
    start = datetime(2026, 8, 8, 10, 0)
    chunks = [
        VideoChunk(start_time=start, end_time=start + timedelta(minutes=1)),
        VideoChunk(
            start_time=start + timedelta(minutes=5),
            end_time=start + timedelta(minutes=6),
        ),
    ]
    card = ActivityCard(
        start_time=start,
        end_time=start + timedelta(minutes=6),
    )

    assign_active_durations([card], chunks)

    assert card.active_duration_seconds == 120
    assert card.duration_minutes == 2


def test_active_duration_does_not_double_count_overlapping_cards():
    start = datetime(2026, 8, 8, 10, 0, tzinfo=timezone.utc)
    chunk = VideoChunk(start_time=start, end_time=start + timedelta(seconds=60))
    first = ActivityCard(
        start_time=start,
        end_time=start + timedelta(seconds=50),
    )
    second = ActivityCard(
        start_time=start + timedelta(seconds=40),
        end_time=start + timedelta(seconds=60),
    )

    assign_active_durations([first, second], [chunk])

    assert first.active_duration_seconds == 40
    assert second.active_duration_seconds == 20
    assert first.active_duration_seconds + second.active_duration_seconds == 60


def test_legacy_card_falls_back_to_wall_clock_duration():
    start = datetime(2026, 8, 8, 10, 0)
    card = ActivityCard(start_time=start, end_time=start + timedelta(minutes=3))

    assert card.active_duration_seconds is None
    assert card.duration_minutes == 3


def test_active_duration_round_trips_through_storage(tmp_path):
    storage = StorageManager(tmp_path / "duration.db", use_pool=False)
    start = datetime(2026, 8, 8, 10, 0)
    card = ActivityCard(
        category="编程",
        title="实现精确时长",
        start_time=start,
        end_time=start + timedelta(minutes=10),
        active_duration_seconds=420,
    )

    try:
        storage.save_card(card)
        loaded = storage.get_cards_for_date(start)
        assert len(loaded) == 1
        assert loaded[0].active_duration_seconds == 420
        assert loaded[0].duration_minutes == 7
    finally:
        storage.close()
