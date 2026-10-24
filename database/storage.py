"""
Dayflow Windows - 数据库管理
"""
import sqlite3
import json
import logging
import threading
from pathlib import Path
from datetime import date, datetime, timedelta, timezone
from typing import List, Optional
from contextlib import contextmanager

import config
from core.types import (
    VideoChunk, ChunkStatus,
    CaptureBatch,
    AnalysisBatch, BatchStatus,
    ActivityCard, AppSite, Distraction
)
from core.card_merger import merge_card
from database.connection_pool import ConnectionPool, PoolExhaustedError

logger = logging.getLogger(__name__)


class StorageManager:
    """SQLite 数据库管理器 - 使用连接池"""
    
    def __init__(self, db_path: Optional[Path] = None, use_pool: bool = True):
        """
        初始化数据库管理器
        
        Args:
            db_path: 数据库文件路径
            use_pool: 是否使用连接池（默认 True）
        """
        self.db_path = db_path or config.DATABASE_PATH
        self._use_pool = use_pool
        self._pool: Optional[ConnectionPool] = None
        self._local = threading.local()  # 线程本地存储（兼容模式）
        
        logger.info(f"数据库路径: {self.db_path}")
        
        if use_pool:
            self._pool = ConnectionPool(
                db_path=str(self.db_path),
                max_size=5,
                timeout=30.0,
                idle_timeout=300.0
            )
        
        self._init_database()
    
    def _init_database(self):
        """初始化数据库结构"""
        schema_path = Path(__file__).parent / "schema.sql"
        with self._get_connection() as conn:
            with open(schema_path, "r", encoding="utf-8") as f:
                conn.executescript(f.read())
            
            # 数据库迁移：为旧数据库添加新字段
            self._migrate_database(conn)
    
    def _migrate_database(self, conn):
        """数据库迁移 - 添加新字段"""
        try:
            # 检查 chunks 表是否有 window_records_path 字段
            cursor = conn.execute("PRAGMA table_info(chunks)")
            columns = [row[1] for row in cursor.fetchall()]
            
            if "window_records_path" not in columns:
                conn.execute("ALTER TABLE chunks ADD COLUMN window_records_path TEXT")
                logger.info("数据库迁移: 添加 chunks.window_records_path 字段")

            batch_columns = [row[1] for row in conn.execute("PRAGMA table_info(analysis_batches)").fetchall()]
            if "source_type" not in batch_columns:
                conn.execute("ALTER TABLE analysis_batches ADD COLUMN source_type TEXT NOT NULL DEFAULT 'video'")
            if "source_ids" not in batch_columns:
                conn.execute("ALTER TABLE analysis_batches ADD COLUMN source_ids TEXT NOT NULL DEFAULT '[]'")

            cursor = conn.execute("PRAGMA table_info(timeline_cards)")
            card_columns = [row[1] for row in cursor.fetchall()]
            if "active_duration_seconds" not in card_columns:
                conn.execute("ALTER TABLE timeline_cards ADD COLUMN active_duration_seconds REAL")
                logger.info("数据库迁移: 添加 timeline_cards.active_duration_seconds 字段")

            capture_columns = [row[1] for row in conn.execute("PRAGMA table_info(capture_batches)").fetchall()]
            if "retry_count" not in capture_columns:
                conn.execute("ALTER TABLE capture_batches ADD COLUMN retry_count INTEGER NOT NULL DEFAULT 0")
                logger.info("数据库迁移: 添加 capture_batches.retry_count 字段")
            if "last_attempt_at" not in capture_columns:
                conn.execute("ALTER TABLE capture_batches ADD COLUMN last_attempt_at TIMESTAMP")
                logger.info("数据库迁移: 添加 capture_batches.last_attempt_at 字段")
        except Exception as e:
            logger.debug(f"数据库迁移检查: {e}")

    def save_capture_batch(self, batch: CaptureBatch) -> int:
        """保存一个已经封存的截图批次。"""
        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                INSERT INTO capture_batches
                    (directory_path, manifest_path, start_time, end_time,
                     duration_seconds, image_count, status, analysis_batch_id,
                     error_message, sealed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(directory_path) DO UPDATE SET
                    manifest_path = excluded.manifest_path,
                    start_time = excluded.start_time,
                    end_time = excluded.end_time,
                    duration_seconds = excluded.duration_seconds,
                    image_count = excluded.image_count,
                    status = excluded.status,
                    error_message = excluded.error_message,
                    sealed_at = excluded.sealed_at
                """,
                (
                    batch.directory_path,
                    batch.manifest_path,
                    batch.start_time.isoformat() if batch.start_time else None,
                    batch.end_time.isoformat() if batch.end_time else None,
                    batch.duration_seconds,
                    batch.image_count,
                    batch.status.value,
                    batch.analysis_batch_id,
                    batch.error_message,
                ),
            )
            row = conn.execute(
                "SELECT id FROM capture_batches WHERE directory_path = ?",
                (batch.directory_path,),
            ).fetchone()
            return row[0] if row else cursor.lastrowid

    def get_pending_capture_batches(self, limit: int = 20) -> List[CaptureBatch]:
        """获取已封存且等待分析的截图批次。"""
        with self._get_connection() as conn:
            rows = conn.execute(
                """
                SELECT * FROM capture_batches
                WHERE status = ?
                ORDER BY start_time ASC
                LIMIT ?
                """,
                (ChunkStatus.PENDING.value, limit),
            ).fetchall()
            return [self._row_to_capture_batch(row) for row in rows]

    def update_capture_batch_status(
        self,
        batch_id: int,
        status: ChunkStatus,
        analysis_batch_id: Optional[int] = None,
        error_message: Optional[str] = None,
    ) -> None:
        with self._get_connection() as conn:
            if status == ChunkStatus.FAILED:
                conn.execute(
                    """
                    UPDATE capture_batches
                    SET status = ?, analysis_batch_id = COALESCE(?, analysis_batch_id),
                        error_message = ?, retry_count = retry_count + 1,
                        last_attempt_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (status.value, analysis_batch_id, error_message, batch_id),
                )
            else:
                conn.execute(
                    """
                    UPDATE capture_batches
                    SET status = ?, analysis_batch_id = COALESCE(?, analysis_batch_id),
                        error_message = ?,
                        last_attempt_at = CASE WHEN ? = 'processing' THEN CURRENT_TIMESTAMP ELSE last_attempt_at END
                    WHERE id = ?
                    """,
                    (status.value, analysis_batch_id, error_message, status.value, batch_id),
                )

    def recover_capture_analysis(
        self,
        capture_root: Path,
        max_retries: int = 3,
        slow_retry_seconds: Optional[int] = None,
    ) -> dict:
        """Register sealed manifests and requeue recoverable interrupted screenshot analysis."""
        capture_root = Path(capture_root)
        now = datetime.now()
        slow_retry_seconds = (
            config.CAPTURE_SLOW_RETRY_SECONDS if slow_retry_seconds is None else slow_retry_seconds
        )
        registered = 0

        with self._get_connection() as conn:
            known_manifests = {
                str(Path(row[0]).resolve()).lower()
                for row in conn.execute("SELECT manifest_path FROM capture_batches")
            }

        if capture_root.exists():
            for manifest_path in capture_root.rglob("manifest.json"):
                normalized = str(manifest_path.resolve()).lower()
                if normalized in known_manifests:
                    continue
                try:
                    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
                    window_end = datetime.fromisoformat(payload["window_end"])
                    if window_end > now:
                        continue
                    interval = max(1, int(payload.get("interval_seconds", 10)))
                    images = [
                        item for item in payload.get("images", [])
                        if item.get("timestamp") and (manifest_path.parent / item.get("file", "")).is_file()
                    ]
                    if not images:
                        continue
                    start_time = datetime.fromisoformat(images[0]["timestamp"])
                    last_time = datetime.fromisoformat(images[-1]["timestamp"])
                    end_time = min(window_end, last_time + timedelta(seconds=interval))
                    self.save_capture_batch(CaptureBatch(
                        directory_path=str(manifest_path.parent),
                        manifest_path=str(manifest_path),
                        start_time=start_time,
                        end_time=end_time,
                        duration_seconds=max(0, (end_time - start_time).total_seconds()),
                        image_count=len(images),
                        status=ChunkStatus.PENDING,
                    ))
                    registered += 1
                    known_manifests.add(normalized)
                except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
                    logger.warning("跳过无法恢复的截图 manifest %s: %s", manifest_path, exc)

        recovered = 0
        deferred = 0
        orphaned = 0
        with self._get_connection() as conn:
            rows = conn.execute(
                """
                SELECT id, status, retry_count, last_attempt_at, directory_path,
                       manifest_path, analysis_batch_id
                FROM capture_batches
                WHERE status IN (?, ?)
                """,
                (ChunkStatus.PROCESSING.value, ChunkStatus.FAILED.value),
            ).fetchall()
            for row in rows:
                if not Path(row["directory_path"]).is_dir() or not Path(row["manifest_path"]).is_file():
                    conn.execute(
                        "UPDATE capture_batches SET status = ? WHERE id = ?",
                        (ChunkStatus.ORPHANED.value, row["id"]),
                    )
                    orphaned += 1
                    continue
                exhausted = int(row["retry_count"] or 0) >= max_retries
                last_attempt = self._db_timestamp_to_local(row["last_attempt_at"])
                can_retry = (
                    row["status"] == ChunkStatus.PROCESSING.value
                    or not exhausted
                    or last_attempt is None
                    or (now - last_attempt).total_seconds() >= slow_retry_seconds
                )
                if not can_retry:
                    deferred += 1
                    continue
                if row["analysis_batch_id"]:
                    conn.execute(
                        """
                        UPDATE analysis_batches
                        SET status = ?, error_message = ?, completed_at = CURRENT_TIMESTAMP
                        WHERE id = ? AND status != ?
                        """,
                        (
                            BatchStatus.FAILED.value,
                            "Application exited or screenshot analysis scheduled for retry",
                            row["analysis_batch_id"],
                            BatchStatus.COMPLETED.value,
                        ),
                    )
                conn.execute(
                    """
                    UPDATE capture_batches
                    SET status = ?, analysis_batch_id = NULL, error_message = NULL,
                        retry_count = CASE WHEN ? = 1 THEN 0 ELSE retry_count END
                    WHERE id = ?
                    """,
                    (ChunkStatus.PENDING.value, 1 if exhausted else 0, row["id"]),
                )
                recovered += 1

        result = {
            "registered": registered,
            "recovered": recovered,
            "deferred": deferred,
            "orphaned": orphaned,
        }
        if any(result.values()):
            logger.info("截图分析恢复完成: %s", result)
        return result

    def requeue_capture_if_retryable(
        self,
        batch_id: int,
        max_retries: int = 3,
        slow_retry_seconds: Optional[int] = None,
    ) -> bool:
        """Move a failed screenshot batch back to pending while its retry budget remains.

        When the fast retry budget is exhausted the batch gets one more chance after
        ``slow_retry_seconds``, so a temporary proxy outage does not lose the footage.
        """
        slow_retry_seconds = (
            config.CAPTURE_SLOW_RETRY_SECONDS if slow_retry_seconds is None else slow_retry_seconds
        )
        with self._get_connection() as conn:
            row = conn.execute(
                """
                SELECT retry_count, last_attempt_at, directory_path, manifest_path
                FROM capture_batches WHERE id = ?
                """,
                (batch_id,),
            ).fetchone()
            if not row:
                return False
            if not Path(row["directory_path"]).is_dir() or not Path(row["manifest_path"]).is_file():
                return False
            exhausted = int(row["retry_count"] or 0) >= max_retries
            if exhausted:
                last_attempt = self._db_timestamp_to_local(row["last_attempt_at"])
                if last_attempt is not None and (
                    datetime.now() - last_attempt
                ).total_seconds() < slow_retry_seconds:
                    return False
            conn.execute(
                """
                UPDATE capture_batches
                SET status = ?, analysis_batch_id = NULL,
                    retry_count = CASE WHEN ? = 1 THEN 0 ELSE retry_count END
                WHERE id = ?
                """,
                (ChunkStatus.PENDING.value, 1 if exhausted else 0, batch_id),
            )
            return True

    def requeue_stale_failed_captures(
        self,
        max_retries: int = 3,
        slow_retry_seconds: Optional[int] = None,
    ) -> int:
        """慢速重试：快速重试次数用尽且已过冷却期的批次重新入队。"""
        slow_retry_seconds = (
            config.CAPTURE_SLOW_RETRY_SECONDS if slow_retry_seconds is None else slow_retry_seconds
        )
        now = datetime.now()
        with self._get_connection() as conn:
            rows = conn.execute(
                """
                SELECT id, retry_count, last_attempt_at, directory_path, manifest_path
                FROM capture_batches WHERE status = ? AND retry_count >= ?
                """,
                (ChunkStatus.FAILED.value, max_retries),
            ).fetchall()
            requeued = 0
            for row in rows:
                if not Path(row["directory_path"]).is_dir() or not Path(row["manifest_path"]).is_file():
                    continue
                last_attempt = self._db_timestamp_to_local(row["last_attempt_at"])
                if last_attempt is not None and (now - last_attempt).total_seconds() < slow_retry_seconds:
                    continue
                conn.execute(
                    """
                    UPDATE capture_batches
                    SET status = ?, analysis_batch_id = NULL, retry_count = 0
                    WHERE id = ?
                    """,
                    (ChunkStatus.PENDING.value, row["id"]),
                )
                requeued += 1
        if requeued:
            logger.info("慢速重试重新入队 %s 个截图批次", requeued)
        return requeued

    @staticmethod
    def _db_timestamp_to_local(value) -> Optional[datetime]:
        """SQLite CURRENT_TIMESTAMP 是 UTC，转成和其他字段一致的本地时间。"""
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(str(value))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone().replace(tzinfo=None)

    def cleanup_orphan_cards(self) -> int:
        """删除挂在非 completed 批次下的卡片，避免它们残留在数据库里。"""
        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                DELETE FROM timeline_cards
                WHERE batch_id IS NOT NULL
                  AND batch_id NOT IN (
                      SELECT id FROM analysis_batches WHERE status = ?
                  )
                """,
                (BatchStatus.COMPLETED.value,),
            )
            deleted = cursor.rowcount or 0
        if deleted:
            logger.info("已清理 %s 张挂在失败批次下的卡片", deleted)
        return deleted

    def _row_to_capture_batch(self, row: sqlite3.Row) -> CaptureBatch:
        return CaptureBatch(
            id=row["id"],
            directory_path=row["directory_path"],
            manifest_path=row["manifest_path"],
            start_time=datetime.fromisoformat(row["start_time"]),
            end_time=datetime.fromisoformat(row["end_time"]),
            duration_seconds=row["duration_seconds"],
            image_count=row["image_count"],
            status=ChunkStatus(row["status"]),
            analysis_batch_id=row["analysis_batch_id"],
            error_message=row["error_message"],
        )

    def reconcile_missing_inputs(self) -> dict:
        """阻止已丢失源文件的历史任务反复进入分析队列。"""
        orphaned_chunks = 0
        orphaned_captures = 0
        with self._get_connection() as conn:
            rows = conn.execute(
                "SELECT id, file_path FROM chunks WHERE status IN (?, ?, ?)",
                (ChunkStatus.PENDING.value, ChunkStatus.PROCESSING.value, ChunkStatus.FAILED.value),
            ).fetchall()
            for row in rows:
                if not Path(row["file_path"]).exists():
                    conn.execute(
                        "UPDATE chunks SET status = ?, batch_id = NULL WHERE id = ?",
                        (ChunkStatus.ORPHANED.value, row["id"]),
                    )
                    orphaned_chunks += 1

            rows = conn.execute(
                "SELECT id, directory_path, manifest_path FROM capture_batches WHERE status IN (?, ?, ?)",
                (ChunkStatus.PENDING.value, ChunkStatus.PROCESSING.value, ChunkStatus.FAILED.value),
            ).fetchall()
            for row in rows:
                if not Path(row["directory_path"]).is_dir() or not Path(row["manifest_path"]).exists():
                    conn.execute(
                        "UPDATE capture_batches SET status = ? WHERE id = ?",
                        (ChunkStatus.ORPHANED.value, row["id"]),
                    )
                    orphaned_captures += 1
        return {"chunks": orphaned_chunks, "capture_batches": orphaned_captures}
    
    def _get_cached_connection(self):
        """获取线程本地的缓存连接（兼容模式）"""
        if not hasattr(self._local, 'conn') or self._local.conn is None:
            self._local.conn = sqlite3.connect(
                str(self.db_path),
                check_same_thread=False,
                timeout=30.0
            )
            self._local.conn.row_factory = sqlite3.Row
            # 使用 WAL 模式，但确保数据立即写入
            self._local.conn.execute("PRAGMA journal_mode=WAL")
            self._local.conn.execute("PRAGMA synchronous=FULL")  # 改为 FULL 确保数据写入
        return self._local.conn
    
    @contextmanager
    def _get_connection(self):
        """获取数据库连接上下文"""
        if self._use_pool and self._pool:
            # 使用连接池
            with self._pool.get_connection() as conn:
                yield conn
        else:
            # 兼容模式：使用缓存连接
            conn = self._get_cached_connection()
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
    
    def close(self):
        """关闭数据库连接"""
        if self._use_pool and self._pool:
            # 关闭连接池
            self._pool.close_all()
            self._pool = None
            logger.info("数据库连接池已关闭")
        else:
            # 兼容模式
            if hasattr(self._local, 'conn') and self._local.conn is not None:
                try:
                    # 最终 checkpoint 确保所有数据写入主数据库文件
                    self._local.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                    self._local.conn.close()
                    logger.info("数据库连接已关闭")
                except Exception as e:
                    logger.error(f"关闭数据库连接失败: {e}")
                finally:
                    self._local.conn = None
    
    # ==================== Chunks ====================
    
    def save_chunk(self, chunk: VideoChunk) -> int:
        """保存视频切片"""
        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                INSERT INTO chunks (file_path, start_time, end_time, duration_seconds, status, batch_id, window_records_path)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    chunk.file_path,
                    chunk.start_time.isoformat() if chunk.start_time else None,
                    chunk.end_time.isoformat() if chunk.end_time else None,
                    chunk.duration_seconds,
                    chunk.status.value,
                    chunk.batch_id,
                    chunk.window_records_path
                )
            )
            return cursor.lastrowid
    
    def get_pending_chunks(self, limit: int = 100) -> List[VideoChunk]:
        """获取待分析的切片"""
        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                SELECT * FROM chunks 
                WHERE status = ? 
                ORDER BY
                    CASE
                        WHEN date(start_time) = date('now', 'localtime') THEN 0
                        ELSE 1
                    END,
                    start_time ASC
                LIMIT ?
                """,
                (ChunkStatus.PENDING.value, limit)
            )
            return [self._row_to_chunk(row) for row in cursor.fetchall()]

    def recover_interrupted_analysis(self) -> dict:
        """恢复上次退出时中断或失败、且源文件仍存在的分析任务。"""
        recovered = 0
        missing = 0
        interrupted_batches = 0

        with self._get_connection() as conn:
            rows = conn.execute(
                """
                SELECT id, file_path, status
                FROM chunks
                WHERE status IN (?, ?)
                """,
                (ChunkStatus.PROCESSING.value, ChunkStatus.FAILED.value),
            ).fetchall()

            recover_ids = []
            missing_ids = []
            for row in rows:
                if Path(row["file_path"]).exists():
                    recover_ids.append(row["id"])
                elif row["status"] == ChunkStatus.PROCESSING.value:
                    missing_ids.append(row["id"])

            for ids, status in (
                (recover_ids, ChunkStatus.PENDING.value),
                (missing_ids, ChunkStatus.FAILED.value),
            ):
                for offset in range(0, len(ids), 500):
                    chunk_ids = ids[offset:offset + 500]
                    placeholders = ",".join("?" for _ in chunk_ids)
                    conn.execute(
                        f"UPDATE chunks SET status = ?, batch_id = NULL WHERE id IN ({placeholders})",
                        (status, *chunk_ids),
                    )

            recovered = len(recover_ids)
            missing = len(missing_ids)

            cursor = conn.execute(
                """
                UPDATE analysis_batches
                SET status = ?,
                    error_message = ?,
                    completed_at = CURRENT_TIMESTAMP
                WHERE status = ?
                """,
                (
                    BatchStatus.FAILED.value,
                    "Application exited during analysis; chunks restored to pending",
                    BatchStatus.PROCESSING.value,
                ),
            )
            interrupted_batches = cursor.rowcount

        result = {
            "recovered": recovered,
            "missing": missing,
            "interrupted_batches": interrupted_batches,
        }
        if recovered or missing or interrupted_batches:
            logger.info(
                "分析任务恢复完成: 恢复 %s 个, 缺失文件 %s 个, 中断批次 %s 个",
                recovered,
                missing,
                interrupted_batches,
            )
        return result

    def get_chunk_progress_for_date(self, date: datetime) -> dict:
        """获取指定日期的视频切片分析进度。"""
        start = date.replace(hour=0, minute=0, second=0, microsecond=0)
        end = date.replace(hour=23, minute=59, second=59, microsecond=999999)

        counts = {
            ChunkStatus.PENDING.value: 0,
            ChunkStatus.PROCESSING.value: 0,
            ChunkStatus.COMPLETED.value: 0,
            ChunkStatus.FAILED.value: 0,
        }
        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                SELECT status, COUNT(*) AS count
                FROM chunks
                WHERE start_time >= ? AND start_time <= ?
                GROUP BY status
                """,
                (start.isoformat(), end.isoformat())
            )
            for row in cursor.fetchall():
                counts[row["status"]] = row["count"]

        counts["total"] = sum(counts.values())
        return counts
    
    def update_chunk_status(self, chunk_id: int, status: ChunkStatus, batch_id: Optional[int] = None):
        """更新切片状态"""
        with self._get_connection() as conn:
            if batch_id is not None:
                conn.execute(
                    "UPDATE chunks SET status = ?, batch_id = ? WHERE id = ?",
                    (status.value, batch_id, chunk_id)
                )
            else:
                conn.execute(
                    "UPDATE chunks SET status = ? WHERE id = ?",
                    (status.value, chunk_id)
                )
    
    def _row_to_chunk(self, row: sqlite3.Row) -> VideoChunk:
        """将数据库行转换为 VideoChunk 对象"""
        # 安全获取 window_records_path（兼容旧数据库）
        window_records_path = None
        try:
            window_records_path = row["window_records_path"]
        except (IndexError, KeyError):
            pass
        
        return VideoChunk(
            id=row["id"],
            file_path=row["file_path"],
            start_time=datetime.fromisoformat(row["start_time"]) if row["start_time"] else None,
            end_time=datetime.fromisoformat(row["end_time"]) if row["end_time"] else None,
            duration_seconds=row["duration_seconds"],
            status=ChunkStatus(row["status"]),
            batch_id=row["batch_id"],
            window_records_path=window_records_path
        )
    
    # ==================== Batches ====================
    
    def create_batch(self, batch: AnalysisBatch) -> int:
        """创建分析批次"""
        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                INSERT INTO analysis_batches (chunk_ids, start_time, end_time, status, observations_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    json.dumps(batch.chunk_ids),
                    batch.start_time.isoformat() if batch.start_time else None,
                    batch.end_time.isoformat() if batch.end_time else None,
                    batch.status.value,
                    batch.observations_json
                )
            )
            conn.execute(
                "UPDATE analysis_batches SET source_type = ?, source_ids = ? WHERE id = ?",
                (batch.source_type, json.dumps(batch.source_ids or batch.chunk_ids), cursor.lastrowid),
            )
            return cursor.lastrowid
    
    def update_batch(self, batch_id: int, status: BatchStatus, 
                     observations_json: Optional[str] = None,
                     error_message: Optional[str] = None):
        """更新批次状态"""
        with self._get_connection() as conn:
            if status == BatchStatus.COMPLETED:
                conn.execute(
                    """
                    UPDATE analysis_batches 
                    SET status = ?, observations_json = ?, completed_at = CURRENT_TIMESTAMP 
                    WHERE id = ?
                    """,
                    (status.value, observations_json or "[]", batch_id)
                )
            elif status == BatchStatus.FAILED:
                conn.execute(
                    """
                    UPDATE analysis_batches 
                    SET status = ?, error_message = ?, completed_at = CURRENT_TIMESTAMP 
                    WHERE id = ?
                    """,
                    (status.value, error_message, batch_id)
                )
            else:
                conn.execute(
                    "UPDATE analysis_batches SET status = ? WHERE id = ?",
                    (status.value, batch_id)
                )
    
    def get_pending_batches(self) -> List[AnalysisBatch]:
        """获取待处理的批次"""
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT * FROM analysis_batches WHERE status = ?",
                (BatchStatus.PENDING.value,)
            )
            return [self._row_to_batch(row) for row in cursor.fetchall()]
    
    def _row_to_batch(self, row: sqlite3.Row) -> AnalysisBatch:
        """将数据库行转换为 AnalysisBatch 对象"""
        return AnalysisBatch(
            id=row["id"],
            chunk_ids=json.loads(row["chunk_ids"]),
            start_time=datetime.fromisoformat(row["start_time"]) if row["start_time"] else None,
            end_time=datetime.fromisoformat(row["end_time"]) if row["end_time"] else None,
            status=BatchStatus(row["status"]),
            observations_json=row["observations_json"],
            error_message=row["error_message"],
            source_type=row["source_type"] if "source_type" in row.keys() else "video",
            source_ids=(
                json.loads(row["source_ids"])
                if "source_ids" in row.keys() and row["source_ids"] not in (None, "", "[]")
                else json.loads(row["chunk_ids"])
            ),
        )
    
    # ==================== Timeline Cards ====================

    @staticmethod
    def _insert_card(conn: sqlite3.Connection, card: ActivityCard, batch_id: Optional[int]) -> int:
        cursor = conn.execute(
            """
            INSERT INTO timeline_cards
            (batch_id, category, title, summary, start_time, end_time,
             app_sites_json, distractions_json, productivity_score,
             active_duration_seconds)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                batch_id,
                card.category,
                card.title,
                card.summary,
                card.start_time.isoformat() if card.start_time else None,
                card.end_time.isoformat() if card.end_time else None,
                json.dumps([a.to_dict() for a in card.app_sites]),
                json.dumps([d.to_dict() for d in card.distractions]),
                card.productivity_score,
                card.active_duration_seconds,
            ),
        )
        return cursor.lastrowid
    
    def save_card(self, card: ActivityCard, batch_id: Optional[int] = None) -> int:
        """保存时间轴卡片"""
        with self._get_connection() as conn:
            return self._insert_card(conn, card, batch_id)

    def replace_cards_for_batch(self, batch_id: int, cards: List[ActivityCard]) -> List[int]:
        """Replace a batch's cards atomically so a partial failure cannot leak into statistics."""
        with self._get_connection() as conn:
            conn.execute("DELETE FROM timeline_cards WHERE batch_id = ?", (batch_id,))
            return [self._insert_card(conn, card, batch_id) for card in cards]

    def apply_card_merges(self, merges: List[tuple]) -> None:
        """Extend existing cards with continuations from a later batch.

        ``merges`` holds ``(existing_card_id, incoming_card)`` pairs. The incoming card
        must not have been inserted on its own, otherwise the time would count twice.
        """
        if not merges:
            return
        with self._get_connection() as conn:
            for target_id, incoming in merges:
                row = conn.execute(
                    "SELECT * FROM timeline_cards WHERE id = ?", (target_id,)
                ).fetchone()
                if row is None:
                    logger.warning("续接卡片不存在，跳过合并: %s", target_id)
                    continue
                host = self._row_to_card(row)
                merge_card(host, incoming)
                conn.execute(
                    """
                    UPDATE timeline_cards
                    SET start_time = ?, end_time = ?, active_duration_seconds = ?,
                        app_sites_json = ?, distractions_json = ?, productivity_score = ?
                    WHERE id = ?
                    """,
                    (
                        host.start_time.isoformat() if host.start_time else None,
                        host.end_time.isoformat() if host.end_time else None,
                        host.active_duration_seconds,
                        json.dumps([a.to_dict() for a in host.app_sites]),
                        json.dumps([d.to_dict() for d in host.distractions]),
                        host.productivity_score,
                        target_id,
                    ),
                )

    def delete_cards_for_batch(self, batch_id: int) -> None:
        with self._get_connection() as conn:
            conn.execute("DELETE FROM timeline_cards WHERE batch_id = ?", (batch_id,))

    def save_consolidated_cards(
        self,
        cards: List[ActivityCard],
        delete_ids: Optional[List[int]] = None,
    ) -> None:
        """Persist cards produced by merging existing rows (historical cleanup).

        Surviving cards keep their id and batch; rows that were folded in are deleted.
        """
        with self._get_connection() as conn:
            if delete_ids:
                placeholders = ",".join("?" for _ in delete_ids)
                conn.execute(
                    f"DELETE FROM timeline_cards WHERE id IN ({placeholders})",
                    tuple(delete_ids),
                )
            for card in cards:
                if card.id is None:
                    continue
                conn.execute(
                    """
                    UPDATE timeline_cards
                    SET start_time = ?, end_time = ?, active_duration_seconds = ?,
                        app_sites_json = ?, distractions_json = ?, productivity_score = ?
                    WHERE id = ?
                    """,
                    (
                        card.start_time.isoformat() if card.start_time else None,
                        card.end_time.isoformat() if card.end_time else None,
                        card.active_duration_seconds,
                        json.dumps([a.to_dict() for a in card.app_sites]),
                        json.dumps([d.to_dict() for d in card.distractions]),
                        card.productivity_score,
                        card.id,
                    ),
                )
    
    def get_cards_for_date(self, date: datetime) -> List[ActivityCard]:
        """获取指定日期的时间轴卡片"""
        start = date.replace(hour=0, minute=0, second=0, microsecond=0)
        end = date.replace(hour=23, minute=59, second=59, microsecond=999999)
        
        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                SELECT t.* FROM timeline_cards t
                LEFT JOIN analysis_batches b ON b.id = t.batch_id
                WHERE t.start_time >= ? AND t.start_time <= ?
                  AND (t.batch_id IS NULL OR b.status = 'completed')
                ORDER BY t.start_time ASC
                """,
                (start.isoformat(), end.isoformat())
            )
            return [self._row_to_card(row) for row in cursor.fetchall()]

    def get_cards_for_range(
        self,
        start_date: date | datetime,
        end_date: date | datetime,
    ) -> List[ActivityCard]:
        """获取包含首尾日期的时间轴卡片。"""
        if isinstance(start_date, datetime):
            start = start_date.replace(hour=0, minute=0, second=0, microsecond=0)
        else:
            start = datetime.combine(start_date, datetime.min.time())

        if isinstance(end_date, datetime):
            end = end_date.replace(hour=23, minute=59, second=59, microsecond=999999)
        else:
            end = datetime.combine(end_date, datetime.max.time())

        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                SELECT t.* FROM timeline_cards t
                LEFT JOIN analysis_batches b ON b.id = t.batch_id
                WHERE t.start_time >= ? AND t.start_time <= ?
                  AND (t.batch_id IS NULL OR b.status = 'completed')
                ORDER BY t.start_time ASC
                """,
                (start.isoformat(), end.isoformat()),
            )
            return [self._row_to_card(row) for row in cursor.fetchall()]
    
    def get_recent_cards(self, limit: int = 10) -> List[ActivityCard]:
        """获取最近的卡片（用作上下文）"""
        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                SELECT t.* FROM timeline_cards t
                LEFT JOIN analysis_batches b ON b.id = t.batch_id
                WHERE t.batch_id IS NULL OR b.status = 'completed'
                ORDER BY t.end_time DESC
                LIMIT ?
                """,
                (limit,)
            )
            return [self._row_to_card(row) for row in cursor.fetchall()]
    
    def _row_to_card(self, row: sqlite3.Row) -> ActivityCard:
        """将数据库行转换为 ActivityCard 对象"""
        return ActivityCard(
            id=row["id"],
            category=row["category"],
            title=row["title"],
            summary=row["summary"],
            start_time=datetime.fromisoformat(row["start_time"]) if row["start_time"] else None,
            end_time=datetime.fromisoformat(row["end_time"]) if row["end_time"] else None,
            app_sites=[AppSite.from_dict(a) for a in json.loads(row["app_sites_json"] or "[]")],
            distractions=[Distraction.from_dict(d) for d in json.loads(row["distractions_json"] or "[]")],
            productivity_score=row["productivity_score"],
            active_duration_seconds=row["active_duration_seconds"]
        )
    
    def update_card(self, card_id: int, category: str = None, title: str = None, 
                    summary: str = None, productivity_score: float = None) -> bool:
        """更新时间轴卡片"""
        try:
            with self._get_connection() as conn:
                # 构建动态更新语句
                updates = []
                params = []
                
                if category is not None:
                    updates.append("category = ?")
                    params.append(category)
                if title is not None:
                    updates.append("title = ?")
                    params.append(title)
                if summary is not None:
                    updates.append("summary = ?")
                    params.append(summary)
                if productivity_score is not None:
                    updates.append("productivity_score = ?")
                    params.append(productivity_score)
                
                if not updates:
                    return False
                
                params.append(card_id)
                sql = f"UPDATE timeline_cards SET {', '.join(updates)} WHERE id = ?"
                conn.execute(sql, params)
                logger.info(f"已更新卡片 {card_id}")
                return True
        except Exception as e:
            logger.error(f"更新卡片失败 {card_id}: {e}")
            return False
    
    def delete_card(self, card_id: int) -> bool:
        """删除时间轴卡片"""
        try:
            with self._get_connection() as conn:
                conn.execute("DELETE FROM timeline_cards WHERE id = ?", (card_id,))
                logger.info(f"已删除卡片 {card_id}")
                return True
        except Exception as e:
            logger.error(f"删除卡片失败 {card_id}: {e}")
            return False
    
    # ==================== Settings ====================
    
    def get_setting(self, key: str, default: str = "") -> str:
        """获取设置值 - 使用独立连接确保读取最新数据"""
        try:
            conn = sqlite3.connect(str(self.db_path), timeout=10.0)
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(
                "SELECT value FROM settings WHERE key = ?", (key,)
            )
            row = cursor.fetchone()
            conn.close()
            value = row["value"] if row else default
            logger.debug(f"读取设置 {key}: {'已找到' if row else '使用默认值'}")
            return value
        except Exception as e:
            logger.error(f"读取设置失败 {key}: {e}")
            return default
    
    def set_setting(self, key: str, value: str):
        """设置值 - 使用独立连接确保立即写入"""
        try:
            conn = sqlite3.connect(str(self.db_path), timeout=10.0)
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute(
                """
                INSERT INTO settings (key, value, updated_at) 
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(key) DO UPDATE SET value = ?, updated_at = CURRENT_TIMESTAMP
                """,
                (key, value, value)
            )
            conn.commit()
            # 强制 checkpoint 确保 WAL 数据写入主文件
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.close()
            logger.info(f"已保存设置 {key}")
        except Exception as e:
            logger.error(f"保存设置失败 {key}: {e}")

    # ==================== Daily Reports ====================

    def get_missing_daily_report_dates(self, start_date: date, end_date: date) -> List[str]:
        """Return dates that have completed activity cards but no cached daily report."""
        with self._get_connection() as conn:
            rows = conn.execute(
                """
                SELECT date(t.start_time) AS report_date
                FROM timeline_cards t
                LEFT JOIN analysis_batches b ON b.id = t.batch_id
                LEFT JOIN daily_reports r ON r.report_date = date(t.start_time)
                WHERE date(t.start_time) BETWEEN ? AND ?
                  AND r.id IS NULL
                  AND (t.batch_id IS NULL OR b.status = 'completed')
                GROUP BY date(t.start_time)
                ORDER BY report_date ASC
                """,
                (start_date.isoformat(), end_date.isoformat()),
            ).fetchall()
            return [row["report_date"] for row in rows]

    def has_open_inputs_for_date(self, date_str: str) -> bool:
        """Check whether a date still has queued or in-flight source analysis."""
        with self._get_connection() as conn:
            capture = conn.execute(
                """
                SELECT 1 FROM capture_batches
                WHERE date(start_time) = ? AND status IN (?, ?)
                LIMIT 1
                """,
                (date_str, ChunkStatus.PENDING.value, ChunkStatus.PROCESSING.value),
            ).fetchone()
            video = conn.execute(
                """
                SELECT 1 FROM chunks
                WHERE date(start_time) = ? AND status IN (?, ?)
                LIMIT 1
                """,
                (date_str, ChunkStatus.PENDING.value, ChunkStatus.PROCESSING.value),
            ).fetchone()
            return bool(capture or video)

    def save_daily_report(self, date_str: str, content: str) -> int:
        """保存日报到缓存"""
        try:
            conn = sqlite3.connect(str(self.db_path), timeout=10.0)
            conn.execute("PRAGMA synchronous=FULL")
            cursor = conn.execute(
                """
                INSERT INTO daily_reports (report_date, content)
                VALUES (?, ?)
                ON CONFLICT(report_date) DO UPDATE SET content = ?, created_at = CURRENT_TIMESTAMP
                """,
                (date_str, content, content)
            )
            conn.commit()
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.close()
            logger.info(f"已保存日报: {date_str}")
            return cursor.lastrowid
        except Exception as e:
            logger.error(f"保存日报失败 {date_str}: {e}")
            return -1

    def get_daily_report(self, date_str: str) -> Optional[str]:
        """获取缓存的日报"""
        try:
            conn = sqlite3.connect(str(self.db_path), timeout=10.0)
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(
                "SELECT content FROM daily_reports WHERE report_date = ?",
                (date_str,)
            )
            row = cursor.fetchone()
            conn.close()
            return row["content"] if row else None
        except Exception as e:
            logger.error(f"读取日报失败 {date_str}: {e}")
            return None
