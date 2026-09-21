"""
Dayflow Windows - API 交互层
使用 OpenAI 兼容格式调用心流 API
"""
import asyncio
import base64
import json
import logging
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import List, Optional, Dict
from datetime import datetime
from urllib.parse import unquote

import httpx
import cv2

import config
from core.types import Observation, ActivityCard, AppSite, Distraction

logger = logging.getLogger(__name__)

# Prompts used by the recording-analysis pipeline are deliberately English-only.
# The backend's content audit has rejected otherwise valid Chinese text requests.
TRANSCRIBE_SYSTEM_PROMPT = """You analyze screen activity. Describe the user's specific actions from the screenshots and window metadata.

Return JSON in this format:
{
  "observations": [
    {"start_ts": 0, "end_ts": 10, "text": "Implemented user login logic in Python"}
  ]
}

Rules:
- start_ts and end_ts are relative seconds.
- Observations must start at 0 and cover the complete capture duration. Do not describe a minute as only a few seconds.
- Write every text value in English. Translate any visible non-English content into English; do not quote or reproduce non-English text.
- Describe actions only (what code was written, what content was viewed, or what operation was performed), without application names.
- Use file names, page titles, document names, and conversation context when they improve precision.
- Naturally mention an identifiable file or page being edited or viewed.
- Return JSON only."""

DAILY_REPORT_SYSTEM_PROMPT = """你是专业的个人工作报告生成助手。根据用户一天的活动记录数据，生成每日工作总结。

## 核心原则
1. 基于证据：所有总结必须严格基于提供的活动数据，不得虚构
2. 智能聚合：将相关活动合并分析，避免流水账
3. 价值导向：突出学习成果、完成事项、重要决策
4. 深度分析：不仅记录"做了什么"，更要分析"学到了什么"、"效率如何"
5. 叙述风格：用连贯的段落和自然语言表达

## 输出格式（严格 Markdown）

# 📋 工作日报 - {date}

## 📊 今日概览
用 2-3 句话高度概括今天的工作重点和主要成果。

## ⏱️ 时间分配分析
分析各类活动的时间占比，使用表格或列表呈现。
| 类别 | 时长 | 占比 | 说明 |
|------|------|------|------|

## ✅ 完成事项
列举今天完成的具体任务和成果，每项说明内容和价值。

## ⏰ 时间线回顾
按时间段总结主要活动（合并相关活动，突出重点）。
**上午 (HH:MM - HH:MM)**：主要活动
**下午 (HH:MM - HH:MM)**：主要活动
**晚上 (HH:MM - HH:MM)**：主要活动

## 🎯 工作重点与领域
识别主要投入精力的领域和方向。

## 📈 生产力分析
基于 productivity_score 分析效率趋势、专注时段、分心模式。

## 🔍 自我评估
### 做得好的
值得肯定和保持的方面，用具体事例说明。
### 待改进的
需要优化和提升的方面，给出具体建议。

## 💡 洞察与建议
基于今日数据给出 1-2 条具体可操作的改进建议。

## 质量标准
- 深度 > 广度：宁可深入分析几个重点，也不要泛泛罗列
- 洞察 > 记录：提供有价值的分析和反思
- 具体 > 抽象：用具体事例和细节支撑结论
"""

GENERATE_CARDS_SYSTEM_PROMPT = """You are a time-management assistant. Generate activity cards from the observation records.

Return JSON in this format:
{
  "cards": [
    {
      "category": "Programming",
      "title": "Dayflow project development",
      "summary": "Implemented user login and wrote unit tests",
      "start_time": "2024-01-01T10:00:00",
      "end_time": "2024-01-01T11:30:00",
      "app_sites": [{"name": "VS Code", "duration_seconds": 5400}],
      "distractions": [],
      "productivity_score": 85
    }
  ]
}

Category definitions:
- Programming: coding, debugging, and code review
- Work: documents, email, project management, and design
- Learning: tutorials, documentation, and note-taking
- Meeting: video meetings and voice calls
- Social: messaging and social media
- Entertainment: videos, games, and music
- Break: no apparent activity
- Other: cannot be classified

productivity_score guidelines:
- 90-100: highly focused core work such as programming, writing, or design
- 70-89: regular work such as email, documents, or meetings
- 50-69: inefficient work with frequent switching or fragmented tasks
- 30-49: light entertainment such as browsing or social media
- 0-29: pure entertainment such as games or videos

Merge consecutive observations that use the same application for similar activity.
Split a period when it switches between different activity types.

For continuity across batches, use the previous card hints when the current activity clearly continues the same work.

Write category, title, summary, and distraction descriptions in Simplified Chinese. Keep application, site, product, and file names in their conventional form. Return JSON only."""


