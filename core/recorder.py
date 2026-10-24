"""
Dayflow Windows - 屏幕录制模块
使用 dxcam 实现低功耗 1FPS 录制
"""
import time
import logging
import threading
import json
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional, Callable, List, Dict

import numpy as np
import dxcam
import cv2

import config
from core.types import VideoChunk, CaptureBatch, ChunkStatus
from core.window_tracker import get_tracker, WindowInfo

logger = logging.getLogger(__name__)


def _enable_per_monitor_dpi() -> None:
    """让采集线程按物理像素枚举显示器，否则高 DPI 下裁剪区域会偏小。"""
    try:
        from ctypes import c_void_p, windll
        # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
        windll.user32.SetThreadDpiAwarenessContext(c_void_p(-4))
    except Exception as exc:  # pragma: no cover - 仅老系统会走到这里
        logger.debug("设置 DPI 感知上下文失败: %s", exc)


def _session_locked() -> bool:
    """锁屏/切换用户时输入桌面不可访问，此时截图只会得到黑屏。"""
    try:
        from ctypes import windll
        handle = windll.user32.OpenInputDesktop(0, False, 0x0001)  # DESKTOP_READOBJECTS
        if not handle:
            return True
        windll.user32.CloseDesktop(handle)
        return False
    except Exception as exc:  # pragma: no cover - 老系统或权限受限
        logger.debug("检测锁屏状态失败: %s", exc)
        return False


