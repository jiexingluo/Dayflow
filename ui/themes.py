"""
Dayflow Windows - 主题管理
IDE 风格的亮色/暗色主题
"""
from dataclasses import dataclass
from typing import Optional
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Signal, QObject


@dataclass
class Theme:
    """主题颜色定义"""
    name: str
    
    # 背景色
    bg_primary: str      # 主背景
    bg_secondary: str    # 次背景（卡片、面板）
    bg_tertiary: str     # 第三背景（输入框）
    bg_hover: str        # 悬停背景
    bg_sidebar: str      # 侧边栏
    
    # 边框
    border: str
    border_hover: str
    
    # 文字
    text_primary: str    # 主文字
    text_secondary: str  # 次文字
    text_muted: str      # 弱化文字
    
    # 强调色
    accent: str          # 主强调色
    accent_hover: str    # 强调色悬停
    accent_light: str    # 浅强调色（背景用）
    
    # 功能色
    success: str
    warning: str
    error: str
    
    # 滚动条
    scrollbar: str
    scrollbar_hover: str
    
    # 卡片阴影
    shadow: str


# 暗色主题 - Apple 风格深色
DARK_THEME = Theme(
    name="dark",
    bg_primary="#000000",       # 纯黑主背景
    bg_secondary="#2C2C2E",     # 卡片背景 - 略浅
    bg_tertiary="#3A3A3C",      # 输入框背景
    bg_hover="#48484A",         # 悬停背景
    bg_sidebar="#000000",       # 侧边栏与主背景保持一致
    border="#3A3A3C",           # 柔和边框
    border_hover="#545456",
    text_primary="#FFFFFF",     # 纯白
    text_secondary="#EBEBF5",   # 次要文字 - Apple 风格
    text_muted="#8E8E93",       # 弱化文字 - Apple 灰
    accent="#0A84FF",           # Apple 蓝
    accent_hover="#409CFF",
    accent_light="rgba(10, 132, 255, 0.15)",
    success="#30D158",          # Apple 绿
    warning="#FF9F0A",          # Apple 橙
    error="#FF453A",            # Apple 红
    scrollbar="#48484A",
    scrollbar_hover="#636366",
    shadow="rgba(0, 0, 0, 0.35)",
)


# 亮色主题 - 工作台风格，用明确的表面层级取代大面积灰色块
LIGHT_THEME = Theme(
    name="light",
    bg_primary="#F5F7FA",       # 页面画布
    bg_secondary="#FFFFFF",     # 卡片与顶部表面
    bg_tertiary="#EEF2F7",      # 输入框与次级控件
    bg_hover="#E3E9F2",         # 悬停反馈
    bg_sidebar="#F8FAFC",       # 侧边栏轻微区分于画布
    border="#D6DDE8",           # 卡片与控件分界
    border_hover="#A8B3C2",     # 窗口轮廓与强调分界
    text_primary="#172033",     # 避免纯黑的生硬感
    text_secondary="#46536A",   # 次要文字
    text_muted="#7C8799",       # 弱化文字
    accent="#2563EB",           # 稳定的工作蓝
    accent_hover="#1D4ED8",
    accent_light="rgba(37, 99, 235, 0.11)",
    success="#16A34A",
    warning="#D97706",
    error="#DC2626",
    scrollbar="#B8C2D1",
    scrollbar_hover="#8F9BAD",
    shadow="rgba(23, 32, 51, 0.08)",
)