class DayflowBackendProvider:
    """
    心流 API 交互类 (OpenAI 兼容格式)
    使用 Chat Completions 接口进行视频分析
    """

    FILE_HINT_BLACKLIST = {
        "visual studio code", "cursor", "google chrome", "microsoft edge", "firefox",
        "wechat", "qq", "telegram", "discord", "notion", "obsidian", "typora",
        "microsoft word", "microsoft excel", "powerpoint", "outlook",
        "文件资源管理器", "windows terminal", "powershell", "cmd", "unknown"
    }
    
    def __init__(
        self,
        api_base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        timeout: float = 120.0,
        provider_mode: Optional[str] = None,
        codex_timeout: Optional[float] = None,
    ):
        self.api_base_url = (api_base_url or config.API_BASE_URL).rstrip("/")
        self.api_key = api_key or config.API_KEY
        self.model = model or config.API_MODEL
        self.timeout = timeout
        self.provider_mode = provider_mode or config.AI_PROVIDER_MODE
        self.codex_timeout = codex_timeout or config.CODEX_EXEC_TIMEOUT_SECONDS
        
        self._client: Optional[httpx.AsyncClient] = None

    @property
    def uses_codex_exec(self) -> bool:
        return self.provider_mode == "codex_exec"

    @staticmethod
    def find_codex_executable() -> Optional[str]:
        """查找官方 Codex CLI，兼容 npm 在 Windows 下的安装路径。"""
        executable = shutil.which("codex")
        if executable:
            return executable

        app_data = Path.home() / "AppData" / "Roaming" / "npm"
        for name in ("codex.cmd", "codex.exe"):
            candidate = app_data / name
            if candidate.exists():
                return str(candidate)
        return None

    def _run_codex_exec(self, prompt: str, images_base64: Optional[List[str]] = None) -> str:
        """通过官方 Codex CLI 执行一次无状态、只读的模型调用。"""
        executable = self.find_codex_executable()
        if not executable:
            raise RuntimeError("未找到 Codex CLI，请先安装并登录 Codex")

        images_base64 = images_base64 or []
        with tempfile.TemporaryDirectory(prefix="dayflow_codex_") as temp_dir:
            temp_path = Path(temp_dir)
            output_path = temp_path / "result.txt"
            image_paths = []
            for index, image_base64 in enumerate(images_base64):
                image_path = temp_path / f"frame_{index:02d}.jpg"
                image_path.write_bytes(base64.b64decode(image_base64))
                image_paths.append(image_path)

            command = [
                executable,
                "exec",
                "-",
                "--ephemeral",
                "--sandbox",
                "read-only",
                "--skip-git-repo-check",
                "--ignore-rules",
                "--color",
                "never",
                "-c",
                'model_reasoning_effort="low"',
                "--output-last-message",
                str(output_path),
            ]
            for image_path in image_paths:
                command.extend(["--image", str(image_path)])

            creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            result = subprocess.run(
                command,
                input=prompt,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=temp_dir,
                timeout=self.codex_timeout,
                creationflags=creation_flags,
                check=False,
            )
            if result.returncode != 0:
                error = (result.stderr or result.stdout or "未知错误").strip()
                raise RuntimeError(f"Codex Exec 失败（退出码 {result.returncode}）: {error[-800:]}")

            response = output_path.read_text(encoding="utf-8").strip() if output_path.exists() else ""
            if not response:
                raise RuntimeError("Codex Exec 未返回结果")
            return response

    async def _codex_completion(self, system_prompt: str, user_prompt: str,
                                images_base64: Optional[List[str]] = None) -> str:
        """在线程中运行 Codex CLI，避免阻塞分析事件循环。"""
        prompt = (
            "Follow the system instructions below exactly. Do not call tools and do not read or modify local files.\n\n"
            f"System instructions:\n{system_prompt}\n\nUser input:\n{user_prompt}"
        )
        return await asyncio.to_thread(self._run_codex_exec, prompt, images_base64)

    @staticmethod
    def _ascii_metadata(value: object, max_length: int = 160) -> str:
        """Return compact ASCII metadata so model requests do not contain CJK text."""
        if value is None:
            return ""
        text = "".join(char if ord(char) < 128 else " " for char in str(value))
        return " ".join(text.split())[:max_length].strip()
    
    @property
    def headers(self) -> dict:
        """请求头"""
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
    
    async def _get_client(self) -> httpx.AsyncClient:
        """获取或创建异步 HTTP 客户端"""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout),
                headers=self.headers
            )
        return self._client
    
    async def close(self):
        """关闭 HTTP 客户端"""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None
    
    def _extract_frames_from_video(self, video_path: str, max_frames: int = 10) -> List[str]:
        """
        从视频中提取关键帧并编码为 base64
        
        Args:
            video_path: 视频文件路径
            max_frames: 最大提取帧数
            
        Returns:
            List[str]: base64 编码的图片列表
        """
        frames_base64 = []
        
        cap = cv2.VideoCapture(video_path)
        if not cap.isOpened():
            logger.error(f"无法打开视频文件: {video_path}")
            return frames_base64
        
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        if total_frames == 0:
            cap.release()
            return frames_base64
        
        # 均匀采样帧
        frame_indices = [int(i * total_frames / max_frames) for i in range(max_frames)]
        
        for idx in frame_indices:
            cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
            ret, frame = cap.read()
            if not ret:
                continue
            
            # 压缩图片以减少传输大小
            frame = cv2.resize(frame, (1280, 720))
            _, buffer = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
            base64_image = base64.b64encode(buffer).decode('utf-8')
            frames_base64.append(base64_image)
        
        cap.release()
        return frames_base64
    
    def _extract_message_content(self, message_content) -> str:
        """兼容不同 OpenAI/Gemini 兼容服务的 message.content 返回格式。"""
        if isinstance(message_content, str):
            return message_content

        if isinstance(message_content, list):
            parts = []
            for item in message_content:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict):
                    # 常见兼容格式：{"type":"text","text":"..."}
                    text = item.get("text")
                    if text:
                        parts.append(text)
            return "\n".join(parts).strip()

        if message_content is None:
            return ""

        return str(message_content)

    @staticmethod
    def _normalize_observation_timestamps(
        observations: List[Observation], duration: float
    ) -> List[Observation]:
        """将模型的相对时间轴确定性映射到 snapshot 的真实时长。"""
        if not observations or duration <= 0:
            return observations

        timeline_start = min(obs.start_ts for obs in observations)
        timeline_end = max(obs.end_ts for obs in observations)
        model_span = timeline_end - timeline_start
        if model_span <= 0:
            observations[0].start_ts = 0
            observations[0].end_ts = duration
            return observations

        for obs in observations:
            normalized_start = (obs.start_ts - timeline_start) / model_span * duration
            normalized_end = (obs.end_ts - timeline_start) / model_span * duration
            obs.start_ts = max(0.0, min(normalized_start, duration))
            obs.end_ts = max(obs.start_ts, min(normalized_end, duration))
        return observations

    def _extract_file_hint(self, window_title: Optional[str], app_name: Optional[str] = None) -> Optional[str]:
        """从窗口标题中提取较像“文件名/页面标题/文档名”的线索。"""
        if not window_title:
            return None

        title = unquote(str(window_title)).strip()
        if not title:
            return None

        # 常见编辑器/浏览器标题分隔符
        candidates = [seg.strip(" -—_|•·[]()") for seg in re.split(r"\s*[\-|—|_|·|•|:：]\s*", title) if seg.strip()]
        if not candidates:
            candidates = [title]

        app_name_norm = (app_name or "").strip().lower()

        scored = []
        for part in candidates:
            part_norm = part.strip().lower()
            if not part_norm:
                continue
            if len(part_norm) <= 1:
                continue
            if part_norm == app_name_norm:
                continue
            if part_norm in self.FILE_HINT_BLACKLIST:
                continue

            score = 0
            if re.search(r"\.[a-z0-9]{1,8}$", part_norm):
                score += 4  # 像文件名
            if any(ch in part for ch in ('/', '\\')):
                score += 3  # 像路径
            if re.search(r"[\u4e00-\u9fffA-Za-z0-9].{2,}", part):
                score += 1
            if len(part) >= 6:
                score += 1
            if 'github' in app_name_norm or 'code' in app_name_norm or 'cursor' in app_name_norm:
                score += 1

            scored.append((score, part.strip()))

        if not scored:
            return None

        best_score, best_part = max(scored, key=lambda x: x[0])
        if best_score < 2:
            return None

        return best_part[:200]

    async def _chat_completion(
        self,
        messages: List[dict],
        temperature: float = 0.3,
        max_tokens: int = 4096,
    ) -> str:
        """
        调用 Chat Completions API
        
        Args:
            messages: 消息列表
            temperature: 温度参数
            
        Returns:
            str: 模型返回的内容
        """
        client = await self._get_client()
        
        request_body = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        
        try:
            response = await client.post(
                f"{self.api_base_url}/chat/completions",
                json=request_body
            )
            response.raise_for_status()
            
            result = response.json()
            choices = result.get("choices") or []
            if not choices:
                raise ValueError(f"响应中缺少 choices: {result}")

            message = choices[0].get("message") or {}
            content = self._extract_message_content(message.get("content"))
            if content:
                return content

            # 兼容部分服务把文本放在顶层 text / output_text
            fallback_text = choices[0].get("text") or result.get("output_text") or ""
            if fallback_text:
                return fallback_text

            raise ValueError(f"无法从响应中提取文本内容: {result}")
            
        except httpx.HTTPStatusError as e:
            logger.error(f"API 请求失败: {e.response.status_code} - {e.response.text}")
            raise
        except Exception as e:
            logger.error(f"API 请求异常: {e}")
            raise

    async def generate_text(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.7,
        max_tokens: int = 4096,
    ) -> str:
        """使用当前分析方式生成纯文本内容。"""
        if self.uses_codex_exec:
            return await self._codex_completion(system_prompt, user_prompt)

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        return await self._chat_completion(
            messages,
            temperature=temperature,
            max_tokens=max_tokens,
        )
    
    async def transcribe_video(
        self,
        video_path: str,
        duration: float,
        prompt: Optional[str] = None,
        window_records: Optional[List[Dict]] = None
    ) -> List[Observation]:
        """
        分析视频切片，获取观察记录
        
        Args:
            video_path: 视频文件路径
            duration: 视频时长（秒）
            prompt: 额外提示词（可选）
            window_records: 窗口记录列表（可选）
            
        Returns:
            List[Observation]: 观察记录列表
        """
        video_file = Path(video_path)
        if not video_file.exists():
            raise FileNotFoundError(f"视频文件不存在: {video_path}")
        
        # 提取视频帧
        frames = self._extract_frames_from_video(video_path, max_frames=8)
        if not frames:
            logger.warning(f"无法从视频提取帧: {video_path}")
            return []
        
        # Build an ASCII-only window timeline for the model request.
        window_info_text = ""
        if window_records:
            window_info_text = "\n\nWindow timeline:\n"
            # 按时间段聚合相同的应用
            current_app = None
            current_title = None
            current_start = 0
            for record in window_records:
                app_name = record.get("app_name", "Unknown")
                window_title = record.get("window_title", "")
                if app_name != current_app or window_title != current_title:
                    if current_app:
                        metadata = " | ".join(filter(None, (
                            self._ascii_metadata(current_app, 80),
                            self._ascii_metadata(current_title, 160),
                        ))) or "Unknown"
                        window_info_text += f"- [{current_start:.0f}s - {record['timestamp']:.0f}s] {metadata}\n"
                    current_app = app_name
                    current_title = window_title
                    current_start = record.get("timestamp", 0)
            # 添加最后一个
            if current_app:
                metadata = " | ".join(filter(None, (
                    self._ascii_metadata(current_app, 80),
                    self._ascii_metadata(current_title, 160),
                ))) or "Unknown"
                window_info_text += f"- [{current_start:.0f}s - {duration:.0f}s] {metadata}\n"
        
        safe_prompt = self._ascii_metadata(prompt, 500)
        user_prompt = (
            f"Analyze these {len(frames)} key frames from a {duration:.0f}-second screen recording. "
            "The frames are sampled uniformly in chronological order. "
            f"Observation records must cover 0 through {duration:.0f} seconds and be written in English."
            f"{window_info_text}"
            + (f"\nAdditional instructions: {safe_prompt}" if safe_prompt else "")
        )

        # 构建 API 消息内容（包含多张图片）
        content = [{"type": "text", "text": user_prompt}]
        
        for i, frame_base64 in enumerate(frames):
            content.append({
                "type": "image_url",
                "image_url": {
                    "url": f"data:image/jpeg;base64,{frame_base64}",
                    "detail": "low"
                }
            })
        
        messages = [
            {"role": "system", "content": TRANSCRIBE_SYSTEM_PROMPT},
            {"role": "user", "content": content}
        ]
        
        try:
            if self.uses_codex_exec:
                response_text = await self._codex_completion(
                    TRANSCRIBE_SYSTEM_PROMPT,
                    user_prompt,
                    frames,
                )
            else:
                response_text = await self._chat_completion(messages)
            observations = self._parse_observations_from_text(response_text, duration)
            observations = self._normalize_observation_timestamps(observations, duration)
            
            # 后处理：用真实窗口信息覆盖 AI 返回的 app_name
            if window_records and observations:
                observations = self._apply_window_records(observations, window_records, duration)
            
            return observations
        except Exception as e:
            logger.error(f"视频分析失败: {e}")
            raise

    async def transcribe_capture_images(
        self,
        directory_path: str,
        image_records: List[Dict],
        duration: float,
        prompt: Optional[str] = None,
        window_records: Optional[List[Dict]] = None,
        max_images: int = 12,
    ) -> List[Observation]:
        """直接分析截图批次，避免先编码为 MP4 再解码。"""
        directory = Path(directory_path)
        valid = []
        for record in image_records:
            path = directory / record.get("file", "")
            if path.exists():
                item = dict(record)
                item["relative_seconds"] = float(item.get("relative_seconds", 0))
                valid.append((item["relative_seconds"], path, item))
        if not valid:
            raise FileNotFoundError(f"截图批次没有可用图片: {directory_path}")

        limit = max(1, int(max_images or len(valid)))
        if len(valid) > limit:
            indices = [int(i * len(valid) / limit) for i in range(limit)]
            selected = [valid[i] for i in indices]
        else:
            selected = valid

        frames = []
        selected_records = []
        for relative_seconds, path, record in selected:
            image = cv2.imread(str(path))
            if image is None:
                continue
            if config.CAPTURE_RESIZE_WIDTH and config.CAPTURE_RESIZE_HEIGHT:
                image = cv2.resize(image, (config.CAPTURE_RESIZE_WIDTH, config.CAPTURE_RESIZE_HEIGHT))
            _, buffer = cv2.imencode('.jpg', image, [cv2.IMWRITE_JPEG_QUALITY, config.CAPTURE_JPEG_QUALITY])
            frames.append(base64.b64encode(buffer).decode('utf-8'))
            selected_records.append((relative_seconds, record))
        if not frames:
            raise RuntimeError(f"无法读取截图批次: {directory_path}")

        timeline = []
        for _, record in selected_records:
            metadata = " | ".join(filter(None, (
                self._ascii_metadata(record.get("app_name"), 80),
                self._ascii_metadata(record.get("window_title"), 160),
            ))) or "Unknown"
            timeline.append(f"- [{record.get('relative_seconds', 0):.0f}s] {metadata}")
        safe_prompt = self._ascii_metadata(prompt, 500)
        user_prompt = (
            f"Analyze this {duration:.0f}-second screenshot batch. It contains {len(frames)} "
            "screenshots in chronological order.\n"
            f"The observation records must cover 0 through {duration:.0f} seconds. "
            "Write observations in English and translate visible non-English content instead of quoting it.\n"
            "Screenshot timeline:\n"
            + "\n".join(timeline)
            + (f"\nAdditional instructions: {safe_prompt}" if safe_prompt else "")
        )
        content = [{"type": "text", "text": user_prompt}]
        for frame in frames:
            content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{frame}", "detail": "low"}})
        messages = [
            {"role": "system", "content": TRANSCRIBE_SYSTEM_PROMPT},
            {"role": "user", "content": content},
        ]
        if self.uses_codex_exec:
            response_text = await self._codex_completion(TRANSCRIBE_SYSTEM_PROMPT, user_prompt, frames)
        else:
            response_text = await self._chat_completion(messages)
        observations = self._parse_observations_from_text(response_text, duration)
        observations = self._normalize_observation_timestamps(observations, duration)
        records = [
            {**record, "timestamp": record.get("relative_seconds", 0)}
            for record in (window_records or image_records)
        ]
        if records and observations:
            observations = self._apply_window_records(observations, records, duration)
        return observations
    
    def _apply_window_records(
        self, 
        observations: List[Observation], 
        window_records: List[Dict],
        duration: float
    ) -> List[Observation]:
        """
        用真实窗口记录覆盖 AI 返回的 app_name
        
        根据时间戳匹配，找到每个 observation 对应时间段内使用最多的应用
        """
        if not window_records:
            return observations
        
        # 预处理：构建时间段到应用的映射
        # 格式: [(start_ts, end_ts, app_name, window_title), ...]
        time_segments = []
        current_app = None
        current_title = None
        current_start = 0
        
        for record in window_records:
            app_name = record.get("app_name", "Unknown")
            window_title = record.get("window_title", "")
            timestamp = record.get("timestamp", 0)
            
            if app_name != current_app:
                if current_app:
                    time_segments.append((current_start, timestamp, current_app, current_title))
                current_app = app_name
                current_title = window_title
                current_start = timestamp
        
        # 添加最后一个时间段
        if current_app:
            time_segments.append((current_start, duration, current_app, current_title))
        
        # 为每个 observation 找到对应的应用
        for obs in observations:
            obs_start = obs.start_ts
            obs_end = obs.end_ts
            
            # 统计这个时间段内各应用的占用时长
            app_durations: Dict[str, float] = {}
            app_titles: Dict[str, str] = {}
            
            for seg_start, seg_end, app_name, window_title in time_segments:
                # 计算重叠时间
                overlap_start = max(obs_start, seg_start)
                overlap_end = min(obs_end, seg_end)
                
                if overlap_end > overlap_start:
                    overlap_duration = overlap_end - overlap_start
                    app_durations[app_name] = app_durations.get(app_name, 0) + overlap_duration
                    if app_name not in app_titles:
                        app_titles[app_name] = window_title
            
            # 找到占用时间最长的应用
            if app_durations:
                main_app = max(app_durations, key=app_durations.get)
                obs.app_name = main_app
                obs.window_title = app_titles.get(main_app, obs.window_title)
                obs.file_hint = self._extract_file_hint(obs.window_title, main_app)
                logger.debug(f"后处理: [{obs_start:.0f}s-{obs_end:.0f}s] app_name -> {main_app}, file_hint -> {obs.file_hint}")
        
        return observations
    
    async def generate_activity_cards(
        self,
        observations: List[Observation],
        context_cards: Optional[List[ActivityCard]] = None,
        start_time: Optional[datetime] = None,
        prompt: Optional[str] = None
    ) -> List[ActivityCard]:
        """
        根据观察记录生成时间轴卡片
        
        Args:
            observations: 观察记录列表
            context_cards: 前序卡片（用于上下文）
            start_time: 开始时间
            prompt: 额外提示词（可选）
            
        Returns:
            List[ActivityCard]: 活动卡片列表
        """
        if not observations:
            return []
        
        # Build an English-only text payload for the second model request.
        obs_text = "Observation records:\n"
        for obs in observations:
            observation = self._ascii_metadata(obs.text, 500) or "Activity visible in screenshots"
            obs_text += f"- [{obs.start_ts:.0f}s - {obs.end_ts:.0f}s] {observation}"
            extras = []
            app_name = self._ascii_metadata(obs.app_name, 80)
            file_hint = self._ascii_metadata(obs.file_hint, 120)
            window_title = self._ascii_metadata(obs.window_title, 120)
            if app_name:
                extras.append(f"Application: {app_name}")
            if file_hint:
                extras.append(f"File or page hint: {file_hint}")
            elif window_title:
                extras.append(f"Window title: {window_title}")
            if extras:
                obs_text += f" ({'; '.join(extras)})"
            obs_text += "\n"
        
        # 添加时间上下文
        if start_time:
            obs_text += f"\nCapture start time: {start_time.isoformat()}"
        
        # 添加前序卡片上下文
        if context_cards:
            safe_context = []
            for card in context_cards[-3:]:
                category = self._ascii_metadata(card.category, 80)
                title = self._ascii_metadata(card.title, 160)
                hint = ": ".join(filter(None, (category, title)))
                if hint:
                    safe_context.append(f"- {hint}")
            if safe_context:
                obs_text += "\n\nPrevious activity card hints:\n" + "\n".join(safe_context) + "\n"
        
        safe_prompt = self._ascii_metadata(prompt, 500)
        if safe_prompt:
            obs_text += f"\nAdditional instructions: {safe_prompt}"
        
        messages = [
            {"role": "system", "content": GENERATE_CARDS_SYSTEM_PROMPT},
            {"role": "user", "content": obs_text}
        ]
        
        try:
            if self.uses_codex_exec:
                response_text = await self._codex_completion(
                    GENERATE_CARDS_SYSTEM_PROMPT,
                    obs_text,
                )
            else:
                response_text = await self._chat_completion(messages)
            return self._parse_cards_from_text(response_text, start_time)
        except Exception as e:
            logger.error(f"卡片生成失败: {e}")
            raise

    async def generate_daily_report(
        self,
        cards: List[ActivityCard],
        date_str: str
    ) -> str:
        """
        根据活动卡片列表生成每日工作报告（Markdown 格式）

        Args:
            cards: 当日活动卡片列表
            date_str: 日期字符串（如 "2026-05-15"）

        Returns:
            str: Markdown 格式的日报内容
        """
        if not cards:
            return f"# 📋 工作日报 - {date_str}\n\n当日无活动记录数据。"

        # 构建活动数据文本
        data_text = f"日期：{date_str}\n\n活动记录（共 {len(cards)} 条）：\n"

        total_minutes = 0
        categories = {}
        for i, card in enumerate(cards, 1):
            start = card.start_time.strftime("%H:%M") if card.start_time else "??:??"
            end = card.end_time.strftime("%H:%M") if card.end_time else "??:??"
            dur = card.duration_minutes
            total_minutes += dur

            cat = card.category or "未分类"
            categories[cat] = categories.get(cat, 0) + dur

            apps = ", ".join(a.name for a in card.app_sites[:3]) if card.app_sites else ""
            distractions = len(card.distractions)

            data_text += (
                f"\n{i}. [{start}-{end}] {cat} - {card.title}\n"
                f"   摘要：{card.summary}\n"
                f"   时长：{dur:.0f}分钟 | 生产力评分：{card.productivity_score:.0f}/100\n"
            )
            if apps:
                data_text += f"   应用：{apps}\n"
            if distractions > 0:
                data_text += f"   分心次数：{distractions}\n"

        # 添加统计摘要
        data_text += f"\n总计记录时长：{total_minutes:.0f} 分钟\n"
        data_text += "类别分布：\n"
        for cat, mins in sorted(categories.items(), key=lambda x: -x[1]):
            pct = mins / total_minutes * 100 if total_minutes > 0 else 0
            data_text += f"  - {cat}: {mins:.0f}分钟 ({pct:.0f}%)\n"

        messages = [
            {"role": "system", "content": DAILY_REPORT_SYSTEM_PROMPT},
            {"role": "user", "content": data_text}
        ]

        try:
            if self.uses_codex_exec:
                return await self._codex_completion(
                    DAILY_REPORT_SYSTEM_PROMPT,
                    data_text,
                )
            return await self._chat_completion(messages, temperature=0.7)
        except Exception as e:
            logger.error(f"日报生成失败: {e}")
            raise
    
    def _parse_observations_from_text(self, text: str, duration: float) -> List[Observation]:
        """从文本响应中解析观察记录"""
        observations = []
        
        try:
            # 尝试提取 JSON
            json_match = re.search(r'\{[\s\S]*\}', text)
            if json_match:
                data = json.loads(json_match.group())
                items = data.get("observations", [])
                
                for item in items:
                    obs = Observation(
                        start_ts=float(item.get("start_ts", 0)),
                        end_ts=float(item.get("end_ts", duration)),
                        text=item.get("text", ""),
                        app_name=item.get("app_name"),
                        window_title=item.get("window_title"),
                        file_hint=item.get("file_hint")
                    )
                    observations.append(obs)
        except json.JSONDecodeError as e:
            logger.warning(f"JSON 解析失败: {e}, 原文: {text[:200]}")
            # 如果 JSON 解析失败，创建一个基于整段文本的观察记录
            observations.append(Observation(
                start_ts=0,
                end_ts=duration,
                text=text[:500]
            ))
        
        return observations
    
    def _parse_cards_from_text(self, text: str, start_time: Optional[datetime]) -> List[ActivityCard]:
        """从文本响应中解析活动卡片"""
        cards = []

        def decode_json_string(value):
            for _ in range(2):
                if not isinstance(value, str):
                    break
                stripped = value.strip()
                if not stripped or stripped[0] not in "[{":
                    break
                try:
                    value = json.loads(stripped)
                except json.JSONDecodeError:
                    break
            return value
        
        try:
            json_match = re.search(r'\{[\s\S]*\}', text)
            if json_match:
                data = decode_json_string(json.loads(json_match.group()))
                if isinstance(data, dict):
                    items = decode_json_string(data.get("cards", []))
                elif isinstance(data, list):
                    items = data
                else:
                    items = []

                if isinstance(items, dict):
                    items = [items]
                if not isinstance(items, list):
                    logger.warning("卡片字段不是数组: %s", type(items).__name__)
                    return cards
                
                for raw_item in items:
                    item = decode_json_string(raw_item)
                    if not isinstance(item, dict):
                        logger.warning("跳过无法解析的卡片项: %s", str(raw_item)[:120])
                        continue

                    # 解析时间
                    card_start = None
                    card_end = None
                    
                    if item.get("start_time"):
                        try:
                            card_start = datetime.fromisoformat(item["start_time"].replace("Z", "+00:00"))
                        except:
                            card_start = start_time
                    else:
                        card_start = start_time
                    
                    if item.get("end_time"):
                        try:
                            card_end = datetime.fromisoformat(item["end_time"].replace("Z", "+00:00"))
                        except:
                            pass
                    
                    # 解析应用列表
                    app_sites = []
                    raw_apps = decode_json_string(item.get("app_sites", []))
                    if isinstance(raw_apps, list):
                        for raw_app in raw_apps:
                            app = decode_json_string(raw_app)
                            if isinstance(app, dict):
                                app_sites.append(AppSite(
                                    name=app.get("name", ""),
                                    duration_seconds=app.get("duration_seconds", 0)
                                ))
                            elif isinstance(app, str) and app.strip():
                                app_sites.append(AppSite(name=app.strip()))
                    
                    # 解析分心记录
                    distractions = []
                    raw_distractions = decode_json_string(item.get("distractions", []))
                    if isinstance(raw_distractions, list):
                        for raw_dist in raw_distractions:
                            dist = decode_json_string(raw_dist)
                            if isinstance(dist, dict):
                                distractions.append(Distraction(
                                    description=dist.get("description", ""),
                                    timestamp=dist.get("timestamp", 0),
                                    duration_seconds=dist.get("duration_seconds", 0)
                                ))
                            elif isinstance(dist, str) and dist.strip():
                                distractions.append(Distraction(
                                    description=dist.strip(),
                                    timestamp=0,
                                ))
                    
                    card = ActivityCard(
                        category=item.get("category", "其他"),
                        title=item.get("title", "未命名活动"),
                        summary=item.get("summary", ""),
                        start_time=card_start,
                        end_time=card_end,
                        app_sites=app_sites,
                        distractions=distractions,
                        productivity_score=float(item.get("productivity_score", 0))
                    )
                    cards.append(card)
                    
        except (json.JSONDecodeError, TypeError, ValueError) as e:
            logger.warning(f"卡片 JSON 解析失败: {e}")
        
        return cards
    
    async def health_check(self) -> bool:
        """检查当前分析方式是否可用。"""
        try:
            if self.uses_codex_exec:
                await self._codex_completion("只回复 OK。", "连接测试")
            else:
                messages = [{"role": "user", "content": "hi"}]
                await self._chat_completion(messages)
            return True
        except Exception as e:
            logger.warning(f"API 健康检查失败: {e}")
            return False
    
    async def test_connection(self) -> tuple[bool, str]:
        """
        测试 API 连接
        
        Returns:
            tuple[bool, str]: (是否成功, 消息)
        """
        if not self.uses_codex_exec and not self.api_key:
            return False, "API Key 未配置"
        
        try:
            if self.uses_codex_exec:
                response = await self._codex_completion("只回复‘测试成功’。", "连接测试")
                return True, f"Codex Exec 可用，已继承本机 Codex 配置\n回复: {response[:100]}"

            messages = [{"role": "user", "content": "你好，请回复'测试成功'"}]
            response = await self._chat_completion(messages)
            return True, f"连接成功！模型: {self.model}\n回复: {response[:100]}"
        except httpx.HTTPStatusError as e:
            return False, f"HTTP 错误 {e.response.status_code}: {e.response.text[:200]}"
        except httpx.ConnectError:
            return False, "连接失败：无法连接到服务器"
        except httpx.TimeoutException:
            return False, "连接超时"
        except Exception as e:
            return False, f"错误: {str(e)}"