class ScreenRecorder:
    """
    屏幕录制器
    - 1 FPS 低功耗录制
    - 每 60 秒自动切片
    - H.264 编码，低码率
    """
    
    def __init__(
        self,
        fps: int = None,
        chunk_duration: int = None,
        output_dir: Path = None,
        on_chunk_saved: Optional[Callable[[VideoChunk], None]] = None,
        output_idx: int = 0,
    ):
        self.fps = fps or config.RECORD_FPS
        self.chunk_duration = chunk_duration or config.CHUNK_DURATION_SECONDS
        self.output_dir = output_dir or config.CHUNKS_DIR
        self.on_chunk_saved = on_chunk_saved
        self._all_screens = (output_idx == -1)
        self.output_idx = int(output_idx) if output_idx is not None else 0

        # 状态
        self._recording = False
        self._paused = False
        self._camera: Optional[dxcam.DXCamera] = None
        self._record_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

        # 多相机模式（全部屏幕）
        self._cameras: list = []
        self._output_geometries: list = []
        self._canvas_shape: Optional[tuple] = None
        self._canvas_offset_x: int = 0
        self._canvas_offset_y: int = 0
        
        # 当前切片信息
        self._current_writer: Optional[cv2.VideoWriter] = None
        self._current_chunk_path: Optional[Path] = None
        self._current_chunk_start: Optional[datetime] = None
        self._frame_count = 0
        
        # 窗口追踪
        self._window_tracker = get_tracker()
        self._current_window_records: List[Dict] = []  # 当前切片的窗口记录
        
        # 确保输出目录存在
        self.output_dir.mkdir(parents=True, exist_ok=True)
    
    @property
    def is_recording(self) -> bool:
        return self._recording
    
    @property
    def is_paused(self) -> bool:
        return self._paused
    
    def start(self):
        """开始录制"""
        if self._recording:
            logger.warning("录制已在进行中")
            return

        if self._all_screens:
            logger.info("开始屏幕录制... (全部屏幕模式)")
            self._create_all_cameras()
        else:
            logger.info(f"开始屏幕录制... (显示器 output_idx={self.output_idx})")
            self._camera = self._create_camera_with_fallback()
        
        self._recording = True
        self._paused = False
        self._stop_event.clear()
        
        # 启动录制线程
        self._record_thread = threading.Thread(target=self._recording_loop, daemon=True)
        self._record_thread.start()
        
        logger.info(f"录制已启动 - FPS: {self.fps}, 切片时长: {self.chunk_duration}秒")
    
    def stop(self):
        """停止录制"""
        if not self._recording:
            return
        
        logger.info("停止屏幕录制...")
        
        self._stop_event.set()
        self._recording = False
        
        # 等待录制线程结束（缩短超时时间）
        if self._record_thread and self._record_thread.is_alive():
            self._record_thread.join(timeout=2)
            if self._record_thread.is_alive():
                logger.warning("录制线程未能在超时内停止")
        
        # 保存当前切片
        try:
            self._finalize_current_chunk()
        except Exception as e:
            logger.error(f"保存切片时出错: {e}")
        
        # 释放 dxcam
        try:
            if self._all_screens:
                for cam in self._cameras:
                    try:
                        del cam
                    except Exception as e:
                        logger.error(f"释放相机时出错: {e}")
                self._cameras = []
                self._output_geometries = []
                self._canvas_shape = None
            elif self._camera:
                del self._camera
                self._camera = None
        except Exception as e:
            logger.error(f"释放相机时出错: {e}")
        
        logger.info("录制已停止")
    
    def pause(self):
        """暂停录制"""
        if self._recording and not self._paused:
            self._paused = True
            logger.info("录制已暂停")
    
    def resume(self):
        """恢复录制"""
        if self._recording and self._paused:
            self._paused = False
            logger.info("录制已恢复")
    
    def _create_camera_with_fallback(self) -> dxcam.DXCamera:
        """创建 dxcam 相机，失败时尝试多种降级参数。"""
        attempts = [
            {"output_idx": self.output_idx, "output_color": "BGR"},
            {"output_idx": self.output_idx},
            {"device_idx": 0, "output_idx": self.output_idx, "output_color": "BGR"},
            {"device_idx": 0, "output_idx": self.output_idx},
            {},
        ]
        last_error = None

        for idx, kwargs in enumerate(attempts, start=1):
            try:
                logger.info(f"尝试初始化 dxcam ({idx}/{len(attempts)}): {kwargs}")
                camera = dxcam.create(**kwargs)
                # 部分环境 create 成功但首次 grab 才会炸，这里预抓一帧尽早暴露问题
                test_frame = camera.grab()
                if test_frame is None:
                    logger.warning("dxcam 初始化成功，但首次抓帧为空；继续使用并在录制循环中重试")
                logger.info("dxcam 初始化成功")
                return camera
            except Exception as e:
                last_error = e
                logger.warning(f"dxcam 初始化尝试失败 ({kwargs}): {e}")
                time.sleep(0.3)

        error_message = (
            "初始化屏幕录制失败。可能原因：显卡/显示器驱动异常、远程桌面环境、"
            "dxcam 与当前输出设备不兼容。建议重启应用、更新显卡驱动，或切换显示器后重试。"
        )
        logger.error(f"{error_message} 最后错误: {last_error}")
        raise RuntimeError(error_message) from last_error

    def _create_all_cameras(self):
        """为每个已连接的显示器创建 dxcam 相机，计算虚拟桌面画布。"""
        self._cameras = []
        self._output_geometries = []

        for output_idx in range(16):
            try:
                cam = dxcam.create(
                    device_idx=0,
                    output_idx=output_idx,
                    output_color="BGR",
                )
                output_obj = cam._output
                output_obj.update_desc()

                if not output_obj.attached_to_desktop:
                    del cam
                    continue

                coords = output_obj.desc.DesktopCoordinates
                geom = {
                    "output_idx": output_idx,
                    "left": coords.left,
                    "top": coords.top,
                    "width": output_obj.resolution[0],
                    "height": output_obj.resolution[1],
                }
                self._cameras.append(cam)
                self._output_geometries.append(geom)
                logger.info(
                    f"  显示器 {output_idx}: {geom['width']}x{geom['height']} "
                    f"at ({geom['left']}, {geom['top']})"
                )
            except Exception as e:
                logger.debug(f"枚举输出 {output_idx} 结束或失败: {e}")
                break

        if not self._cameras:
            raise RuntimeError("未找到任何可用的显示器输出")

        # 计算虚拟桌面边界框
        min_left = min(g["left"] for g in self._output_geometries)
        min_top = min(g["top"] for g in self._output_geometries)
        max_right = max(g["left"] + g["width"] for g in self._output_geometries)
        max_bottom = max(g["top"] + g["height"] for g in self._output_geometries)

        self._canvas_offset_x = min_left
        self._canvas_offset_y = min_top
        canvas_width = max_right - min_left
        canvas_height = max_bottom - min_top
        self._canvas_shape = (canvas_height, canvas_width, 3)

        logger.info(
            f"虚拟桌面: {canvas_width}x{canvas_height}, "
            f"偏移: ({min_left}, {min_top}), "
            f"显示器数: {len(self._cameras)}"
        )

    def _grab_all_screens(self) -> Optional[np.ndarray]:
        """从所有相机抓取帧并拼接到虚拟桌面画布。"""
        if not self._cameras or self._canvas_shape is None:
            return None

        canvas = np.zeros(self._canvas_shape, dtype=np.uint8)

        for cam, geom in zip(self._cameras, self._output_geometries):
            frame = cam.grab()
            if frame is None:
                continue

            x = geom["left"] - self._canvas_offset_x
            y = geom["top"] - self._canvas_offset_y
            h = geom["height"]
            w = geom["width"]

            fh, fw = frame.shape[:2]
            if fh != h or fw != w:
                frame = cv2.resize(frame, (w, h))

            canvas[y:y + h, x:x + w] = frame

        return canvas

    def _recording_loop(self):
        """录制主循环"""
        frame_interval = 1.0 / self.fps
        last_frame_time = 0
        last_window_info = None  # 缓存上次窗口信息
        
        while not self._stop_event.is_set():
            current_time = time.time()
            
            # 控制帧率 - 使用精确等待而非轮询
            time_to_wait = frame_interval - (current_time - last_frame_time)
            if time_to_wait > 0:
                self._stop_event.wait(min(time_to_wait, 0.5))  # 最多等待0.5秒，确保能响应停止信号
                continue
            
            # 暂停检查
            if self._paused:
                self._stop_event.wait(0.5)
                continue
            
            try:
                # 捕获屏幕
                if self._all_screens:
                    frame = self._grab_all_screens()
                else:
                    frame = self._camera.grab()
                if frame is None:
                    time.sleep(0.1)
                    continue
                
                # 先采集窗口信息（在帧捕获时立即采集，确保时间对齐）
                frame_capture_time = datetime.now()
                window_info = self._window_tracker.get_active_window()
                
                # 检查是否需要创建新切片
                if self._should_create_new_chunk():
                    self._finalize_current_chunk()
                    self._create_new_chunk(frame.shape)
                    last_window_info = None  # 重置窗口缓存
                
                # 记录窗口信息（仅在窗口变化时记录，减少数据量）
                if self._current_chunk_start and window_info:
                    # 检查窗口是否变化
                    window_changed = (
                        last_window_info is None or
                        last_window_info.app_name != window_info.app_name or
                        last_window_info.window_title != window_info.window_title
                    )
                    
                    if window_changed:
                        elapsed = (frame_capture_time - self._current_chunk_start).total_seconds()
                        self._current_window_records.append({
                            "timestamp": elapsed,
                            "app_name": self._window_tracker.get_friendly_app_name(window_info),
                            "window_title": window_info.window_title,
                            "process_name": window_info.app_name
                        })
                        last_window_info = window_info
                
                # 写入帧
                if self._current_writer:
                    self._current_writer.write(frame)
                    self._frame_count += 1
                
                last_frame_time = current_time
                
            except Exception as e:
                logger.error(f"录制帧错误: {e}")
                time.sleep(1)
    
    def _should_create_new_chunk(self) -> bool:
        """检查是否需要创建新切片"""
        if self._current_chunk_start is None:
            return True
        
        elapsed = (datetime.now() - self._current_chunk_start).total_seconds()
        return elapsed >= self.chunk_duration
    
    def _create_new_chunk(self, frame_shape: tuple):
        """创建新的视频切片"""
        timestamp = datetime.now()
        filename = f"chunk_{timestamp.strftime('%Y%m%d_%H%M%S')}.mp4"
        self._current_chunk_path = self.output_dir / filename
        self._current_chunk_start = timestamp
        self._frame_count = 0
        self._current_window_records = []  # 重置窗口记录
        
        # 创建 VideoWriter
        height, width = frame_shape[:2]
        fourcc = cv2.VideoWriter_fourcc(*'mp4v')
        
        self._current_writer = cv2.VideoWriter(
            str(self._current_chunk_path),
            fourcc,
            self.fps,
            (width, height)
        )
        
        logger.debug(f"创建新切片: {filename}")
    
    def _finalize_current_chunk(self):
        """完成当前切片"""
        if self._current_writer is None:
            return
        
        self._current_writer.release()
        
        if self._current_chunk_path and self._current_chunk_path.exists():
            end_time = datetime.now()
            duration = (end_time - self._current_chunk_start).total_seconds()
            
            # 保存窗口记录到 JSON 文件
            window_records_path = None
            if self._current_window_records:
                window_records_path = self._current_chunk_path.with_suffix('.json')
                try:
                    with open(window_records_path, 'w', encoding='utf-8') as f:
                        json.dump(self._current_window_records, f, ensure_ascii=False, indent=2)
                    logger.debug(f"窗口记录已保存: {window_records_path.name}")
                except Exception as e:
                    logger.warning(f"保存窗口记录失败: {e}")
                    window_records_path = None
            
            # 创建切片对象
            chunk = VideoChunk(
                file_path=str(self._current_chunk_path),
                start_time=self._current_chunk_start,
                end_time=end_time,
                duration_seconds=duration,
                status=ChunkStatus.PENDING,
                window_records_path=str(window_records_path) if window_records_path else None
            )
            
            logger.info(f"切片已保存: {self._current_chunk_path.name} ({duration:.1f}秒, {self._frame_count}帧, {len(self._current_window_records)}条窗口记录)")
            
            # 回调通知
            if self.on_chunk_saved:
                try:
                    self.on_chunk_saved(chunk)
                except Exception as e:
                    logger.error(f"切片保存回调错误: {e}")
        
        self._current_writer = None
        self._current_chunk_path = None
        self._current_chunk_start = None
        self._frame_count = 0
        self._current_window_records = []


