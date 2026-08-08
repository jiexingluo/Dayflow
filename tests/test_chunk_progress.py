from datetime import datetime, timedelta
from pathlib import Path

from core.types import AnalysisBatch, BatchStatus, ChunkStatus, VideoChunk
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


def test_recover_interrupted_analysis_only_retries_existing_files(tmp_path):
    storage = StorageManager(tmp_path / "recovery.db", use_pool=False)
    selected = datetime(2026, 8, 8, 12, 0)
    processing_file = tmp_path / "processing.mp4"
    failed_file = tmp_path / "failed.mp4"
    processing_file.write_bytes(b"video")
    failed_file.write_bytes(b"video")

    try:
        processing_id = storage.save_chunk(
            _chunk(str(processing_file), selected, ChunkStatus.PROCESSING)
        )
        failed_id = storage.save_chunk(
            _chunk(str(failed_file), selected, ChunkStatus.FAILED)
        )
        missing_id = storage.save_chunk(
            _chunk(str(tmp_path / "missing.mp4"), selected, ChunkStatus.PROCESSING)
        )
        completed_id = storage.save_chunk(
            _chunk(str(tmp_path / "completed.mp4"), selected, ChunkStatus.COMPLETED)
        )
        batch_id = storage.create_batch(AnalysisBatch(
            chunk_ids=[processing_id, missing_id],
            start_time=selected,
            end_time=selected + timedelta(minutes=2),
            status=BatchStatus.PROCESSING,
        ))
        storage.update_batch(batch_id, BatchStatus.PROCESSING)

        result = storage.recover_interrupted_analysis()

        assert result == {
            "recovered": 2,
            "missing": 1,
            "interrupted_batches": 1,
        }
        with storage._get_connection() as conn:
            statuses = {
                row["id"]: (row["status"], row["batch_id"])
                for row in conn.execute("SELECT id, status, batch_id FROM chunks")
            }
            batch = conn.execute(
                "SELECT status, error_message FROM analysis_batches WHERE id = ?",
                (batch_id,),
            ).fetchone()

        assert statuses[processing_id] == ("pending", None)
        assert statuses[failed_id] == ("pending", None)
        assert statuses[missing_id] == ("failed", None)
        assert statuses[completed_id] == ("completed", None)
        assert batch["status"] == "failed"
        assert "restored to pending" in batch["error_message"]
    finally:
        storage.close()


def test_pending_chunks_prioritize_today_over_historical_backlog(tmp_path):
    storage = StorageManager(tmp_path / "priority.db", use_pool=False)
    today = datetime.now().replace(hour=12, minute=0, second=0, microsecond=0)

    try:
        storage.save_chunk(_chunk("historical.mp4", today - timedelta(days=30), ChunkStatus.PENDING))
        storage.save_chunk(_chunk("today-later.mp4", today + timedelta(minutes=1), ChunkStatus.PENDING))
        storage.save_chunk(_chunk("today-earlier.mp4", today, ChunkStatus.PENDING))

        pending = storage.get_pending_chunks()

        assert [Path(chunk.file_path).name for chunk in pending] == [
            "today-earlier.mp4",
            "today-later.mp4",
            "historical.mp4",
        ]
    finally:
        storage.close()
