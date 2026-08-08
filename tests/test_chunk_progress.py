from datetime import datetime, timedelta

from core.types import ChunkStatus, VideoChunk
from database.storage import StorageManager


def _chunk(path: str, start: datetime, status: ChunkStatus) -> VideoChunk:
    return VideoChunk(
        file_path=path,
        start_time=start,
        end_time=start + timedelta(minutes=1),
        duration_seconds=60,
        status=status,
    )


def test_chunk_progress_groups_statuses_for_selected_date(tmp_path):
    storage = StorageManager(tmp_path / "progress.db", use_pool=False)
    selected = datetime(2026, 8, 8, 12, 0)

    try:
        statuses = [
            ChunkStatus.PENDING,
            ChunkStatus.PROCESSING,
            ChunkStatus.COMPLETED,
            ChunkStatus.COMPLETED,
            ChunkStatus.FAILED,
        ]
        for index, status in enumerate(statuses):
            storage.save_chunk(_chunk(f"selected-{index}.mp4", selected, status))

        storage.save_chunk(
            _chunk("other-day.mp4", selected - timedelta(days=1), ChunkStatus.COMPLETED)
        )

        assert storage.get_chunk_progress_for_date(selected) == {
            "pending": 1,
            "processing": 1,
            "completed": 2,
            "failed": 1,
            "total": 5,
        }
    finally:
        storage.close()