class ScreenshotRecorder(ScreenRecorder):
    """按固定时间窗口保存 JPEG 截图，不生成视频文件。

    默认用 Pillow 的 GDI 截图（``ImageGrab``）：显示器插拔、锁屏、分辨率切换时
    只会在当次调用返回错误，不会像 DXGI 那样在采集线程里无限重试把整个录制卡死。
    ``CAPTURE_BACKEND=dxcam`` 可切回原来的 DXGI 采集。
    """

    def __init__(self, interval_seconds=None, batch_duration_minutes=None,
                 output_dir=None, on_batch_saved=None, output_idx=0):
        super().__init__(fps=1, chunk_duration=60, output_dir=output_dir or config.CAPTURES_DIR,
                         output_idx=output_idx)
        self.interval_seconds = max(1, int(interval_seconds or config.CAPTURE_INTERVAL_SECONDS))
        self.batch_duration_seconds = max(
            60, int(batch_duration_minutes or config.CAPTURE_BATCH_DURATION_MINUTES) * 60
        )
        self.on_batch_saved = on_batch_saved
        self._batch_window_start = None
        self._batch_window_end = None
        self._batch_dir = None
        self._batch_manifest_path = None
        self._batch_manifest = []
        self._batch_first_capture = None
        self._batch_last_capture = None
        # 空闲暂停会在另一个线程里修剪/封存当前批次
        self._batch_lock = threading.RLock()
        self._backend = (config.CAPTURE_BACKEND or "gdi").lower()
        # 采集线程代次：卡死重启后旧线程的结果必须丢弃
        self._generation = 0
        self._last_progress = time.monotonic()
        self._watchdog_thread = None
        self._session_was_locked = False

    def start(self):
        if self._recording:
            logger.warning("截图采集已在进行中")
            return
        self._open_capture_backend()
        self._recording = True
        self._paused = False
        self._stop_event.clear()
        self._last_progress = time.monotonic()
        self._generation += 1
        self._record_thread = threading.Thread(
            target=self._recording_loop, args=(self._generation,), daemon=True
        )
        self._record_thread.start()
        self._watchdog_thread = threading.Thread(
            target=self._watchdog_loop, args=(self._generation,), daemon=True
        )
        self._watchdog_thread.start()
        logger.info(
            "截图采集已启动 - 间隔: %s秒, 批次窗口: %s分钟, 采集后端: %s",
            self.interval_seconds, self.batch_duration_seconds // 60, self._backend,
        )

    def stop(self):
        if not self._recording:
            return
        self._stop_event.set()
        self._recording = False
        self._generation += 1  # 让可能卡在 grab 里的旧线程回来后直接退出
        if self._record_thread and self._record_thread.is_alive():
            self._record_thread.join(timeout=2)
        if self._watchdog_thread and self._watchdog_thread.is_alive():
            self._watchdog_thread.join(timeout=2)
        self._watchdog_thread = None
        with self._batch_lock:
            self._finalize_capture_batch()
        self._release_cameras()
        logger.info("截图采集已停止")

    def pause(self, trim_end: Optional[datetime] = None):
        """暂停采集。``trim_end`` 用于把空闲开始后的截图从当前批次中剔除。"""
        if not (self._recording and not self._paused):
            return
        # 先置暂停，避免采集线程在修剪过程中再写入一张空闲截图
        self._paused = True
        if trim_end is not None:
            self._trim_and_seal_batch(trim_end)
        logger.info("录制已暂停")

    def resume(self):
        if self._recording and self._paused:
            self._paused = False
            self._last_progress = time.monotonic()
            logger.info("录制已恢复")

    # ---------- 采集后端 ----------

    def _open_capture_backend(self):
        if self._backend == "dxcam":
            if self._all_screens:
                self._create_all_cameras()
            else:
                self._camera = self._create_camera_with_fallback()
            return
        try:
            from PIL import ImageGrab  # noqa: F401
        except Exception as exc:
            logger.warning("Pillow 截图不可用，回退到 dxcam: %s", exc)
            self._backend = "dxcam"
            self._open_capture_backend()

    def _release_cameras(self):
        try:
            if self._cameras:
                for cam in self._cameras:
                    del cam
                self._cameras = []
                self._output_geometries = []
                self._canvas_shape = None
            if self._camera:
                del self._camera
                self._camera = None
        except Exception as exc:
            logger.warning("释放截图相机失败: %s", exc)

    def _grab_frame(self):
        if self._backend == "dxcam":
            return self._grab_all_screens() if self._all_screens else self._camera.grab()
        return self._grab_gdi()

    def _grab_gdi(self):
        from PIL import ImageGrab

        if _session_locked():
            if not self._session_was_locked:
                self._session_was_locked = True
                logger.info("检测到锁屏或用户切换，暂停截图")
            return None
        if self._session_was_locked:
            self._session_was_locked = False
            logger.info("锁屏结束，恢复截图")

        bbox = None
        if not self._all_screens:
            bbox = self._monitor_bbox(self.output_idx)
        image = ImageGrab.grab(bbox=bbox, all_screens=True)
        return cv2.cvtColor(np.array(image), cv2.COLOR_RGB2BGR)

    @staticmethod
    def _monitor_bbox(index: int):
        """返回指定显示器的虚拟桌面坐标；拿不到时退回整个虚拟桌面。"""
        try:
            import win32api
            monitors = win32api.EnumDisplayMonitors()
            if 0 <= index < len(monitors):
                left, top, right, bottom = monitors[index][2]
                return (left, top, right, bottom)
            logger.warning("显示器序号 %s 超出范围（共 %s 个），改用全部屏幕", index, len(monitors))
        except Exception as exc:
            logger.debug("枚举显示器失败，改用全部屏幕: %s", exc)
        return None

    # ---------- 采集循环 ----------

    def _recording_loop(self, generation: int):
        _enable_per_monitor_dpi()
        next_capture = 0.0
        while not self._stop_event.is_set():
            if generation != self._generation:
                return
            now_monotonic = time.monotonic()
            if now_monotonic < next_capture:
                self._stop_event.wait(min(next_capture - now_monotonic, 0.5))
                continue
            next_capture = now_monotonic + self.interval_seconds
            if self._paused:
                continue
            try:
                frame = self._grab_frame()
            except Exception as exc:
                logger.error("截图失败: %s", exc)
                frame = None
            self._last_progress = time.monotonic()
            if generation != self._generation:
                return
            if frame is None:
                continue
            try:
                self._store_capture(frame)
            except Exception as exc:
                logger.error("截图采集错误: %s", exc)

    def _watchdog_loop(self, generation: int):
        """采集线程一旦卡在底层抓屏调用里，主动重启它。"""
        interval = max(5, int(config.CAPTURE_WATCHDOG_INTERVAL_SECONDS))
        timeout = max(interval * 2, int(config.CAPTURE_STALL_TIMEOUT_SECONDS))
        while not self._stop_event.is_set() and generation == self._generation:
            self._stop_event.wait(interval)
            if self._stop_event.is_set() or generation != self._generation:
                return
            if not self._recording or self._paused:
                self._last_progress = time.monotonic()
                continue
            stalled = time.monotonic() - self._last_progress
            if stalled < timeout:
                continue
            logger.error("截图采集已卡住 %.0f 秒，重启采集线程", stalled)
            self._last_progress = time.monotonic()
            self._generation += 1
            generation = self._generation
            self._record_thread = threading.Thread(
                target=self._recording_loop, args=(generation,), daemon=True
            )
            self._record_thread.start()

    def _store_capture(self, frame, captured_at: Optional[datetime] = None):
        captured_at = captured_at or datetime.now()
        with self._batch_lock:
            if self._stop_event.is_set():
                return
            self._ensure_capture_batch(captured_at)
            filename = f"capture_{captured_at.strftime('%Y%m%d_%H%M%S_%f')[:-3]}.jpg"
            path = self._batch_dir / filename
            target = frame
            if config.CAPTURE_RESIZE_WIDTH and config.CAPTURE_RESIZE_HEIGHT:
                target = cv2.resize(frame, (config.CAPTURE_RESIZE_WIDTH, config.CAPTURE_RESIZE_HEIGHT))
            ok = cv2.imwrite(str(path), target, [cv2.IMWRITE_JPEG_QUALITY, config.CAPTURE_JPEG_QUALITY])
            if not ok:
                raise RuntimeError(f"写入截图失败: {path}")

            window_info = self._window_tracker.get_active_window()
            record = {
                "file": filename,
                "timestamp": captured_at.isoformat(),
                "relative_seconds": (captured_at - self._batch_first_capture).total_seconds(),
                "app_name": self._window_tracker.get_friendly_app_name(window_info) if window_info else "Unknown",
                "window_title": window_info.window_title if window_info else "",
                "process_name": window_info.app_name if window_info else "",
            }
            self._batch_manifest.append(record)
            self._batch_last_capture = captured_at
            self._write_manifest()
            if captured_at >= self._batch_window_end:
                self._finalize_capture_batch()

    # ---------- 批次管理（调用方需持有 _batch_lock） ----------

    def _ensure_capture_batch(self, captured_at):
        if self._batch_window_start is not None and captured_at < self._batch_window_end:
            return
        if self._batch_window_start is not None:
            self._finalize_capture_batch()
        epoch = captured_at.timestamp()
        start_epoch = epoch - (epoch % self.batch_duration_seconds)
        self._batch_window_start = datetime.fromtimestamp(start_epoch)
        self._batch_window_end = self._batch_window_start + timedelta(seconds=self.batch_duration_seconds)
        folder = self._batch_window_start.strftime("%Y-%m-%d")
        name = self._batch_window_start.strftime("%H-%M")
        self._batch_dir = self.output_dir / folder / name
        # 空闲暂停会把同一 5 分钟窗口提前封存，恢复后不能再写进已入库的目录
        suffix = 1
        while self._batch_dir.exists():
            suffix += 1
            self._batch_dir = self.output_dir / folder / f"{name}-{suffix}"
        self._batch_dir.mkdir(parents=True, exist_ok=True)
        self._batch_manifest_path = self._batch_dir / "manifest.json"
        self._batch_manifest = []
        self._batch_first_capture = captured_at
        self._batch_last_capture = captured_at

    def _write_manifest(self):
        with self._batch_manifest_path.open("w", encoding="utf-8") as stream:
            json.dump({
                "window_start": self._batch_window_start.isoformat(),
                "window_end": self._batch_window_end.isoformat(),
                "interval_seconds": self.interval_seconds,
                "images": self._batch_manifest,
            }, stream, ensure_ascii=False, indent=2)

    def _reset_batch_state(self):
        self._batch_window_start = None
        self._batch_window_end = None
        self._batch_dir = None
        self._batch_manifest_path = None
        self._batch_manifest = []
        self._batch_first_capture = None
        self._batch_last_capture = None

    def _discard_capture_batch(self):
        """丢弃当前批次（没有可用截图时），同时清掉空目录。"""
        directory = self._batch_dir
        self._reset_batch_state()
        if not directory:
            return
        try:
            manifest = directory / "manifest.json"
            if manifest.exists():
                manifest.unlink()
            directory.rmdir()
            directory.parent.rmdir()
        except OSError:
            pass

    def _trim_and_seal_batch(self, cutoff: datetime):
        """把 cutoff 之后的截图删掉并立即封存，空闲时间不再计入有效时长。"""
        with self._batch_lock:
            if not self._batch_manifest or not self._batch_dir:
                return
            kept = []
            dropped = []
            for record in self._batch_manifest:
                try:
                    captured_at = datetime.fromisoformat(record["timestamp"])
                except (KeyError, ValueError):
                    kept.append(record)
                    continue
                (kept if captured_at <= cutoff else dropped).append(record)

            for record in dropped:
                path = self._batch_dir / str(record.get("file", ""))
                try:
                    path.unlink(missing_ok=True)
                except OSError as exc:
                    logger.debug("删除空闲截图失败 %s: %s", path, exc)

            self._batch_manifest = kept
            if not kept:
                logger.info("空闲开始前的批次没有截图，已丢弃")
                self._discard_capture_batch()
                return
            self._batch_first_capture = datetime.fromisoformat(kept[0]["timestamp"])
            self._batch_last_capture = datetime.fromisoformat(kept[-1]["timestamp"])
            self._write_manifest()
            self._finalize_capture_batch(end_override=cutoff)
            logger.info("已剔除空闲前 %s 张截图", len(dropped))

    def _finalize_capture_batch(self, end_override: Optional[datetime] = None):
        with self._batch_lock:
            if not self._batch_dir or not self._batch_manifest or not self._batch_first_capture:
                self._reset_batch_state()
                return
            self._write_manifest()
            end_time = min(
                self._batch_window_end,
                self._batch_last_capture + timedelta(seconds=self.interval_seconds),
            )
            if end_override is not None:
                end_time = min(end_time, max(end_override, self._batch_last_capture))
            if end_time <= self._batch_first_capture:
                self._discard_capture_batch()
                return
            batch = CaptureBatch(
                directory_path=str(self._batch_dir),
                manifest_path=str(self._batch_manifest_path),
                start_time=self._batch_first_capture,
                end_time=end_time,
                duration_seconds=max(0, (end_time - self._batch_first_capture).total_seconds()),
                image_count=len(self._batch_manifest),
                status=ChunkStatus.PENDING,
            )
            logger.info("截图批次已封存: %s (%s张)", self._batch_dir, batch.image_count)
            self._reset_batch_state()
            if self.on_batch_saved:
                self.on_batch_saved(batch)


