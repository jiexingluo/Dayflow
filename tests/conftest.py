import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication


# QWidget tests and the existing QCoreApplication signal tests share one event loop.
_qt_test_app = QApplication.instance() or QApplication([])
