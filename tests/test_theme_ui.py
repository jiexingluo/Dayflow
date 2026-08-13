from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import QApplication

from database.storage import StorageManager
from ui.main_window import CustomTitleBar, SettingsPanel, TitleBarButton
from ui.stats_view import StatsPanel
from ui.themes import DARK_THEME, LIGHT_THEME, get_theme_manager


def _render_button(button: TitleBarButton, background: str) -> QImage:
    image = QImage(button.size(), QImage.Format_ARGB32)
    image.fill(QColor(background))
    painter = QPainter(image)
    try:
        button.render(painter, QPoint())
    finally:
        painter.end()
    return image


def _count_pixels_different_from(image: QImage, background: str) -> int:
    expected = QColor(background).rgb()
    return sum(
        image.pixel(x, y) != expected
        for y in range(image.height())
        for x in range(image.width())
    )


def test_dark_theme_uses_black_application_background():
    qapp = QApplication.instance()
    manager = get_theme_manager()
    original_theme = manager.current_theme
    original_applied = manager._theme_applied
    original_stylesheet = qapp.styleSheet()

    try:
        manager._current_theme = DARK_THEME
        manager._theme_applied = False
        qapp.setStyleSheet("")

        manager.set_theme(DARK_THEME)

        assert manager._theme_applied
        assert DARK_THEME.bg_primary == "#000000"
        assert "background-color: #000000" in qapp.styleSheet()
    finally:
        manager._current_theme = original_theme
        manager._theme_applied = original_applied
        qapp.setStyleSheet(original_stylesheet)


def test_stats_and_settings_color_every_scroll_layer(tmp_path):
    qapp = QApplication.instance()
    storage = StorageManager(tmp_path / "theme-ui.db", use_pool=False)
    manager = get_theme_manager()
    original_theme = manager.current_theme

    try:
        stats = StatsPanel(storage)
        settings = SettingsPanel(storage)

        for theme in (DARK_THEME, LIGHT_THEME):
            manager.set_theme(theme)
            qapp.processEvents()

            for panel in (stats, settings):
                assert theme.bg_primary in panel.styleSheet()
                assert theme.bg_primary in panel.scroll.styleSheet()
                assert theme.bg_primary in panel.scroll.viewport().styleSheet()
                assert theme.bg_primary in panel.scroll_content.styleSheet()
    finally:
        manager.set_theme(original_theme)
        stats.deleteLater()
        settings.deleteLater()
        qapp.processEvents()
        storage.close()


def test_title_bar_icons_are_drawn_and_maximize_state_changes():
    qapp = QApplication.instance()
    manager = get_theme_manager()
    original_theme = manager.current_theme

    try:
        for theme in (DARK_THEME, LIGHT_THEME):
            manager.set_theme(theme)
            for icon_type in ("tray", "minimize", "maximize", "restore", "close"):
                button = TitleBarButton(icon_type)
                image = _render_button(button, theme.bg_primary)
                assert _count_pixels_different_from(image, theme.bg_primary) > 0
                button.deleteLater()

        manager.set_theme(DARK_THEME)
        title_bar = CustomTitleBar()
        title_bar.resize(420, 32)
        title_bar.show()
        qapp.processEvents()
        dark_preview = title_bar.grab().toImage()
        assert QColor(dark_preview.pixel(300, 16)) == QColor(DARK_THEME.bg_primary)
        manager.set_theme(LIGHT_THEME)
        qapp.processEvents()
        light_preview = title_bar.grab().toImage()
        assert QColor(light_preview.pixel(300, 16)) == QColor(LIGHT_THEME.bg_primary)
        title_bar.update_maximize_button(True)
        assert title_bar.max_btn._icon_type == "restore"
        assert title_bar.max_btn.toolTip() == "还原"
        title_bar.update_maximize_button(False)
        assert title_bar.max_btn._icon_type == "maximize"
        assert title_bar.max_btn.toolTip() == "最大化"
        assert title_bar.close_btn.cursor().shape() == Qt.PointingHandCursor
        title_bar.deleteLater()
    finally:
        manager.set_theme(original_theme)
        qapp.processEvents()
