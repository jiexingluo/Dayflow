"""
Dayflow Windows - 卡片合并
把模型产出的碎片卡片（10~40 秒一段）整理成可读的活动段落。

合并只改动卡片的时间范围和明细，不改变“有效时长”总量：
每张卡片合并前的 active_duration_seconds 是真实录制覆盖时间，合并后取累加。
"""
import logging
from datetime import datetime
from typing import List, Optional, Tuple

import config
from core.types import ActivityCard, AppSite, Distraction

logger = logging.getLogger(__name__)

# 这些类别的短暂切换更适合记录为“分心”，而不是改写主活动标题。
DISTRACTION_CATEGORIES = {"Social", "Entertainment", "Break", "Other", "社交", "娱乐", "休息", "其他"}


def _naive(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    return value.replace(tzinfo=None) if value.tzinfo else value


def effective_seconds(card: ActivityCard) -> float:
    """卡片的有效时长：优先用录制覆盖时长，旧数据回退到起止时间。"""
    if card.active_duration_seconds is not None:
        return max(float(card.active_duration_seconds), 0.0)
    start, end = _naive(card.start_time), _naive(card.end_time)
    if start and end and end > start:
        return (end - start).total_seconds()
    return 0.0


def primary_app(card: ActivityCard) -> Optional[str]:
    if not card.app_sites:
        return None
    best = max(card.app_sites, key=lambda item: max(float(item.duration_seconds or 0), 0.0))
    name = (best.name or "").strip()
    return name or None


def _wall_gap_seconds(left: ActivityCard, right: ActivityCard) -> Optional[float]:
    left_end, right_start = _naive(left.end_time), _naive(right.start_time)
    if not left_end or not right_start:
        return None
    return (right_start - left_end).total_seconds()


def _merge_app_sites(host: ActivityCard, absorbed: ActivityCard) -> None:
    merged = {}
    for app in list(host.app_sites) + list(absorbed.app_sites):
        name = (app.name or "").strip() or "Unknown"
        entry = merged.get(name)
        if entry is None:
            merged[name] = AppSite(
                name=name,
                duration_seconds=max(float(app.duration_seconds or 0), 0.0),
                icon_url=app.icon_url,
            )
        else:
            entry.duration_seconds += max(float(app.duration_seconds or 0), 0.0)
            entry.icon_url = entry.icon_url or app.icon_url
    host.app_sites = sorted(merged.values(), key=lambda item: item.duration_seconds, reverse=True)


def merge_card(host: ActivityCard, absorbed: ActivityCard, as_distraction: bool = False) -> None:
    """把 absorbed 并入 host（就地修改 host）。"""
    host_seconds = effective_seconds(host)
    absorbed_seconds = effective_seconds(absorbed)
    total_seconds = host_seconds + absorbed_seconds

    start_candidates = [value for value in (_naive(host.start_time), _naive(absorbed.start_time)) if value]
    end_candidates = [value for value in (_naive(host.end_time), _naive(absorbed.end_time)) if value]
    if start_candidates:
        host.start_time = min(start_candidates)
    if end_candidates:
        host.end_time = max(end_candidates)

    if host.active_duration_seconds is not None or absorbed.active_duration_seconds is not None:
        host.active_duration_seconds = total_seconds

    _merge_app_sites(host, absorbed)
    host.distractions = list(host.distractions) + list(absorbed.distractions)
    if as_distraction and absorbed.title:
        timestamp = 0.0
        if absorbed.start_time and host.start_time:
            timestamp = max((_naive(absorbed.start_time) - _naive(host.start_time)).total_seconds(), 0.0)
        host.distractions.append(Distraction(
            description=absorbed.title,
            timestamp=timestamp,
            duration_seconds=absorbed_seconds,
        ))

    if total_seconds > 0:
        host.productivity_score = (
            float(host.productivity_score or 0) * host_seconds
            + float(absorbed.productivity_score or 0) * absorbed_seconds
        ) / total_seconds


def _should_record_as_distraction(host: ActivityCard, absorbed: ActivityCard) -> bool:
    return (
        absorbed.category in DISTRACTION_CATEGORIES
        and absorbed.category != host.category
    )


def can_merge(left: ActivityCard, right: ActivityCard, gap_seconds: float) -> bool:
    """同一活动的两段能否续接成一张卡。"""
    if left.category and right.category and left.category != right.category:
        return False
    gap = _wall_gap_seconds(left, right)
    if gap is None or gap > gap_seconds or gap < -gap_seconds:
        return False
    left_app, right_app = primary_app(left), primary_app(right)
    if left_app and right_app and left_app != right_app:
        return False
    return True


def _neighbors(cards: List[ActivityCard], index: int, gap_seconds: float):
    """返回与当前卡片相邻且间隔可接受的 (前一张, 后一张)。"""
    current = cards[index]
    current_start, current_end = _naive(current.start_time), _naive(current.end_time)
    if current_start is None or current_end is None:
        return None, None

    previous = cards[index - 1] if index > 0 else None
    following = cards[index + 1] if index + 1 < len(cards) else None

    if previous is not None:
        previous_end = _naive(previous.end_time)
        if previous_end is None or (current_start - previous_end).total_seconds() > gap_seconds:
            previous = None
    if following is not None:
        following_start = _naive(following.start_time)
        if following_start is None or (following_start - current_end).total_seconds() > gap_seconds:
            following = None

    return previous, following


def consolidate_cards(
    cards: List[ActivityCard],
    min_seconds: Optional[float] = None,
    gap_seconds: Optional[float] = None,
    drop_empty: bool = True,
) -> List[ActivityCard]:
    """把碎片卡片合并成连续活动。

    1. 有效时长不足 min_seconds 的卡片并入相邻卡片（优先更长、同类别的邻居）；
    2. 相邻且类别、主应用一致的卡片继续合并，避免跨批次断成两张。

    ``drop_empty`` 为 True 时丢弃没有任何录制证据的卡片；维护脚本处理历史
    数据时应传 False，避免删掉用户仍想看到的内容。
    """
    min_seconds = float(config.CARD_MIN_DURATION_SECONDS if min_seconds is None else min_seconds)
    gap_seconds = float(config.CARD_MERGE_GAP_SECONDS if gap_seconds is None else gap_seconds)
    if not cards:
        return []

    working = list(cards) if not drop_empty else [
        card for card in cards if effective_seconds(card) > 0
    ]
    working.sort(key=lambda card: _naive(card.start_time) or datetime.min)
    if not working:
        return []

    # 1. 短卡片并入相邻卡片。跳过无法合并的卡片，避免死循环。
    blocked = set()
    changed = True
    while changed:
        changed = False
        for index, card in enumerate(working):
            if id(card) in blocked or effective_seconds(card) >= min_seconds:
                continue
            previous, following = _neighbors(working, index, gap_seconds)
            candidates = [item for item in (previous, following) if item is not None]
            if not candidates:
                blocked.add(id(card))
                continue
            host = max(
                candidates,
                key=lambda item: (
                    item.category == card.category,
                    effective_seconds(item),
                ),
            )
            merge_card(host, card, as_distraction=_should_record_as_distraction(host, card))
            working.pop(index)
            changed = True
            break

    # 2. 同类别的相邻卡片续接。
    merged = True
    while merged:
        merged = False
        for index in range(len(working) - 1):
            left, right = working[index], working[index + 1]
            if not can_merge(left, right, gap_seconds):
                continue
            merge_card(left, right)
            working.pop(index + 1)
            merged = True
            break

    return working


def find_carry_target(
    cards: List[ActivityCard],
    previous_cards: List[ActivityCard],
    gap_seconds: Optional[float] = None,
) -> Tuple[Optional[ActivityCard], Optional[ActivityCard]]:
    """找出可以并入上一批次遗留卡片的当前批次首卡。"""
    gap_seconds = float(config.CARD_MERGE_GAP_SECONDS if gap_seconds is None else gap_seconds)
    if not cards or not previous_cards:
        return None, None
    first = cards[0]
    for previous in previous_cards:
        if previous.id is None:
            continue
        if previous.end_time is None or first.start_time is None:
            continue
        if can_merge(previous, first, gap_seconds):
            return first, previous
    return None, None