class RecordingManager:
    """
    录制管理器
    整合录制器和数据库存储，支持空闲自动暂停
    """

    def __init__(self, storage_manager=None):
        from database.storage import StorageManager
        self.storage = storage_manager or StorageManager()
        try:
            output_idx = int(self.storage.get_setting("record_output_idx", "0"))
        except Exception:
            output_idx = 0
        capture_dir = config.CAPTURES_DIR
        custom_dir = self.storage.get_setting("custom_chunks_dir", "")
        if custom_dir:
            capture_dir = Path(custom_dir)
        self.recorder = ScreenshotRecorder(
            interval_seconds=self._get_int_setting("capture_interval_seconds", config.CAPTURE_INTERVAL_SECONDS),
            batch_duration_minutes=self._get_int_setting("capture_batch_duration_minutes", config.CAPTURE_BATCH_DURATION_MINUTES),
            output_dir=capture_dir,
            on_batch_saved=self._on_batch_saved,
            output_idx=output_idx,
        )

        self._idle_paused = False
        self._idle_detector = None

    def _on_chunk_saved(self, chunk: VideoChunk):
        """切片保存回调 - 写入数据库"""
        try:
            chunk_id = self.storage.save_chunk(chunk)
            logger.info(f"切片已入库: ID={chunk_id}")
        except Exception as e:
            logger.error(f"切片入库失败: {e}")

    def _get_int_setting(self, key, default):
        try:
            return int(self.storage.get_setting(key, str(default)))
        except (TypeError, ValueError):
            return default

    def _on_batch_saved(self, batch: CaptureBatch):
        try:
            batch_id = self.storage.save_capture_batch(batch)
            logger.info("截图批次已入库: ID=%s", batch_id)
        except Exception as exc:
            logger.error("截图批次入库失败: %s", exc)

    def start_recording(self):
        """开始录制"""
        self.recorder.start()
        self._start_idle_detector()

    def stop_recording(self):
        """停止录制"""
        self._stop_idle_detector()
        self.recorder.stop()

    def pause_recording(self):
        """暂停录制（手动）"""
        self._idle_paused = False
        self.recorder.pause()

    def resume_recording(self):
        """恢复录制（手动）"""
        self._idle_paused = False
        self.recorder.resume()

    @property
    def is_recording(self) -> bool:
        return self.recorder.is_recording

    @property
    def is_paused(self) -> bool:
        return self.recorder.is_paused

    @property
    def is_idle_paused(self) -> bool:
        return self._idle_paused

    def _start_idle_detector(self):
        """启动空闲检测"""
        if not config.IDLE_PAUSE_ENABLED:
            return

        try:
            from core.idle_detector import IdleDetector
            timeout = config.IDLE_PAUSE_TIMEOUT_SECONDS
            self._idle_detector = IdleDetector(
                timeout_seconds=timeout,
                check_interval=5,
                on_idle=self._on_idle,
                on_active=self._on_active
            )
            self._idle_detector.start()
        except Exception as e:
            logger.warning(f"空闲检测启动失败: {e}")

    def _stop_idle_detector(self):
        """停止空闲检测"""
        if self._idle_detector:
            try:
                self._idle_detector.stop()
            except Exception as e:
                logger.warning(f"空闲检测停止失败: {e}")
            self._idle_detector = None
        self._idle_paused = False

    def _on_idle(self, idle_seconds: float = 0.0):
        """空闲回调 - 自动暂停录制，并把空闲开始后的截图从有效时长里剔除。"""
        if self.recorder.is_recording and not self.recorder.is_paused:
            try:
                last_input = datetime.now() - timedelta(seconds=max(idle_seconds, 0.0))
            except Exception:
                last_input = None
            self.recorder.pause(trim_end=last_input)
            self._idle_paused = True
            logger.info(f"检测到空闲 {config.IDLE_PAUSE_TIMEOUT_SECONDS} 秒，录制已自动暂停")

    def _on_active(self):
        """活动恢复回调 - 自动恢复录制"""
        if self._idle_paused:
            self.recorder.resume()
            self._idle_paused = False
            logger.info("检测到活动恢复，录制已自动恢复")
