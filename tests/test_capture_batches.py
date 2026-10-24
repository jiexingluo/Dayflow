import json
from datetime import datetime, timedelta, timezone

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


def test_apply_card_merges_extends_previous_card(tmp_path):
    from core.types import AppSite

    storage = StorageManager(tmp_path / "carry.db", use_pool=False)
    start = datetime(2026, 10, 24, 10, 0)
    try:
        previous_batch = storage.create_batch(AnalysisBatch(
            start_time=start,
            end_time=start + timedelta(minutes=5),
            status=BatchStatus.COMPLETED,
            source_type="capture",
        ))
        host_id = storage.replace_cards_for_batch(previous_batch, [ActivityCard(
            category="Programming",
            title="实现卡片续接",
            start_time=start,
            end_time=start + timedelta(minutes=5),
            app_sites=[AppSite(name="Cursor", duration_seconds=300)],
            active_duration_seconds=300,
        )])[0]

        storage.replace_cards_for_batch(previous_batch + 1, [])
        storage.apply_card_merges([(host_id, ActivityCard(
            category="Programming",
            title="实现卡片续接",
            start_time=start + timedelta(minutes=5),
            end_time=start + timedelta(minutes=8),
            app_sites=[AppSite(name="Cursor", duration_seconds=180)],
            active_duration_seconds=180,
        ))])

        cards = storage.get_cards_for_date(start)
        assert len(cards) == 1
        assert cards[0].end_time == start + timedelta(minutes=8)
        assert cards[0].active_duration_seconds == 480
        assert cards[0].app_sites[0].duration_seconds == 480
    finally:
        storage.close()


def test_exhausted_failed_batch_is_retried_after_cooldown(tmp_path):
    storage = StorageManager(tmp_path / "slow-retry.db", use_pool=False)
    root = tmp_path / "captures"
    directory = root / "2026-09-21" / "10-00"
    directory.mkdir(parents=True)
    (directory / "capture.jpg").write_bytes(b"jpeg")
    captured_at = datetime.now() - timedelta(hours=1)
    (directory / "manifest.json").write_text(json.dumps({
        "window_start": captured_at.isoformat(),
        "window_end": (captured_at + timedelta(minutes=5)).isoformat(),
        "interval_seconds": 10,
        "images": [{
            "file": "capture.jpg",
            "timestamp": captured_at.isoformat(),
            "relative_seconds": 0,
        }],
    }), encoding="utf-8")

    try:
        storage.recover_capture_analysis(root, max_retries=3)
        batch_id = storage.get_pending_capture_batches()[0].id
        for _ in range(4):
            storage.update_capture_batch_status(
                batch_id, ChunkStatus.FAILED, error_message="503 Service Unavailable"
            )

        # 快速重试次数用完后，冷却期内不再重试
        assert storage.requeue_capture_if_retryable(batch_id, max_retries=3) is False
        assert storage.recover_capture_analysis(root, max_retries=3)["recovered"] == 0

        # 超过冷却时间后重新排队，并重置重试预算
        stale = (datetime.now(timezone.utc) - timedelta(hours=7)).replace(tzinfo=None).isoformat(sep=" ")
        with storage._get_connection() as conn:
            conn.execute(
                "UPDATE capture_batches SET last_attempt_at = ? WHERE id = ?",
                (stale, batch_id),
            )

        assert storage.requeue_capture_if_retryable(batch_id, max_retries=3) is True
        pending = storage.get_pending_capture_batches()
        assert [batch.id for batch in pending] == [batch_id]
        with storage._get_connection() as conn:
            assert conn.execute(
                "SELECT retry_count FROM capture_batches WHERE id = ?", (batch_id,)
            ).fetchone()[0] == 0
    finally:
        storage.close()


