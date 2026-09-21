import asyncio
import base64
import json
import subprocess
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import config
from core.llm_provider import DayflowBackendProvider
from core.email_service import AICommentGenerator
from core.types import ActivityCard, Observation
from ui.main_window import MainWindow, SettingsPanel


def test_codex_exec_uses_ephemeral_read_only_images():
    provider = DayflowBackendProvider(provider_mode="codex_exec")
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        output_index = command.index("--output-last-message") + 1
        Path(command[output_index]).write_text('{"ok":true}', encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    image = base64.b64encode(b"jpeg-data").decode("ascii")
    with patch.object(provider, "find_codex_executable", return_value="codex.cmd"), \
         patch("core.llm_provider.subprocess.run", side_effect=fake_run):
        result = provider._run_codex_exec("测试提示", [image])

    command = captured["command"]
    assert result == '{"ok":true}'
    assert command[:3] == ["codex.cmd", "exec", "-"]
    assert "--ephemeral" in command
    assert command[command.index("--sandbox") + 1] == "read-only"
    assert command[command.index("--image") + 1].endswith("frame_00.jpg")
    assert captured["kwargs"]["input"] == "测试提示"
    assert captured["kwargs"]["check"] is False


def test_codex_connection_does_not_require_api_key():
    provider = DayflowBackendProvider(
        provider_mode="codex_exec",
        api_key="",
    )
    provider._codex_completion = AsyncMock(return_value="测试成功")

    success, message = asyncio.run(provider.test_connection())

    assert success is True
    assert "Codex Exec 可用" in message


def test_generic_text_generation_uses_codex_exec():
    provider = DayflowBackendProvider(provider_mode="codex_exec", api_key="")
    provider._codex_completion = AsyncMock(return_value="Codex 文本结果")

    result = asyncio.run(provider.generate_text("系统要求", "用户内容"))

    assert result == "Codex 文本结果"
    provider._codex_completion.assert_awaited_once_with("系统要求", "用户内容")


def test_generic_text_generation_preserves_api_options():
    provider = DayflowBackendProvider(provider_mode="api", api_key="key")
    provider._chat_completion = AsyncMock(return_value="API 文本结果")

    result = asyncio.run(provider.generate_text(
        "系统要求",
        "用户内容",
        temperature=0.6,
        max_tokens=321,
    ))

    assert result == "API 文本结果"
    provider._chat_completion.assert_awaited_once_with(
        [
            {"role": "system", "content": "系统要求"},
            {"role": "user", "content": "用户内容"},
        ],
        temperature=0.6,
        max_tokens=321,
    )


def test_codex_wrapper_uses_english_headers():
    provider = DayflowBackendProvider(provider_mode="codex_exec")
    provider._run_codex_exec = Mock(return_value='{"ok":true}')

    asyncio.run(provider._codex_completion("Return JSON only.", "Analyze this activity."))

    sent_prompt = provider._run_codex_exec.call_args.args[0]
    assert "System instructions:" in sent_prompt
    assert "User input:" in sent_prompt
    assert "系统要求" not in sent_prompt
    assert sent_prompt.isascii()


def test_screenshot_request_filters_non_ascii_metadata(tmp_path):
    provider = DayflowBackendProvider(provider_mode="codex_exec")
    provider._codex_completion = AsyncMock(return_value=(
        '{"observations":[{"start_ts":0,"end_ts":60,'
        '"text":"Reviewed a design document"}]}'
    ))
    image_path = tmp_path / "frame.jpg"
    image_path.write_bytes(b"placeholder")
    records = [{
        "file": image_path.name,
        "relative_seconds": 0,
        "app_name": "飞书 Edge",
        "window_title": "系统设计 specification",
    }]

    with patch("core.llm_provider.cv2.imread", return_value=Mock()), \
         patch("core.llm_provider.cv2.resize", return_value=Mock()), \
         patch("core.llm_provider.cv2.imencode", return_value=(True, bytearray(b"jpeg"))):
        asyncio.run(provider.transcribe_capture_images(
            str(tmp_path), records, 60, prompt="请详细分析 be precise"
        ))

    system_prompt, user_prompt, _ = provider._codex_completion.call_args.args
    assert system_prompt.isascii()
    assert user_prompt.isascii()
    assert "Edge" in user_prompt
    assert "specification" in user_prompt
    assert "飞书" not in user_prompt
    assert "系统设计" not in user_prompt


def test_card_request_is_english_only_but_accepts_chinese_output():
    provider = DayflowBackendProvider(provider_mode="codex_exec")
    provider._codex_completion = AsyncMock(return_value='''
        {"cards":[{
            "category":"工作",
            "title":"检查设计文档",
            "summary":"核对系统功能与设计规格",
            "start_time":"2026-08-08T10:00:00",
            "end_time":"2026-08-08T10:01:00",
            "app_sites":[],
            "distractions":[],
            "productivity_score":80
        }]}
    ''')
    observations = [Observation(
        start_ts=0,
        end_ts=60,
        text="Reviewed the system specification 系统规格",
        app_name="飞书 Microsoft Edge",
        window_title="设计文档 - Work",
    )]
    context = [ActivityCard(category="工作", title="整理旧方案 legacy plan")]

    cards = asyncio.run(provider.generate_activity_cards(
        observations,
        context_cards=context,
        start_time=datetime(2026, 8, 8, 10, 0),
        prompt="请合并相似活动 merge related activity",
    ))

    system_prompt, user_prompt = provider._codex_completion.call_args.args
    assert system_prompt.isascii()
    assert user_prompt.isascii()
    assert "Reviewed the system specification" in user_prompt
    assert "Microsoft Edge" in user_prompt
    assert "legacy plan" in user_prompt
    assert cards[0].title == "检查设计文档"


def test_codex_card_generation_uses_existing_json_parser():
    provider = DayflowBackendProvider(provider_mode="codex_exec")
    provider._codex_completion = AsyncMock(return_value='''
        {"cards":[{
            "category":"编程",
            "title":"实现 Codex 模式",
            "summary":"接入本机 Codex CLI",
            "start_time":"2026-08-08T10:00:00",
            "end_time":"2026-08-08T10:01:00",
            "app_sites":[],
            "distractions":[],
            "productivity_score":90
        }]}
    ''')

    cards = asyncio.run(provider.generate_activity_cards([
        Observation(start_ts=0, end_ts=60, text="编写 Codex 接入代码")
    ]))

    assert len(cards) == 1
    assert cards[0].title == "实现 Codex 模式"
    assert cards[0].duration_minutes == 1


def test_card_parser_accepts_codex_stringified_items():
    provider = DayflowBackendProvider(provider_mode="codex_exec")
    response = json.dumps({
        "cards": [json.dumps({
            "category": "编程",
            "title": "修复分析队列",
            "summary": "恢复中断任务",
            "start_time": "2026-08-08T15:00:00",
            "end_time": "2026-08-08T15:10:00",
            "app_sites": ["Visual Studio Code"],
            "distractions": ["查看通知"],
            "productivity_score": 88,
        }, ensure_ascii=False)]
    }, ensure_ascii=False)

    cards = provider._parse_cards_from_text(response, None)

    assert len(cards) == 1
    assert cards[0].title == "修复分析队列"
    assert cards[0].app_sites[0].name == "Visual Studio Code"
    assert cards[0].distractions[0].description == "查看通知"


def test_card_parser_skips_unstructured_string_items():
    provider = DayflowBackendProvider(provider_mode="codex_exec")

    cards = provider._parse_cards_from_text(
        '{"cards":["not a card", {"title":"有效卡片","category":"其他"}]}',
        datetime(2026, 8, 8, 15, 0),
    )

    assert len(cards) == 1
    assert cards[0].title == "有效卡片"


def test_observation_timestamps_are_scaled_to_real_snapshot_duration():
    observations = [
        Observation(start_ts=0, end_ts=2, text="阅读文章"),
        Observation(start_ts=2, end_ts=3, text="查看代码"),
        Observation(start_ts=3, end_ts=4, text="打开文件"),
    ]

    normalized = DayflowBackendProvider._normalize_observation_timestamps(
        observations, 60
    )

    assert [(item.start_ts, item.end_ts) for item in normalized] == [
        (0, 30),
        (30, 45),
        (45, 60),
    ]


class _FakeWidget:
    def __init__(self, checked=False, text=""):
        self.checked = checked
        self.text_value = text
        self.visible = True

    def isChecked(self):
        return self.checked

    def text(self):
        return self.text_value

    def setVisible(self, visible):
        self.visible = visible

    def hide(self):
        self.visible = False


def _fake_settings_panel(uses_codex=True):
    api_widgets = [_FakeWidget() for _ in range(7)]
    return SimpleNamespace(
        codex_mode_btn=_FakeWidget(checked=uses_codex),
        api_desc=api_widgets[0],
        codex_desc=_FakeWidget(),
        api_url_label=api_widgets[1],
        api_url_input=api_widgets[2],
        api_key_label=api_widgets[3],
        api_key_input=api_widgets[4],
        api_model_label=api_widgets[5],
        api_model_input=api_widgets[6],
        save_btn=_FakeWidget(),
    )


def test_codex_mode_hides_api_configuration_and_save_button():
    panel = _fake_settings_panel(uses_codex=True)

    SettingsPanel._update_ai_mode(panel)

    assert panel.codex_desc.visible is True
    assert panel.save_btn.visible is False
    assert all(not widget.visible for widget in (
        panel.api_desc,
        panel.api_url_label,
        panel.api_url_input,
        panel.api_key_label,
        panel.api_key_input,
        panel.api_model_label,
        panel.api_model_input,
    ))


def test_provider_mode_selection_is_saved_immediately():
    panel = _fake_settings_panel(uses_codex=True)
    panel.api_key_input.text_value = "preserved-key"
    panel.test_result_label = _FakeWidget()
    panel.storage = SimpleNamespace(set_setting=Mock())
    panel.api_key_saved = SimpleNamespace(emit=Mock())
    panel._update_ai_mode = lambda: SettingsPanel._update_ai_mode(panel)
    original_mode = config.AI_PROVIDER_MODE

    try:
        SettingsPanel._on_ai_mode_changed(panel)
        assert config.AI_PROVIDER_MODE == "codex_exec"
        panel.storage.set_setting.assert_called_once_with("ai_provider_mode", "codex_exec")
        panel.api_key_saved.emit.assert_called_once_with("preserved-key")
    finally:
        config.AI_PROVIDER_MODE = original_mode


class _SettingsStorage:
    def __init__(self, values):
        self.values = values

    def get_setting(self, key, default=""):
        return self.values.get(key, default)


def _email_report_data():
    stats = {
        "date": "2026年08月08日",
        "recorded_minutes": 60,
        "score": 80,
        "categories": [],
    }
    deep_analysis = {
        "focus": {"has_data": False},
        "rhythm": {"has_data": False},
        "switching": {"has_data": False},
        "categories": {"has_data": False},
        "day_type": {"type": "常规日", "indicators": "节奏正常"},
    }
    return stats, deep_analysis


def test_email_ai_content_uses_codex_without_api_key():
    storage = _SettingsStorage({
        "ai_provider_mode": "codex_exec",
        "api_key": "",
        "api_url": "preserved-url",
        "api_model": "preserved-model",
    })
    generator = AICommentGenerator(storage)
    generator._call_model_sync = Mock(side_effect=["Codex 点评", "Codex 深度分析"])
    stats, deep_analysis = _email_report_data()

    comment = generator.generate_comment(stats, deep_analysis)
    analysis = generator.generate_deep_analysis(stats, deep_analysis)

    assert comment == "Codex 点评"
    assert analysis == "Codex 深度分析"
    assert generator._call_model_sync.call_count == 2
    settings = generator._call_model_sync.call_args_list[0].args[1]
    assert settings == {
        "api_base_url": "preserved-url",
        "api_key": "",
        "model": "preserved-model",
        "provider_mode": "codex_exec",
    }


def test_auto_daily_report_codex_mode_is_not_blocked_by_missing_key():
    storage = Mock()
    storage.get_daily_report.return_value = None
    storage.get_cards_for_date.return_value = [Mock()]
    storage.get_setting.side_effect = lambda key, default="": {
        "ai_provider_mode": "codex_exec",
        "api_key": "",
    }.get(key, default)
    window = SimpleNamespace(storage=storage)

    with patch("threading.Thread") as thread:
        MainWindow._auto_generate_yesterday_report(window)

    thread.assert_called_once()
    assert thread.call_args.kwargs["daemon"] is True


def test_auto_daily_report_api_mode_still_requires_key():
    storage = Mock()
    storage.get_daily_report.return_value = None
    storage.get_cards_for_date.return_value = [Mock()]
    storage.get_setting.side_effect = lambda key, default="": {
        "ai_provider_mode": "api",
        "api_key": "",
    }.get(key, default)
    window = SimpleNamespace(storage=storage)

    with patch("threading.Thread") as thread:
        MainWindow._auto_generate_yesterday_report(window)

    thread.assert_not_called()


def test_backlog_starts_analysis_without_recording():
    window = SimpleNamespace(
        storage=SimpleNamespace(
            get_pending_chunks=Mock(return_value=[Mock()]),
            get_pending_capture_batches=Mock(return_value=[]),
        ),
        _start_analysis=Mock(),
    )
    original_mode = config.AI_PROVIDER_MODE
    original_key = config.API_KEY

    try:
        config.AI_PROVIDER_MODE = "codex_exec"
        config.API_KEY = ""
        MainWindow._start_analysis_for_backlog(window)
        window._start_analysis.assert_called_once_with()
    finally:
        config.AI_PROVIDER_MODE = original_mode
        config.API_KEY = original_key


def test_backlog_api_mode_waits_for_key():
    window = SimpleNamespace(
        storage=SimpleNamespace(
            get_pending_chunks=Mock(return_value=[Mock()]),
            get_pending_capture_batches=Mock(return_value=[]),
        ),
        _start_analysis=Mock(),
    )
    original_mode = config.AI_PROVIDER_MODE
    original_key = config.API_KEY

    try:
        config.AI_PROVIDER_MODE = "api"
        config.API_KEY = ""
        MainWindow._start_analysis_for_backlog(window)
        window._start_analysis.assert_not_called()
    finally:
        config.AI_PROVIDER_MODE = original_mode
        config.API_KEY = original_key


def test_capture_backlog_starts_analysis_without_recording():
    window = SimpleNamespace(
        storage=SimpleNamespace(
            get_pending_chunks=Mock(return_value=[]),
            get_pending_capture_batches=Mock(return_value=[Mock()]),
        ),
        _start_analysis=Mock(),
    )
    original_mode = config.AI_PROVIDER_MODE
    original_key = config.API_KEY

    try:
        config.AI_PROVIDER_MODE = "codex_exec"
        config.API_KEY = ""
        MainWindow._start_analysis_for_backlog(window)
        window._start_analysis.assert_called_once_with()
    finally:
        config.AI_PROVIDER_MODE = original_mode
        config.API_KEY = original_key