# 便捷函数：同步调用
def transcribe_video_sync(video_path: str, duration: float, **kwargs) -> List[Observation]:
    """同步版本的视频分析"""
    loop = asyncio.new_event_loop()
    provider = DayflowBackendProvider(**kwargs)
    try:
        return loop.run_until_complete(provider.transcribe_video(video_path, duration))
    finally:
        loop.run_until_complete(provider.close())
        loop.close()


def generate_cards_sync(
    observations: List[Observation],
    context_cards: Optional[List[ActivityCard]] = None,
    **kwargs
) -> List[ActivityCard]:
    """同步版本的卡片生成"""
    loop = asyncio.new_event_loop()
    provider = DayflowBackendProvider(**kwargs)
    try:
        return loop.run_until_complete(provider.generate_activity_cards(observations, context_cards))
    finally:
        loop.run_until_complete(provider.close())
        loop.close()


def generate_daily_report_sync(
    cards: List[ActivityCard],
    date_str: str,
    **kwargs
) -> str:
    """同步版本的日报生成"""
    loop = asyncio.new_event_loop()
    provider = DayflowBackendProvider(**kwargs)
    try:
        return loop.run_until_complete(provider.generate_daily_report(cards, date_str))
    finally:
        loop.run_until_complete(provider.close())
        loop.close()


def generate_text_sync(
    system_prompt: str,
    user_prompt: str,
    temperature: float = 0.7,
    max_tokens: int = 4096,
    **kwargs
) -> str:
    """同步版本的通用文本生成。"""
    loop = asyncio.new_event_loop()
    provider = DayflowBackendProvider(**kwargs)
    try:
        return loop.run_until_complete(provider.generate_text(
            system_prompt,
            user_prompt,
            temperature=temperature,
            max_tokens=max_tokens,
        ))
    finally:
        loop.run_until_complete(provider.close())
        loop.close()
