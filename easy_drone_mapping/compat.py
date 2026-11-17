# -*- coding: utf-8 -*-
"""Small helpers so the same code runs on QGIS 3 (Qt5) and QGIS 4 (Qt6)."""
from qgis.core import Qgis, QgsMessageLog

try:  # Qt6
    from qgis.PyQt.QtGui import QAction  # noqa: F401
except ImportError:  # Qt5
    from qgis.PyQt.QtWidgets import QAction  # noqa: F401

LOG_TAG = "Easy Drone Mapping"


def message_level(name):
    """'Info' | 'Warning' | 'Critical' | 'Success' -> Qgis message level."""
    scoped = getattr(Qgis, "MessageLevel", None)
    if scoped is not None and hasattr(scoped, name):
        return getattr(scoped, name)
    return getattr(Qgis, name)


def log(message, level="Info"):
    QgsMessageLog.logMessage(str(message), LOG_TAG, message_level(level))


def exec_dialog(dialog):
    """Run a modal dialog on both Qt5 (exec_) and Qt6 (exec)."""
    run = getattr(dialog, "exec", None) or getattr(dialog, "exec_")
    return run()
