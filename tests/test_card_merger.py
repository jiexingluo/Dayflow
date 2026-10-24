from datetime import datetime, timedelta

from core.card_merger import (
    can_merge,
    consolidate_cards,
    effective_seconds,
    find_carry_target,
    merge_card,
)
from core.types import ActivityCard, AppSite, Distraction


def card(start_minute, minutes, category="Programming", title="", apps=None,
         active_seconds=None, distractions=None):
    start = datetime(2026, 10, 24, 9, 0) + timedelta(minutes=start_minute)
    end = start + timedelta(minutes=minutes)
    return ActivityCard(
        category=category,
        title=title or f"{category} {start_minute}",
        start_time=start,
        end_time=end,
        app_sites=[AppSite(name=name, duration_seconds=seconds) for name, seconds in (apps or [])],
        distractions=distractions or [],
        productivity_score=80,
        active_duration_seconds=minutes * 60 if active_seconds is None else active_seconds,
    )


def test_short_card_is_absorbed_into_long_neighbor():
    long_card = card(0, 8, category="Programming", apps=[("Visual Studio Code", 480)])
    brief = card(2, 0.5, category="Social", title="查看群消息",
                 apps=[("WeChat", 30)], active_seconds=30)

    result = consolidate_cards([long_card, brief], min_seconds=120, gap_seconds=120)

    assert len(result) == 1
    merged = result[0]
    assert merged is long_card
    assert merged.active_duration_seconds == 510
    assert merged.start_time == datetime(2026, 10, 24, 9, 0)
    assert merged.end_time == datetime(2026, 10, 24, 9, 8)
    assert {app.name: app.duration_seconds for app in merged.app_sites} == {
        "Visual Studio Code": 480,
        "WeChat": 30,
    }
    # 短暂切换保留为分心记录
    assert merged.distractions[-1].description == "查看群消息"
    assert merged.distractions[-1].duration_seconds == 30


def test_short_same_category_card_is_folded_without_distraction_entry():
    host = card(0, 6, category="Work", apps=[("Microsoft Word", 360)])
    brief = card(3, 1, category="Work", title="查看目录", active_seconds=60)

    result = consolidate_cards([host, brief], min_seconds=120, gap_seconds=60)

    assert len(result) == 1
    assert result[0].distractions == []
    assert result[0].active_duration_seconds == 420


def test_adjacent_same_activity_cards_merge_across_batches():
    first = card(0, 10, category="Programming", apps=[("Cursor", 600)], active_seconds=600)
    second = card(10, 10, category="Programming", apps=[("Cursor", 540)],
                  active_seconds=540)

    result = consolidate_cards([first, second], min_seconds=120, gap_seconds=120)

    assert len(result) == 1
    assert result[0].active_duration_seconds == 1140
    assert result[0].end_time == datetime(2026, 10, 24, 9, 20)


def test_cards_without_recording_evidence_are_dropped():
    evidence = card(0, 5, active_seconds=300)
    invented = card(20, 5, active_seconds=0)

    result = consolidate_cards([evidence, invented], min_seconds=120)

    assert result == [evidence]


def test_unrelated_activities_are_not_merged():
    coding = card(0, 10, category="Programming", apps=[("Cursor", 600)])
    meeting = card(10, 10, category="Meeting", apps=[("Feishu", 600)])

    result = consolidate_cards([coding, meeting], min_seconds=120)

    assert len(result) == 2


def test_can_merge_requires_same_category_and_nearby_time():
    left = card(0, 10, category="Programming", apps=[("Cursor", 600)])
    same = card(10, 5, category="Programming", apps=[("Cursor", 300)])
    other_category = card(10, 5, category="Learning", apps=[("Cursor", 300)])
    far_away = card(40, 5, category="Programming", apps=[("Cursor", 300)])

    assert can_merge(left, same, gap_seconds=60)
    assert not can_merge(left, other_category, gap_seconds=60)
    assert not can_merge(left, far_away, gap_seconds=60)


def test_find_carry_target_links_first_card_to_previous_batch_card():
    previous = card(0, 10, category="Programming", apps=[("Cursor", 600)])
    previous.id = 42
    continuation = card(10, 5, category="Programming", apps=[("Cursor", 300)])
    unrelated = card(10, 5, category="Social", apps=[("WeChat", 300)])

    assert find_carry_target([continuation], [previous], gap_seconds=120) == (continuation, previous)
    assert find_carry_target([unrelated], [previous], gap_seconds=120) == (None, None)


def test_merge_card_keeps_productivity_weighted_by_time():
    host = card(0, 10, active_seconds=600)
    host.productivity_score = 90
    brief = card(10, 10, active_seconds=600)
    brief.productivity_score = 30

    merge_card(host, brief)

    assert host.productivity_score == 60
    assert effective_seconds(host) == 1200


def test_scheduler_merges_fragments_and_carries_continuation_across_batches(tmp_path):
    from core.analysis import AnalysisScheduler
    from core.types import AnalysisBatch, BatchStatus
    from database.storage import StorageManager

    storage = StorageManager(tmp_path / "carry.db", use_pool=False)
    scheduler = AnalysisScheduler(storage=storage, provider=object(), batch_duration_minutes=5)
    base = datetime(2026, 10, 24, 9, 0)
    try:
        first_batch = storage.create_batch(AnalysisBatch(
            start_time=base, end_time=base + timedelta(minutes=10),
            status=BatchStatus.PENDING, source_type="capture",
        ))
        first_cards = [
            card(0, 9, category="Programming", title="写测试", apps=[("Cursor", 540)]),
            card(9, 1, category="Social", title="看消息", apps=[("WeChat", 60)],
                 active_seconds=60),
        ]
        stored, carries = scheduler._persist_cards(first_batch, first_cards)
        storage.update_batch(first_batch, BatchStatus.COMPLETED)
        scheduler._apply_carry_merges(first_batch, carries)

        assert len(stored) == 1  # 1 分钟的看消息被并入写测试
        coding_card = storage.get_cards_for_date(base)[0]
        assert coding_card.active_duration_seconds == 600

        second_batch = storage.create_batch(AnalysisBatch(
            start_time=base + timedelta(minutes=10),
            end_time=base + timedelta(minutes=15),
            status=BatchStatus.PENDING, source_type="capture",
        ))
        second_cards = [
            card(10, 5, category="Programming", title="继续写测试",
                 apps=[("Cursor", 300)]),
        ]
        stored, carries = scheduler._persist_cards(second_batch, second_cards)
        storage.update_batch(second_batch, BatchStatus.COMPLETED)
        scheduler._apply_carry_merges(second_batch, carries)

        assert stored == []  # 整批都续接到上一张卡片，不再单独入库
        assert len(carries) == 1
        coding_card = storage.get_cards_for_date(base)[0]
        assert coding_card.end_time == base + timedelta(minutes=15)
        assert coding_card.active_duration_seconds == 900
        assert len(storage.get_cards_for_date(base)) == 1
    finally:
        storage.close()
