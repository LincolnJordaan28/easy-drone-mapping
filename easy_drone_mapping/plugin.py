# -*- coding: utf-8 -*-
"""QGIS entry point: a toolbar button and menu entry that open the panel."""
import os

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QIcon

from .compat import QAction

MENU = "&Easy Drone Mapping"


class EasyOdmPlugin:
    def __init__(self, iface):
        self.iface = iface
        self.panel = None
        self.action = None
        self.help_action = None

    def initGui(self):
        icon = QIcon(os.path.join(os.path.dirname(__file__), "icon.svg"))
        self.action = QAction(icon, "Easy Drone Mapping",
                              self.iface.mainWindow())
        self.action.setToolTip("Process drone photos into an orthophoto, "
                               "elevation models and a point cloud")
        self.action.setCheckable(True)
        self.action.triggered.connect(self.toggle_panel)
        self.iface.addToolBarIcon(self.action)
        self.iface.addPluginToMenu(MENU, self.action)

        self.help_action = QAction("Setup guide", self.iface.mainWindow())
        self.help_action.triggered.connect(self.show_help)
        self.iface.addPluginToMenu(MENU, self.help_action)

    def unload(self):
        if self.panel is not None:
            self.panel.shutdown()
            self.iface.removeDockWidget(self.panel)
            self.panel.deleteLater()
            self.panel = None
        for action in (self.action, self.help_action):
            if action is not None:
                self.iface.removePluginMenu(MENU, action)
        if self.action is not None:
            self.iface.removeToolBarIcon(self.action)
        self.action = None
        self.help_action = None

    def _ensure_panel(self):
        if self.panel is None:
            from .panel import DronePanel
            self.panel = DronePanel(self.iface, self.iface.mainWindow())
            self.iface.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea,
                                     self.panel)
            self.panel.visibilityChanged.connect(self._sync_action)
        return self.panel

    def _sync_action(self, visible):
        if self.action is not None:
            self.action.setChecked(bool(visible))

    def toggle_panel(self, checked=True):
        panel = self._ensure_panel()
        panel.setVisible(bool(checked))
        if checked:
            panel.raise_()

    def show_help(self):
        from .dialogs import HelpDialog
        from .compat import exec_dialog
        exec_dialog(HelpDialog(self.iface.mainWindow()))
