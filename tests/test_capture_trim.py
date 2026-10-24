import json
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

from core.recorder import ScreenshotRecorder
from core.types import CaptureBatch


class _FakeWindow:
    app_name = "code.exe"
    window_title = "dayflow - Visual Studio Code"
    process_id = 1


class _FakeTracker:
    def get_active_window(self):
        return _FakeWindow()

    def get_friendly_app_name(self, info):
        return "Visual Studio Code"


def _frame():
    return np.zeros((720, 1280, 3), dtype=np.uint8)


def _recorder(tmp_path, saved: list):
    recorder = ScreenshotRecorder(
        interval_seconds=10,
        batch_duration_minutes=5,
        output_dir=tmp_path / "captures",
        on_batch_saved=saved.append,
    )
    recorder._window_tracker = _FakeTracker()
    recorder._recording = True
    return recorder


def test_idle_pause_drops_idle_screenshots_and_seals_batch(tmp_path):
    saved: list[CaptureBatch] = []
    recorder = _recorder(tmp_path, saved)
    base = datetime(2026, 10, 24, 10, 0, 0)

    for offset in (0, 10, 20, 30):
        recorder._store_capture(_frame(), base + timedelta(seconds=offset))
    # 用户已经离开，但空闲检测要到 15 分钟后才触发，这段时间不应计入
    for offset in (40, 120, 290):
        recorder._store_capture(_frame(), base + timedelta(seconds=offset))

    recorder.pause(trim_end=base + timedelta(seconds=30))

    assert len(saved) == 1
    batch = saved[0]
    assert batch.start_time == base
    assert batch.end_time == base + timedelta(seconds=30)
    assert batch.image_count == 4
    manifest = json.loads(Path(batch.manifest_path).read_text(encoding="utf-8"))
    assert [item["relative_seconds"] for item in manifest["images"]] == [0, 10, 20, 30]
    # 空闲截图已从磁盘删除
    assert len(list(Path(batch.directory_path).glob("*.jpg"))) == 4


def test_stale_batch_directory_is_not_reused(tmp_path):
    saved: list[CaptureBatch] = []
    recorder = _recorder(tmp_path, saved)
    base = datetime(2026, 10, 24, 10, 0, 0)

    for offset in (0, 10):
        recorder._store_capture(_frame(), base + timedelta(seconds=offset))
    recorder.pause(trim_end=base + timedelta(seconds=10))
    first_dir = Path(saved[0].directory_path)

    # 恢复后仍落在同一个 5 分钟窗口，必须写入新目录，避免复用已入库的路径
    recorder.resume()
    for offset in (180, 190):
        recorder._store_capture(_frame(), base + timedelta(seconds=offset))
    recorder.pause(trim_end=base + timedelta(seconds=190))

    assert len(saved) == 2
    assert Path(saved[1].directory_path) != first_dir
    assert first_dir.exists()


def test_batch_is_sealed_when_window_ends(tmp_path):
    saved: list[CaptureBatch] = []
    recorder = _recorder(tmp_path, saved)
    base = datetime(2026, 10, 24, 10, 0, 0)

    for offset in (0, 10, 295, 305):
        recorder._store_capture(_frame(), base + timedelta(seconds=offset))
    recorder._finalize_capture_batch()

    assert len(saved) == 2
    assert saved[0].image_count == 3
    assert saved[1].image_count == 1
    assert saved[1].start_time == base + timedelta(seconds=305)


def test_empty_batch_after_trim_is_discarded(tmp_path):
    saved: list[CaptureBatch] = []
    recorder = _recorder(tmp_path, saved)
    base = datetime(2026, 10, 24, 10, 0, 0)

    recorder._store_capture(_frame(), base + timedelta(seconds=60))
    recorder.pause(trim_end=base)

    assert saved == []
    assert not any((tmp_path / "captures").rglob("*.jpg"))
