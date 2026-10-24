"""把历史时间轴卡片按新的卡片粒度规则重排。

只做合并与重新分配时长，不改变每天的有效总时长。

用法::

    python tools/consolidate_cards.py --days 7
    python tools/consolidate_cards.py --start 2026-10-19 --end 2026-10-24
    python tools/consolidate_cards.py --days 7 --dry-run

运行前会自动把数据库备份到数据目录。
"""
import argparse
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config
from core.card_merger import consolidate_cards, effective_seconds
from database.storage import StorageManager


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--days", type=int, default=7, help="处理最近 N 天（默认 7）")
    parser.add_argument("--start", type=str, help="起始日期 YYYY-MM-DD")
    parser.add_argument("--end", type=str, help="结束日期 YYYY-MM-DD（含）")
    parser.add_argument("--db", type=str, default=None, help="数据库路径（默认使用应用数据目录）")
    parser.add_argument("--dry-run", action="store_true", help="只打印结果，不写数据库")
    return parser.parse_args()


def resolve_range(args):
    if args.start:
        start = date.fromisoformat(args.start)
        end = date.fromisoformat(args.end) if args.end else date.today()
    else:
        end = date.today()
        start = end - timedelta(days=max(args.days - 1, 0))
    if end < start:
        raise SystemExit("结束日期早于起始日期")
    return start, end


def backup_database(db_path: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = db_path.with_name(f"{db_path.stem}_before_consolidation_{stamp}.db")
    with sqlite3.connect(db_path) as source, sqlite3.connect(backup_path) as target:
        source.backup(target)
    return backup_path


def iter_days(start: date, end: date):
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)


def main():
    args = parse_args()
    start, end = resolve_range(args)
    db_path = Path(args.db) if args.db else config.DATABASE_PATH
    backup_path = None
    if not args.dry_run:
        backup_path = backup_database(db_path)
        print(f"数据库已备份: {backup_path}\n")

    storage = StorageManager(db_path)
    try:
        total_before = total_after = 0
        removed = 0
        for day in iter_days(start, end):
            cards = storage.get_cards_for_date(datetime.combine(day, datetime.min.time()))
            if len(cards) < 2:
                continue
            before_seconds = sum(effective_seconds(card) for card in cards)
            consolidated = consolidate_cards(cards, drop_empty=False)
            after_seconds = sum(effective_seconds(card) for card in consolidated)
            surviving_ids = {card.id for card in consolidated if card.id is not None}
            delete_ids = [card.id for card in cards if card.id is not None and card.id not in surviving_ids]

            print(
                f"{day}: {len(cards)} -> {len(consolidated)} 张卡片，"
                f"有效时长 {before_seconds / 3600:.2f}h -> {after_seconds / 3600:.2f}h"
            )
            total_before += before_seconds
            total_after += after_seconds
            removed += len(delete_ids)
            if not args.dry_run:
                storage.save_consolidated_cards(consolidated, delete_ids)

        print(
            f"\n合计: 有效时长 {total_before / 3600:.2f}h -> {total_after / 3600:.2f}h，"
            f"删除 {removed} 张被合并的卡片"
        )
        if args.dry_run:
            print("dry-run：未写入数据库")
            return
        print(f"数据库备份: {backup_path}")
    finally:
        storage.close()


if __name__ == "__main__":
    main()
