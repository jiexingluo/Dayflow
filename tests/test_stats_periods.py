import os
from datetime import date, datetime, timedelta

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QDate
from PySide6.QtWidgets import QApplication

from core.types import ActivityCard
from database.storage import StorageManager
from ui.stats_view import StatsPanel, get_stats_date_range, iter_period_buckets
from ui.timeline_view import TimelineHeader


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.mark.parametrize(
    ("range_type", "today", "expected"),
    [
        (
            "week",
            date(2026, 8, 5),
            (date(2026, 8, 3), date(2026, 8, 5), date(2026, 7, 27), date(2026, 7, 29)),
        ),
        (
            "month",
            date(2026, 8, 9),
            (date(2026, 8, 1), date(2026, 8, 9), date(2026, 7, 1), date(2026, 7, 9)),
        ),
        (
            "quarter",
            date(2026, 8, 9),
            (date(2026, 7, 1), date(2026, 8, 9), date(2026, 4, 1), date(2026, 5, 10)),
        ),
        (
            "quarter",
            date(2026, 1, 15),
            (date(2026, 1, 1), date(2026, 1, 15), date(2025, 10, 1), date(2025, 10, 15)),
        ),
    ],
)
def test_natural_stats_ranges_compare_matching_elapsed_time(range_type, today, expected):
    period = get_stats_date_range(range_type, today)
    assert (period.start, period.end, period.previous_start, period.previous_end) == expected


def test_quarter_buckets_are_clipped_to_natural_weeks():
    period = get_stats_date_range("quarter", date(2026, 8, 9))
    buckets = iter_period_buckets(period, "quarter")

    assert buckets[0] == (date(2026, 7, 1), date(2026, 7, 5))
    assert buckets[1] == (date(2026, 7, 6), date(2026, 7, 12))
    assert buckets[-1] == (date(2026, 8, 3), date(2026, 8, 9))
    assert all(end - start <= timedelta(days=6) for start, end in buckets)


def test_storage_get_cards_for_range_is_inclusive(tmp_path):
    storage = StorageManager(tmp_path / "stats-range.db", use_pool=False)
    try:
        for day in (date(2026, 7, 31), date(2026, 8, 1), date(2026, 8, 9), date(2026, 8, 10)):
            start = datetime.combine(day, datetime.min.time()).replace(hour=12)
            storage.save_card(
                ActivityCard(
                    title=day.isoformat(),
                    start_time=start,
                    end_time=start + timedelta(minutes=30),
                )
            )

        cards = storage.get_cards_for_range(date(2026, 8, 1), date(2026, 8, 9))
        assert [card.title for card in cards] == ["2026-08-01", "2026-08-09"]
    finally:
        storage.close()


def test_stats_range_buttons_are_exclusive_and_update_copy(tmp_path, qapp):
    storage = StorageManager(tmp_path / "stats-panel.db", use_pool=False)
    try:
        panel = StatsPanel(storage)
        panel._set_range("quarter")

        assert panel.quarter_btn.isChecked()
        assert not panel.week_btn.isChecked()
        assert not panel.month_btn.isChecked()
        assert panel.app_section_title.text() == "本季度应用 / 网站使用"
        panel.deleteLater()
        qapp.processEvents()
    finally:
        storage.close()


def test_timeline_calendar_selects_history_and_rejects_future(qapp):
    header = TimelineHeader()
    selected = []
    header.date_changed.connect(selected.append)

    nav_layout = header.layout().itemAt(0).layout()
    assert nav_layout.indexOf(header.today_btn) < nav_layout.indexOf(header.calendar_btn)

    historical = QDate(2025, 12, 31)
    header._on_calendar_date_selected(historical)

    assert selected[-1].date() == historical.toPython()
    assert header.calendar.maximumDate() == QDate.currentDate()
    assert "2025年12月31日" in header.date_label.text()

    count = len(selected)
    header._on_calendar_date_selected(QDate.currentDate().addDays(1))
    assert len(selected) == count

    header.set_date(datetime.now())
    assert not header.next_btn.isEnabled()
    header.deleteLater()
    qapp.processEvents()