class ThemeManager(QObject):
    """主题管理器"""
    
    theme_changed = Signal(object)  # 传递 Theme 对象
    
    _instance: Optional['ThemeManager'] = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance
    
    def __init__(self):
        if self._initialized:
            return
        super().__init__()
        self._current_theme = DARK_THEME
        self._theme_applied = False
        self._initialized = True
    
    @property
    def current_theme(self) -> Theme:
        return self._current_theme
    
    @property
    def is_dark(self) -> bool:
        return self._current_theme.name == "dark"
    
    def set_theme(self, theme: Theme):
        """设置主题"""
        if self._current_theme == theme:
            if not self._theme_applied:
                self._apply_global_theme()
            return  # 避免重复切换和发送信号
        self._current_theme = theme
        self._apply_global_theme()
        self.theme_changed.emit(theme)
    
    def toggle_theme(self):
        """切换主题"""
        if self.is_dark:
            self.set_theme(LIGHT_THEME)
        else:
            self.set_theme(DARK_THEME)
    
    def _apply_global_theme(self):
        """应用全局样式"""
        app = QApplication.instance()
        if app:
            app.setStyleSheet(self.get_global_stylesheet())
            self._theme_applied = True
    
    def get_global_stylesheet(self) -> str:
        """生成全局样式表"""
        t = self._current_theme
        return f"""
            /* ===== 全局基础 ===== */
            QMainWindow {{
                background-color: {t.bg_primary};
            }}
            
            QWidget {{
                color: {t.text_primary};
                font-family: "Segoe UI", "Microsoft YaHei UI", sans-serif;
            }}

            /* 标签默认透明背景，避免祖先容器的声明式背景色级联到标签上 */
            QLabel {{
                background: transparent;
            }}

            /* ===== 滚动条 ===== */
            QScrollArea {{
                border: none;
                background-color: transparent;
            }}
            
            QScrollBar:vertical {{
                width: 8px;
                background: transparent;
            }}
            QScrollBar::handle:vertical {{
                background: {t.scrollbar};
                border-radius: 4px;
                min-height: 30px;
            }}
            QScrollBar::handle:vertical:hover {{
                background: {t.scrollbar_hover};
            }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
                height: 0;
            }}
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
                background: transparent;
            }}
            
            /* ===== 输入框 ===== */
            QLineEdit {{
                background-color: {t.bg_tertiary};
                border: 1px solid {t.border};
                border-radius: 8px;
                padding: 10px 14px;
                color: {t.text_primary};
                font-size: 14px;
            }}
            QLineEdit:focus {{
                border-color: {t.accent};
            }}
            QLineEdit::placeholder {{
                color: {t.text_muted};
            }}
            
            /* ===== 按钮 ===== */
            QPushButton {{
                background-color: {t.bg_tertiary};
                color: {t.text_primary};
                border: 1px solid {t.border};
                border-radius: 8px;
                padding: 8px 16px;
                font-size: 13px;
            }}
            QPushButton:hover {{
                background-color: {t.bg_hover};
                border-color: {t.border_hover};
            }}
            QPushButton:pressed {{
                background-color: {t.bg_tertiary};
            }}
            QPushButton:disabled {{
                background-color: {t.bg_secondary};
                color: {t.text_muted};
                border-color: {t.border};
            }}
            
            /* ===== 进度条 ===== */
            QProgressBar {{
                background-color: {t.bg_tertiary};
                border: none;
                border-radius: 6px;
                text-align: center;
            }}
            QProgressBar::chunk {{
                border-radius: 6px;
            }}
            
            /* ===== 工具提示 ===== */
            QToolTip {{
                background-color: {t.bg_secondary};
                color: {t.text_primary};
                border: 1px solid {t.border};
                border-radius: 6px;
                padding: 6px 10px;
            }}
            
            /* ===== 消息框 ===== */
            QMessageBox {{
                background-color: {t.bg_primary};
            }}
            QMessageBox QLabel {{
                color: {t.text_primary};
            }}
            QMessageBox QPushButton {{
                min-width: 80px;
                padding: 8px 20px;
            }}
            
            /* ===== 菜单 ===== */
            QMenu {{
                background-color: {t.bg_secondary};
                border: 1px solid {t.border};
                border-radius: 8px;
                padding: 4px;
            }}
            QMenu::item {{
                padding: 8px 24px;
                border-radius: 4px;
                color: {t.text_primary};
            }}
            QMenu::item:selected {{
                background-color: {t.bg_hover};
            }}
            QMenu::separator {{
                height: 1px;
                background-color: {t.border};
                margin: 4px 8px;
            }}
        """


# 全局函数
def get_theme_manager() -> ThemeManager:
    """获取主题管理器实例"""
    return ThemeManager()


def get_efficiency_color(score: float, theme: 'Theme' = None) -> str:
    """根据效率分数获取对应颜色
    
    Args:
        score: 效率分数 (0-100)
        theme: 主题对象，默认使用当前主题
    
    Returns:
        颜色值字符串
    """
    if theme is None:
        theme = get_theme()
    
    if score >= 70:
        return theme.success  # 绿色 - 高效
    elif score >= 40:
        return theme.warning  # 橙色 - 中等
    else:
        return theme.text_muted  # 灰色 - 低效


def get_theme() -> Theme:
    """获取当前主题"""
    return get_theme_manager().current_theme


def is_dark_theme() -> bool:
    """是否为暗色主题"""
    return get_theme_manager().is_dark