def test_cleanup_orphan_cards_removes_cards_of_failed_batches(tmp_path):
    storage = StorageManager(tmp_path / "orphan-cards.db", use_pool=False)
    start = datetime(2026, 9, 21, 10, 0)
    failed_id = storage.create_batch(AnalysisBatch(
        start_time=start, end_time=start + timedelta(minutes=5), status=BatchStatus.FAILED,
    ))
    completed_id = storage.create_batch(AnalysisBatch(
        start_time=start, end_time=start + timedelta(minutes=5), status=BatchStatus.PENDING,
    ))
    card = ActivityCard(
        category="Work", title="测试", start_time=start,
        end_time=start + timedelta(minutes=1), active_duration_seconds=60,
    )
    try:
        storage.replace_cards_for_batch(failed_id, [card])
        storage.replace_cards_for_batch(completed_id, [card])
        storage.update_batch(completed_id, BatchStatus.COMPLETED, "[]")

        assert storage.cleanup_orphan_cards() == 1
        with storage._get_connection() as conn:
            remaining = conn.execute("SELECT batch_id FROM timeline_cards").fetchall()
        assert [row[0] for row in remaining] == [completed_id]
        assert storage.cleanup_orphan_cards() == 0
    finally:
        storage.close()


def _register_capture(storage, tmp_path, name="10-00"):
    root = tmp_path / "captures"
    directory = root / "2026-09-21" / name
    directory.mkdir(parents=True)
    (directory / "capture.jpg").write_bytes(b"jpeg")
    captured_at = datetime.now() - timedelta(hours=1)
    (directory / "manifest.json").write_text(json.dumps({
        "window_start": captured_at.isoformat(),
        "window_end": (captured_at + timedelta(minutes=5)).isoformat(),
        "interval_seconds": 10,
        "images": [{
            "file": "capture.jpg",
            "timestamp": captured_at.isoformat(),
            "relative_seconds": 0,
        }],
    }), encoding="utf-8")
    storage.recover_capture_analysis(root, max_retries=3)
    return storage.get_pending_capture_batches()[0].id


def _retry_count(storage, batch_id):
    with storage._get_connection() as conn:
        return conn.execute(
            "SELECT retry_count FROM capture_batches WHERE id = ?", (batch_id,)
        ).fetchone()[0]


def test_fast_retry_budget_is_not_reset_between_attempts(tmp_path):
    storage = StorageManager(tmp_path / "fast-retry.db", use_pool=False)
    try:
        batch_id = _register_capture(storage, tmp_path)

        for expected in (1, 2):
            storage.update_capture_batch_status(batch_id, ChunkStatus.FAILED, error_message="503")
            assert storage.requeue_capture_if_retryable(batch_id, max_retries=3) is True
            assert _retry_count(storage, batch_id) == expected

        storage.update_capture_batch_status(batch_id, ChunkStatus.FAILED, error_message="503")
        assert storage.requeue_capture_if_retryable(batch_id, max_retries=3) is False
        assert _retry_count(storage, batch_id) == 3
    finally:
        storage.close()


def test_stale_failed_captures_are_requeued_without_restart(tmp_path):
    storage = StorageManager(tmp_path / "stale-retry.db", use_pool=False)
    try:
        batch_id = _register_capture(storage, tmp_path)
        for _ in range(3):
            storage.update_capture_batch_status(batch_id, ChunkStatus.FAILED, error_message="503")

        # 冷却未到，扫描时不会重复打接口
        assert storage.requeue_stale_failed_captures(max_retries=3) == 0

        stale = (datetime.now(timezone.utc) - timedelta(hours=7)).replace(tzinfo=None)
        with storage._get_connection() as conn:
            conn.execute(
                "UPDATE capture_batches SET last_attempt_at = ? WHERE id = ?",
                (stale.isoformat(sep=" "), batch_id),
            )

        assert storage.requeue_stale_failed_captures(max_retries=3) == 1
        assert _retry_count(storage, batch_id) == 0
        assert [b.id for b in storage.get_pending_capture_batches()] == [batch_id]
    finally:
        storage.close()
