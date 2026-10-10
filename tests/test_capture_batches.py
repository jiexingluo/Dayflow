import json
from datetime import datetime, timedelta

from core.types import ActivityCard, AnalysisBatch, BatchStatus, CaptureBatch, ChunkStatus
from database.storage import StorageManager


def _capture(tmp_path, status=ChunkStatus.PENDING):
    directory = tmp_path / "captures" / "2026-09-21" / "10-00"
    directory.mkdir(parents=True)
    manifest = directory / "manifest.json"
    manifest.write_text('{"images": []}', encoding="utf-8")
    start = datetime(2026, 9, 21, 10, 0)
    return CaptureBatch(
        directory_path=str(directory),
        manifest_path=str(manifest),
        start_time=start,
        end_time=start + timedelta(minutes=15),
        duration_seconds=900,
        image_count=90,
        status=status,
    )


def test_capture_batch_round_trips_and_can_be_queued(tmp_path):
    storage = StorageManager(tmp_path / "capture.db", use_pool=False)
    try:
        capture_id = storage.save_capture_batch(_capture(tmp_path))
        pending = storage.get_pending_capture_batches()
        assert [batch.id for batch in pending] == [capture_id]
        assert pending[0].image_count == 90

        analysis_id = storage.create_batch(AnalysisBatch(
            start_time=pending[0].start_time,
            end_time=pending[0].end_time,
            status=BatchStatus.PENDING,
            source_type="capture",
            source_ids=[capture_id],
        ))
        storage.update_capture_batch_status(capture_id, ChunkStatus.PROCESSING, analysis_id)
        with storage._get_connection() as conn:
            row = conn.execute(
                "SELECT status, analysis_batch_id FROM capture_batches WHERE id = ?",
                (capture_id,),
            ).fetchone()
        assert tuple(row) == ("processing", analysis_id)
    finally:
        storage.close()


def test_reconcile_marks_missing_capture_batch_orphaned(tmp_path):
    storage = StorageManager(tmp_path / "orphaned.db", use_pool=False)
    try:
        batch = _capture(tmp_path)
        capture_id = storage.save_capture_batch(batch)
        batch.manifest_path = str(tmp_path / "missing" / "manifest.json")
        storage.save_capture_batch(batch)
        result = storage.reconcile_missing_inputs()
        assert result["capture_batches"] == 1
        with storage._get_connection() as conn:
            status = conn.execute(
                "SELECT status FROM capture_batches WHERE id = ?", (capture_id,)
            ).fetchone()[0]
        assert status == "orphaned"
    finally:
        storage.close()


def test_recover_capture_analysis_registers_manifest_and_requeues_failures(tmp_path):
    storage = StorageManager(tmp_path / "recovery.db", use_pool=False)
    root = tmp_path / "captures"
    directory = root / "2026-09-21" / "10-00"
    directory.mkdir(parents=True)
    image = directory / "capture.jpg"
    image.write_bytes(b"jpeg")
    captured_at = datetime.now() - timedelta(hours=1)
    manifest = directory / "manifest.json"
    manifest.write_text(json.dumps({
        "window_start": captured_at.isoformat(),
        "window_end": (captured_at + timedelta(minutes=5)).isoformat(),
        "interval_seconds": 10,
        "images": [{
            "file": image.name,
            "timestamp": captured_at.isoformat(),
            "relative_seconds": 0,
        }],
    }), encoding="utf-8")

    try:
        result = storage.recover_capture_analysis(root, max_retries=3)
        assert result["registered"] == 1
        pending = storage.get_pending_capture_batches()
        assert len(pending) == 1

        storage.update_capture_batch_status(
            pending[0].id, ChunkStatus.FAILED, error_message="timeout"
        )
        result = storage.recover_capture_analysis(root, max_retries=3)
        assert result["recovered"] == 1
        assert storage.get_pending_capture_batches()[0].id == pending[0].id
    finally:
        storage.close()


def test_failed_batch_cards_are_hidden_from_activity_queries(tmp_path):
    storage = StorageManager(tmp_path / "cards.db", use_pool=False)
    start = datetime(2026, 9, 21, 10, 0)
    failed_id = storage.create_batch(AnalysisBatch(
        start_time=start,
        end_time=start + timedelta(minutes=5),
        status=BatchStatus.FAILED,
    ))
    completed_id = storage.create_batch(AnalysisBatch(
        start_time=start,
        end_time=start + timedelta(minutes=5),
        status=BatchStatus.PENDING,
    ))
    card = ActivityCard(
        category="工作",
        title="测试",
        start_time=start,
        end_time=start + timedelta(minutes=1),
        active_duration_seconds=60,
    )
    try:
        storage.replace_cards_for_batch(failed_id, [card])
        storage.replace_cards_for_batch(completed_id, [card])
        storage.update_batch(completed_id, BatchStatus.COMPLETED, "[]")
        cards = storage.get_cards_for_date(start)
        assert len(cards) == 1
        assert cards[0].title == "测试"
    finally:
        storage.close()
