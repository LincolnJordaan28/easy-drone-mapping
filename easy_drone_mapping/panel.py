# -*- coding: utf-8 -*-
"""The dock panel: four numbered steps and one big button."""
import json
import os
import time

from qgis.core import QgsApplication, QgsProject, QgsSettings
from qgis.PyQt.QtCore import Qt, QThread, QTimer, QUrl, pyqtSignal
from qgis.PyQt.QtGui import QDesktopServices, QFont
from qgis.PyQt.QtWidgets import (QCheckBox, QComboBox, QDockWidget,
                                 QDoubleSpinBox, QFileDialog, QFormLayout,
                                 QFrame, QGroupBox, QHBoxLayout, QLabel,
                                 QLineEdit, QMessageBox, QPlainTextEdit,
                                 QProgressBar, QProgressDialog, QPushButton,
                                 QScrollArea, QSizePolicy, QToolButton,
                                 QVBoxLayout, QWidget)

from . import estimate, native, nodeodm, presets, results
from .compat import exec_dialog, log, message_level
from .dialogs import DEFAULT_URL, HelpDialog, SettingsDialog
from .engine import EngineManager
from .job import OdmJob
from .job_native import NativeJob
from .native import NativeInstaller
from .nodeodm import NodeClient, NodeError

SETTINGS_PREFIX = "easy_odm/"
GREEN, RED, AMBER, GREY = "#2e9e5b", "#d64545", "#e09b1a", "#9aa0a6"


class EngineProbe(QThread):
    """Asks the engine 'are you there?' without freezing QGIS."""
    answered = pyqtSignal(object)   # dict(ok, info, options, error)

    def __init__(self, url, token, want_options, parent=None):
        super().__init__(parent)
        self._url, self._token, self._want = url, token, want_options

    def run(self):
        out = {"ok": False, "info": None, "options": None, "error": "",
               "url": self._url}
        try:
            client = NodeClient(self._url, self._token, timeout=6)
            out["info"] = client.info()
            if self._want:
                out["options"] = client.options()
            out["ok"] = True
        except NodeError as e:
            out["error"] = str(e)
        except Exception as e:  # noqa: BLE001
            out["error"] = "%s: %s" % (type(e).__name__, e)
        self.answered.emit(out)


class DronePanel(QDockWidget):
    def __init__(self, iface, parent=None):
        super().__init__("Easy Drone Mapping", parent)
        self.setObjectName("EasyDroneMappingPanel")
        self.iface = iface
        self.settings = QgsSettings()

        self.engine = EngineManager(self)
        self.engine.log.connect(self._append_log)
        self.engine.status.connect(self._on_engine_status)
        self.engine.finished.connect(self._on_engine_finished)

        self.installer = NativeInstaller(self)
        self.installer.log.connect(self._append_log)
        self.installer.status.connect(self._on_engine_status)
        self.installer.finished.connect(self._on_install_finished)

        self.job = None
        self._probe = None
        self._engine_state = "checking"
        self._old_probes = []
        self._engine_ready = False
        self._engine_info = None
        self._node_options = None
        self._waiting_for_engine_until = 0
        self._run_when_ready = False
        self._images = []
        self._results_edited = False
        self._name_edited = False
        self._last_result = None
        self._job_started = 0
        self._eta = None
        self._reinstall_tried = False

        self._build_ui()
        self._load_settings()

        self._probe_timer = QTimer(self)
        self._probe_timer.timeout.connect(self._probe_tick)
        self._probe_timer.start(3000)
        self._clock = QTimer(self)
        self._clock.timeout.connect(self._tick_clock)

        self._set_engine_state("checking", "Checking for the engine…")
        QTimer.singleShot(200, self.check_engine)

    # ================================================================ settings
    def _get(self, key, default=None, kind=str):
        return self.settings.value(SETTINGS_PREFIX + key, default, type=kind)

    def _set(self, key, value):
        self.settings.setValue(SETTINGS_PREFIX + key, value)

    def _config(self):
        return {
            "engine": self._engine_kind(),
            "odm_path": self._get("odm_path", ""),
            "url": self._get("url", DEFAULT_URL) or DEFAULT_URL,
            "token": self._get("token", ""),
            "gpu": self._get("gpu", False, bool),
            "cli": self._get("cli", ""),
            "remove_after": self._get("remove_after", True, bool),
            "keep_zip": self._get("keep_zip", False, bool),
        }

    def _engine_kind(self):
        """'builtin' = ODM run directly (no Docker); 'docker' = NodeODM."""
        kind = self._get("engine", "")
        if kind in ("builtin", "docker"):
            return kind
        return "builtin" if native.IS_WINDOWS else "docker"

    def _builtin(self):
        return self._engine_kind() == "builtin"

    def _client(self):
        cfg = self._config()
        return NodeClient(cfg["url"], cfg["token"])

    def _is_local(self):
        try:
            return self._client().is_local
        except NodeError:
            return False

    # ====================================================================== UI
    def _build_ui(self):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        body = QWidget()
        root = QVBoxLayout(body)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(10)
        scroll.setWidget(body)
        self.setWidget(scroll)

        intro = QLabel("Turn drone photos into an orthophoto, elevation "
                       "models and a point cloud — free, on your own computer.")
        intro.setWordWrap(True)
        root.addWidget(intro)

        # ---- resume banner (hidden unless an earlier job is still around)
        self.resume_box = QFrame()
        self.resume_box.setFrameShape(QFrame.Shape.StyledPanel)
        rb = QVBoxLayout(self.resume_box)
        self.resume_label = QLabel()
        self.resume_label.setWordWrap(True)
        rb.addWidget(self.resume_label)
        rrow = QHBoxLayout()
        self.resume_btn = QPushButton("Resume")
        self.resume_btn.clicked.connect(self._resume_saved_job)
        self.forget_btn = QPushButton("Forget it")
        self.forget_btn.clicked.connect(self._forget_saved_job)
        rrow.addWidget(self.resume_btn)
        rrow.addWidget(self.forget_btn)
        rrow.addStretch(1)
        rb.addLayout(rrow)
        self.resume_box.hide()
        root.addWidget(self.resume_box)

        # ---- 1 engine
        box1 = self._card("1 · Processing engine")
        l1 = QVBoxLayout(box1)
        status_row = QHBoxLayout()
        self.engine_dot = QLabel("●")
        self.engine_text = QLabel("")
        self.engine_text.setWordWrap(True)
        self.engine_text.setSizePolicy(QSizePolicy.Policy.Expanding,
                                       QSizePolicy.Policy.Preferred)
        status_row.addWidget(self.engine_dot, 0, Qt.AlignmentFlag.AlignTop)
        status_row.addWidget(self.engine_text, 1)
        l1.addLayout(status_row)
        self.engine_busy_bar = QProgressBar()
        self.engine_busy_bar.setRange(0, 0)
        self.engine_busy_bar.setTextVisible(False)
        self.engine_busy_bar.setMaximumHeight(6)
        self.engine_busy_bar.hide()
        l1.addWidget(self.engine_busy_bar)
        brow = QHBoxLayout()
        self.start_btn = QPushButton("Start engine")
        self.start_btn.clicked.connect(self.start_engine)
        self.stop_btn = QPushButton("Stop engine")
        self.stop_btn.clicked.connect(self.stop_engine)
        self.install_btn = QPushButton("Install engine")
        self.install_btn.clicked.connect(self.install_engine)
        self.settings_btn = QPushButton("Settings…")
        self.settings_btn.clicked.connect(self.open_settings)
        self.help_btn = QPushButton("Setup guide")
        self.help_btn.clicked.connect(self.open_help)
        for b in (self.start_btn, self.stop_btn, self.install_btn):
            brow.addWidget(b)
        brow.addStretch(1)
        brow.addWidget(self.help_btn)
        brow.addWidget(self.settings_btn)
        l1.addLayout(brow)
        root.addWidget(box1)

        # ---- 2 photos
        box2 = self.box_photos = self._card("2 · Photos")
        l2 = QVBoxLayout(box2)
        prow = QHBoxLayout()
        self.photos_edit = QLineEdit()
        self.photos_edit.setPlaceholderText("Folder with the drone photos")
        self.photos_edit.editingFinished.connect(self._photos_changed)
        pbrowse = QPushButton("Browse…")
        pbrowse.clicked.connect(self._browse_photos)
        prow.addWidget(self.photos_edit, 1)
        prow.addWidget(pbrowse)
        l2.addLayout(prow)
        crow = QHBoxLayout()
        self.photos_info = QLabel("No folder chosen yet.")
        self.photos_info.setWordWrap(True)
        self.show_photos_btn = QPushButton("Show on map")
        self.show_photos_btn.setToolTip(
            "Adds a point for every photo that carries a GPS position, so "
            "you can check the flight covers your site before processing.")
        self.show_photos_btn.setEnabled(False)
        self.show_photos_btn.clicked.connect(self._show_photo_positions)
        crow.addWidget(self.photos_info, 1)
        crow.addWidget(self.show_photos_btn)
        l2.addLayout(crow)
        root.addWidget(box2)

        # ---- 3 products
        box3 = self.box_products = self._card("3 · What to create")
        l3 = QVBoxLayout(box3)
        self.cb_ortho = self._check(
            "Orthophoto", "One seamless, map-accurate aerial image (GeoTIFF).")
        self.cb_dsm = self._check(
            "Surface model (DSM)",
            "Elevation of everything the drone saw: ground, trees, buildings.")
        self.cb_dtm = self._check(
            "Terrain model (DTM)",
            "Elevation of the bare ground, with vegetation and buildings "
            "filtered out.")
        self.cb_pc = self._check(
            "Point cloud", "Millions of 3D points (LAZ). View it in 2D or in "
            "a QGIS 3D map view.")
        self.cb_3d = self._check(
            "3D textured model", "A photo-textured mesh (OBJ) for viewers "
            "such as MeshLab or Blender. Adds processing time.", checked=False)
        for cb in (self.cb_ortho, self.cb_dsm, self.cb_dtm, self.cb_pc,
                   self.cb_3d):
            l3.addWidget(cb)
            cb.toggled.connect(self._products_changed)

        qrow = QHBoxLayout()
        qrow.addWidget(QLabel("Quality"))
        self.quality = QComboBox()
        for key, label, _desc, _opts in presets.QUALITY_PRESETS:
            self.quality.addItem(label, key)
        self.quality.setCurrentIndex(1)
        self.quality.currentIndexChanged.connect(self._quality_changed)
        qrow.addWidget(self.quality, 1)
        l3.addLayout(qrow)
        self.quality_hint = QLabel()
        self.quality_hint.setWordWrap(True)
        self.quality_hint.setEnabled(False)
        l3.addWidget(self.quality_hint)
        self.estimate_label = QLabel()
        self.estimate_label.setWordWrap(True)
        self.estimate_label.setTextFormat(Qt.TextFormat.RichText)
        self.estimate_label.hide()
        l3.addWidget(self.estimate_label)

        self.more_btn = QToolButton()
        self.more_btn.setText("More options")
        self.more_btn.setCheckable(True)
        self.more_btn.setAutoRaise(True)
        self.more_btn.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.more_btn.setArrowType(Qt.ArrowType.RightArrow)
        self.more_btn.toggled.connect(self._toggle_more)
        l3.addWidget(self.more_btn)

        self.more = QWidget()
        mform = QFormLayout(self.more)
        mform.setContentsMargins(4, 0, 0, 0)
        self.ortho_res = self._res_spin(
            "Size of one orthophoto pixel on the ground. Smaller is sharper "
            "but slower. ODM never goes finer than the photos allow.")
        mform.addRow("Orthophoto pixel size", self.ortho_res)
        self.dem_res = self._res_spin(
            "Size of one elevation-model cell on the ground.")
        mform.addRow("Elevation pixel size", self.dem_res)
        grow = QHBoxLayout()
        self.gcp_edit = QLineEdit()
        self.gcp_edit.setPlaceholderText("optional gcp_list.txt")
        self.gcp_edit.setToolTip(
            "Ground control points in ODM's gcp_list.txt format, for "
            "survey-grade positioning.")
        gbrowse = QPushButton("…")
        gbrowse.setMaximumWidth(32)
        gbrowse.clicked.connect(self._browse_gcp)
        grow.addWidget(self.gcp_edit, 1)
        grow.addWidget(gbrowse)
        mform.addRow("Ground control", grow)
        self.extra_edit = QLineEdit()
        self.extra_edit.setPlaceholderText("e.g. auto-boundary; crop=0")
        self.extra_edit.setToolTip(
            "For experts: any other ODM option as name=value, separated by "
            "semicolons. A name on its own switches that option on.\n"
            "See docs.opendronemap.org/arguments")
        mform.addRow("Extra ODM options", self.extra_edit)
        self.cb_report = QCheckBox("Also write a PDF quality report")
        self.cb_report.setToolTip(
            "OpenDroneMap's own report with accuracy figures and overlap "
            "diagrams. Off by default: it adds time and is not needed for "
            "the maps.")
        mform.addRow("", self.cb_report)
        self.more.hide()
        l3.addWidget(self.more)
        root.addWidget(box3)

        # ---- 4 output
        box4 = self.box_output = self._card("4 · Save results")
        l4 = QFormLayout(box4)
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("Name for this survey")
        self.name_edit.textEdited.connect(self._name_was_edited)
        l4.addRow("Name", self.name_edit)
        orow = QHBoxLayout()
        self.out_edit = QLineEdit()
        self.out_edit.setPlaceholderText("Folder for the results")
        self.out_edit.textEdited.connect(self._results_was_edited)
        obrowse = QPushButton("Browse…")
        obrowse.clicked.connect(self._browse_out)
        orow.addWidget(self.out_edit, 1)
        orow.addWidget(obrowse)
        l4.addRow("Folder", orow)
        root.addWidget(box4)

        # ---- run
        self.run_btn = QPushButton("Process photos")
        f = QFont(self.run_btn.font())
        f.setBold(True)
        if f.pointSize() > 0:
            f.setPointSize(f.pointSize() + 1)
        self.run_btn.setFont(f)
        self.run_btn.setMinimumHeight(42)
        self.run_btn.setStyleSheet(
            "QPushButton{background:%s;color:white;border:none;"
            "border-radius:5px;padding:6px 12px;}"
            "QPushButton:hover{background:#278a4f;}"
            "QPushButton:disabled{background:#9aa0a6;color:#eeeeee;}" % GREEN)
        self.run_btn.clicked.connect(self.run)
        root.addWidget(self.run_btn)

        # ---- progress
        self.progress_box = QFrame()
        pl = QVBoxLayout(self.progress_box)
        pl.setContentsMargins(0, 0, 0, 0)
        self.stage_label = QLabel("")
        self.stage_label.setWordWrap(True)
        pl.addWidget(self.stage_label)
        prow2 = QHBoxLayout()
        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setFormat("%p%")
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.clicked.connect(self.cancel)
        prow2.addWidget(self.progress, 1)
        prow2.addWidget(self.cancel_btn)
        pl.addLayout(prow2)
        self.clock_label = QLabel("")
        self.clock_label.setEnabled(False)
        pl.addWidget(self.clock_label)
        self.progress_box.hide()
        root.addWidget(self.progress_box)

        # ---- after a job
        self.done_box = QFrame()
        dl = QVBoxLayout(self.done_box)
        dl.setContentsMargins(0, 0, 0, 0)
        self.done_label = QLabel("")
        self.done_label.setWordWrap(True)
        self.done_label.setTextFormat(Qt.TextFormat.RichText)
        dl.addWidget(self.done_label)
        drow = QHBoxLayout()
        self.open_folder_btn = QPushButton("Open results folder")
        self.open_folder_btn.clicked.connect(self._open_results_folder)
        self.open_report_btn = QPushButton("Quality report")
        self.open_report_btn.clicked.connect(self._open_report)
        drow.addWidget(self.open_folder_btn)
        drow.addWidget(self.open_report_btn)
        drow.addStretch(1)
        dl.addLayout(drow)
        self.done_box.hide()
        root.addWidget(self.done_box)

        # ---- details / log
        self.log_btn = QToolButton()
        self.log_btn.setText("Details")
        self.log_btn.setCheckable(True)
        self.log_btn.setAutoRaise(True)
        self.log_btn.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.log_btn.setArrowType(Qt.ArrowType.RightArrow)
        self.log_btn.toggled.connect(self._toggle_log)
        root.addWidget(self.log_btn)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(4000)
        self.log_view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.log_view.setMinimumHeight(160)
        mono = QFont("monospace")
        mono.setStyleHint(QFont.StyleHint.Monospace)
        if self.font().pointSize() > 0:
            mono.setPointSize(max(self.font().pointSize() - 1, 7))
        self.log_view.setFont(mono)
        self.log_view.hide()
        root.addWidget(self.log_view)

        root.addStretch(1)
        self._quality_changed()
        # Wide enough that nothing is cut off and no sideways scrolling.
        scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setMinimumWidth(
            body.minimumSizeHint().width()
            + scroll.verticalScrollBar().sizeHint().width() + 6)

    def _card(self, title):
        box = QGroupBox(title)
        f = QFont(box.font())
        f.setBold(True)
        box.setFont(f)
        # children should not inherit the bold title font
        box.setStyleSheet("QGroupBox{font-weight:bold;} "
                          "QGroupBox *{font-weight:normal;}")
        return box

    def _check(self, text, tip, checked=True):
        cb = QCheckBox(text)
        cb.setToolTip(tip)
        cb.setChecked(checked)
        return cb

    def _res_spin(self, tip):
        spin = QDoubleSpinBox()
        spin.setRange(0.0, 500.0)
        spin.setDecimals(1)
        spin.setSingleStep(0.5)
        spin.setSuffix(" cm")
        spin.setSpecialValueText("Automatic")
        spin.setValue(0.0)
        spin.setToolTip(tip)
        return spin

    # ============================================================ small slots
    def _toggle_more(self, on):
        self.more.setVisible(on)
        self.more_btn.setArrowType(Qt.ArrowType.DownArrow if on
                                   else Qt.ArrowType.RightArrow)

    def _toggle_log(self, on):
        self.log_view.setVisible(on)
        self.log_btn.setArrowType(Qt.ArrowType.DownArrow if on
                                  else Qt.ArrowType.RightArrow)

    def _append_log(self, text):
        self.log_view.appendPlainText(text)

    def _append_log_lines(self, lines):
        self.log_view.appendPlainText("\n".join(str(l) for l in lines))

    def _quality_changed(self, *_):
        key = self.quality.currentData()
        self.quality_hint.setText(presets.preset(key)[2])
        self._products_changed()

    def _products_changed(self, *_):
        only_ortho = self.cb_ortho.isChecked() and not any(
            c.isChecked() for c in (self.cb_dsm, self.cb_dtm, self.cb_pc,
                                    self.cb_3d))
        hint = presets.preset(self.quality.currentData())[2]
        if only_ortho:
            hint += (" Orthophoto only: ODM uses its quick method, which "
                     "skips the dense 3D reconstruction.")
        self.quality_hint.setText(hint)
        self._update_estimate()

    # ---------------------------------------------------------- estimates
    def _time_factor(self):
        try:
            return float(self._get("time_factor", 1.0, float) or 1.0)
        except (TypeError, ValueError):
            return 1.0

    def _plain_options(self, **change):
        """ODM options for the ticked products (no memory limit applied)."""
        products = dict(orthophoto=self.cb_ortho.isChecked(),
                        dsm=self.cb_dsm.isChecked(),
                        dtm=self.cb_dtm.isChecked(),
                        point_cloud=self.cb_pc.isChecked(),
                        model_3d=self.cb_3d.isChecked())
        products.update(change)
        if not any(products.values()):
            return None
        extra, _problems = presets.parse_extra_options(self.extra_edit.text())
        return presets.build_options(
            quality=self.quality.currentData(),
            ortho_res=self.ortho_res.value() or None,
            dem_res=self.dem_res.value() or None,
            extra=extra, report=self.cb_report.isChecked(), **products)

    def _estimate(self, options, free_gb=None):
        """Estimated seconds for a job on this computer."""
        cores = os.cpu_count() or 1
        mem = native.memory_status()
        if free_gb is not None and mem:
            mem = dict(mem, free=free_gb)
        light = int(options.get("max-concurrency") or 0) or \
            native.safe_concurrency(mem, self.quality.currentData()) or cores
        dense = cores if light >= cores else max(light, estimate.dense_cores(
            mem and mem["free"], options.get("pc-quality"), cores))
        return estimate.total(estimate.stage_seconds(
            options, len(self._images), light, dense, self._time_factor()))

    def _update_estimate(self):
        label = getattr(self, "estimate_label", None)
        if label is None:
            return
        opts = self._plain_options() if self._images else None
        if not opts or not self._builtin():
            label.hide()
            return
        now = self._estimate(opts)
        lines = ["<b>Estimated time on this computer: %s.</b>"
                 % estimate.human(now)]
        if self.cb_3d.isChecked():
            without = self._estimate(self._plain_options(model_3d=False))
            if now - without > 300:
                lines.append("Unticking <i>3D textured model</i> saves %s."
                             % estimate.human(now - without)[6:])
        mem = native.memory_status()
        if mem:
            best_free = max(mem["free"], mem["total"] - 3.5)
            best = self._estimate(opts, best_free)
            if best < 0.8 * now and now - best > 600:
                lines.append(
                    "Only %.1f of %.0f GB memory is free. With other programs "
                    "closed it would take %s." % (mem["free"], mem["total"],
                                                  estimate.human(best)))
        label.setText("<br>".join(lines))
        label.show()

    def _memory_check(self, options):
        """Before a long job: offer to free memory first. True = go ahead."""
        if not self._builtin() or "max-concurrency" in options:
            return True
        while True:
            mem = native.memory_status()
            if not mem:
                return True
            now = self._estimate(options)
            best = self._estimate(options,
                                  max(mem["free"], mem["total"] - 3.5))
            if not (best < 0.75 * now and now > 1200):
                return True
            cores = os.cpu_count() or 1
            light = native.safe_concurrency(mem, self.quality.currentData()) \
                or cores
            text = ("Only %.1f GB of this computer's %.0f GB memory is free, "
                    "so OpenDroneMap can use only %d of its %d cores for the "
                    "heaviest steps.\n\nEstimated time now: %s.\n"
                    "With other programs closed: %s."
                    % (mem["free"], mem["total"], light, cores,
                       estimate.human(now), estimate.human(best)))
            users = native.top_memory_users()
            if users:
                text += "\n\nPrograms using the most memory:\n" + "\n".join(
                    "  • %s — %.1f GB" % (n, gb) for n, gb in users)
            text += ("\n\nClose what you can (save your work first), then "
                     "click Check again.")
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Icon.Information)
            box.setWindowTitle("Free some memory to finish sooner?")
            box.setText(text)
            again = box.addButton("Check again",
                                  QMessageBox.ButtonRole.ActionRole)
            start = box.addButton("Start anyway",
                                  QMessageBox.ButtonRole.AcceptRole)
            box.addButton(QMessageBox.StandardButton.Cancel)
            box.setDefaultButton(again)
            exec_dialog(box)
            clicked = box.clickedButton()
            if clicked is start:
                return True
            if clicked is not again:
                return False

    def _learn_pace(self, result):
        """Adjust future estimates to how fast this computer really was."""
        actual, expected = result.get("calibration") or (0.0, 0.0)
        if expected < 300 or actual <= 0:
            return
        ratio = max(0.3, min(3.0, actual / expected))
        factor = self._time_factor() * (1 + 0.6 * (ratio - 1))
        self._set("time_factor", max(0.2, min(5.0, factor)))

    def _name_was_edited(self, _text):
        self._name_edited = True

    def _results_was_edited(self, _text):
        self._results_edited = True

    # ================================================================== photos
    def _browse_photos(self):
        start = self.photos_edit.text() or self._get("last_photos", "")
        folder = QFileDialog.getExistingDirectory(
            self, "Choose the folder with your drone photos", start)
        if folder:
            self.photos_edit.setText(folder)
            self._photos_changed()

    def _photos_changed(self):
        folder = self.photos_edit.text().strip()
        self._images = presets.list_images(folder) if os.path.isdir(folder) \
            else []
        n = len(self._images)
        if not folder:
            self.photos_info.setText("No folder chosen yet.")
        elif not os.path.isdir(folder):
            self.photos_info.setText("That folder does not exist.")
        elif n == 0:
            self.photos_info.setText(
                "No photos found here (looking for JPG, TIFF or DNG files).")
        else:
            size = sum(os.path.getsize(p) for p in self._images) / 1048576.0
            self.photos_info.setText(
                "%d photo%s found (%s)." % (n, "" if n == 1 else "s",
                                           _human_mb(size)))
        self.show_photos_btn.setEnabled(n > 0)
        self._update_estimate()
        if n:
            self._set("last_photos", folder)
            base = os.path.basename(os.path.normpath(folder)) or "survey"
            if not self._name_edited:
                self.name_edit.setText(base)
            if not self._results_edited:
                self.out_edit.setText(
                    os.path.normpath(folder) + "_results")

    def _show_photo_positions(self):
        if not self._images:
            return
        dlg = QProgressDialog("Reading photo positions…", "Stop", 0,
                              len(self._images), self)
        dlg.setWindowModality(Qt.WindowModality.WindowModal)
        dlg.setMinimumDuration(400)

        def step(i, _n):
            dlg.setValue(i)
            return not dlg.wasCanceled()

        layer, count = results.photo_positions_layer(self._images, step)
        dlg.setValue(len(self._images))
        if count == 0:
            QMessageBox.information(
                self, "Photo positions",
                "None of these photos carry a GPS position.\n\nODM can still "
                "process them, but the map will not be placed on Earth "
                "unless you add a ground control point file.")
            return
        layer.setName("Photo positions (%s)" % (self.name_edit.text() or "survey"))
        QgsProject.instance().addMapLayer(layer)
        try:
            canvas = self.iface.mapCanvas()
            from qgis.core import QgsCoordinateTransform
            tr = QgsCoordinateTransform(
                layer.crs(), canvas.mapSettings().destinationCrs(),
                QgsProject.instance())
            rect = tr.transformBoundingBox(layer.extent())
            rect.scale(1.3)
            canvas.setExtent(rect)
            canvas.refresh()
        except Exception as e:  # noqa: BLE001
            log("zoom to photos skipped: %s" % e, "Warning")
        missing = len(self._images) - count
        text = "%d photo positions added to the map." % count
        if missing:
            text += " %d photos have no GPS position." % missing
        self._message(text, "Info")

    def _browse_gcp(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose the ground control point file",
            self.photos_edit.text(), "Text files (*.txt);;All files (*)")
        if path:
            self.gcp_edit.setText(path)

    def _browse_out(self):
        folder = QFileDialog.getExistingDirectory(
            self, "Choose where to save the results", self.out_edit.text())
        if folder:
            self.out_edit.setText(folder)
            self._results_edited = True

    # ================================================================== engine
    def _set_engine_state(self, state, text):
        colors = {"ready": GREEN, "down": RED, "busy": AMBER, "checking": GREY}
        self.engine_dot.setStyleSheet("color:%s;" % colors.get(state, GREY))
        self.engine_text.setText(text)
        self.engine_busy_bar.setVisible(state == "busy")
        running_job = self.job is not None
        builtin = self._builtin()
        local = (not builtin) and self._is_local()
        installing = self.installer.busy
        self.start_btn.setVisible(local and state == "down")
        self.stop_btn.setVisible(local and state == "ready")
        self.stop_btn.setEnabled(not running_job)
        self.install_btn.setVisible(builtin and (
            installing or (state == "down" and native.CAN_AUTO_INSTALL)))
        self.install_btn.setText("Cancel download" if installing
                                 else "Install engine")
        self.settings_btn.setEnabled(not running_job and state != "busy")
        self._engine_state = state

    def check_engine(self):
        """Fire an asynchronous 'are you there?' at the engine."""
        if self._builtin():
            self._check_builtin()
            return
        if self._probe is not None and self._probe.isRunning():
            return
        cfg = self._config()
        probe = EngineProbe(cfg["url"], cfg["token"],
                            want_options=self._node_options is None, parent=self)
        probe.answered.connect(self._on_probe)
        probe.finished.connect(probe.deleteLater)
        self._probe = probe
        probe.start()

    def _check_builtin(self):
        if self.installer.busy:
            return
        folder = native.find_install(self._get("odm_path", ""))
        if folder:
            self._engine_ready = True
            ver = native.installed_version(folder)
            parts = ["Ready", "OpenDroneMap %s" % ver if ver
                     else "OpenDroneMap", "built in"]
            if os.cpu_count():
                parts.append("%d CPU cores" % os.cpu_count())
            mem = native.memory_status()
            if mem:
                parts.append("%.0f GB memory (%.0f GB free)"
                             % (mem["total"], mem["free"]))
            self._set_engine_state("ready", " · ".join(parts))
            self._check_saved_job()
            if self._run_when_ready:
                self._run_when_ready = False
                QTimer.singleShot(0, self.run)
            return
        self._engine_ready = False
        if native.CAN_AUTO_INSTALL:
            self._set_engine_state(
                "down", "Not installed yet. Click Install engine: a one-time, "
                "free %d MB download of OpenDroneMap. Nothing else to set up."
                % native.PINNED_SIZE_MB)
        else:
            self._set_engine_state(
                "down", "The built-in engine installs itself on Windows only. "
                "In Settings, either point to a native OpenDroneMap folder or "
                "switch the engine to Docker.")

    def install_engine(self, *_args, latest=False, confirm=True):
        """Download and install OpenDroneMap from inside QGIS (Windows)."""
        if self.installer.busy:
            self.installer.cancel()
            return False
        if not native.CAN_AUTO_INSTALL:
            self._stop("The one-click install is for Windows. On this system, "
                       "use the Docker engine (Settings) or point Settings to "
                       "a native OpenDroneMap folder.")
            return False
        target = self._get("odm_path", "") or native.default_install_dir()
        if confirm:
            answer = QMessageBox.question(
                self, "Install the processing engine",
                "Download OpenDroneMap from its official GitHub releases "
                "(about %d MB) and install it for your user account in:\n\n"
                "%s\n\nIt is free, open-source software. No administrator "
                "rights and no Docker are needed. Continue?"
                % (native.PINNED_SIZE_MB, target),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                return False
        self._engine_ready = False
        self.installer.install(target, use_latest=latest)
        self._set_engine_state("busy", "Starting download…")
        return True

    def _on_install_finished(self, ok, message):
        if ok:
            self._set("odm_path", message)
            self._append_log("OpenDroneMap installed in %s" % message)
            self._message("The processing engine is installed.", "Success")
            self._check_builtin()
            return
        self._run_when_ready = False
        self._engine_ready = False
        if message == "CANCELED":
            self._check_builtin()
            return
        self._set_engine_state("down", message)
        self.log_btn.setChecked(True)

    def _probe_tick(self):
        if self.job is not None or self.engine.busy or self.installer.busy:
            return
        if self._builtin():
            self._tick = getattr(self, "_tick", 0) + 1
            if self._tick % 4 == 0 and self.isVisible():
                self._check_builtin()
                self._update_estimate()
            return
        waiting = time.time() < self._waiting_for_engine_until
        # Poll quickly while the engine boots, lazily otherwise.
        self._tick = getattr(self, "_tick", 0) + 1
        if waiting or self._tick % 4 == 0:
            if self.isVisible() or waiting:
                self.check_engine()

    def _on_probe(self, out):
        if self.sender() is self._probe:
            self._probe = None
        if out.get("url") != self._config()["url"] or self._builtin():
            return  # settings changed while we were asking
        if self.engine.busy:
            return
        waiting = time.time() < self._waiting_for_engine_until
        if out["ok"]:
            self._waiting_for_engine_until = 0
            self._engine_ready = True
            self._engine_info = out["info"]
            if out["options"] is not None:
                self._node_options = out["options"]
            self._set_engine_state("ready", self._describe_engine(out["info"]))
            self._check_saved_job()
            if self._run_when_ready:
                self._run_when_ready = False
                QTimer.singleShot(0, self.run)
        else:
            self._engine_ready = False
            self._node_options = None
            if waiting:
                self._set_engine_state("busy", "Engine is starting up…")
            elif self._waiting_for_engine_until:
                self._waiting_for_engine_until = 0
                self._run_when_ready = False
                self._set_engine_state(
                    "down", "The engine was started but is not answering. "
                    "Open Details below for the log, or try Start engine again.")
            elif self._is_local():
                self._set_engine_state(
                    "down", "Not running. Click Start engine (first time? "
                    "open the Setup guide).")
            else:
                self._set_engine_state("down", out["error"])

    @staticmethod
    def _describe_engine(info):
        parts = ["Ready"]
        ver = info.get("engineVersion")
        if ver and ver != "?":
            parts.append("%s %s" % (str(info.get("engine", "odm")).upper(), ver))
        cores = info.get("cpuCores")
        if cores:
            parts.append("%s CPU cores" % cores)
        mem = info.get("totalMemory")
        if mem:
            parts.append("%.0f GB memory" % (mem / 1073741824.0))
        return " · ".join(parts)

    def start_engine(self):
        if self.engine.busy:
            return
        cfg = self._config()
        self._set_engine_state("busy", "Starting…")
        self.engine.start_engine(cfg["cli"], cfg["gpu"], self._client().port)

    def stop_engine(self):
        if self.engine.busy or self.job is not None:
            return
        self._set_engine_state("busy", "Stopping…")
        self.engine.stop_engine(self._config()["cli"])

    def _on_engine_status(self, text):
        self._set_engine_state("busy", text)

    def _on_engine_finished(self, ok, message):
        if ok and "stopped" in message.lower():
            self._engine_ready = False
            self._node_options = None
            self._set_engine_state("down", "Stopped.")
            return
        if ok:
            self._node_options = None
            self._waiting_for_engine_until = time.time() + 120
            self._set_engine_state("busy", "Engine is starting up…")
            self.check_engine()
            return
        self._run_when_ready = False
        self._engine_ready = False
        if message == "NO_DOCKER":
            self._set_engine_state("down", "Docker is not installed yet.")
            answer = QMessageBox.question(
                self, "Docker is needed",
                "The ODM engine runs inside Docker, which was not found on "
                "this computer.\n\nOpen the setup guide?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if answer == QMessageBox.StandardButton.Yes:
                self.open_help()
        elif message == "DOCKER_NOT_RUNNING":
            import platform
            if platform.system() == "Linux":
                how = ("Start it (for example: sudo systemctl start docker), "
                       "then click Start engine.")
            else:
                how = ("Open Docker Desktop, wait until it says it is "
                       "running, then click Start engine.")
            self._set_engine_state(
                "down", "Docker is installed but not running. " + how)
        else:
            self._set_engine_state("down", message)
            self.log_btn.setChecked(True)

    def open_settings(self):
        before = self._config()
        dlg = SettingsDialog(before, self)
        if not exec_dialog(dlg):
            return
        after = dlg.values()
        try:
            NodeClient(after["url"])
        except NodeError as e:
            QMessageBox.warning(self, "Settings", str(e))
            return
        for k, v in after.items():
            self._set(k, v)
        if after["odm_path"] and not native.runner_in(after["odm_path"]) \
                and after["engine"] == "builtin" and not native.IS_WINDOWS:
            QMessageBox.warning(
                self, "Settings", "No OpenDroneMap was found in:\n%s\n\n"
                "The folder should contain run.py and run.sh."
                % after["odm_path"])
        self._engine_ready = False
        self._node_options = None
        self._waiting_for_engine_until = 0
        self._set_engine_state("checking", "Checking for the engine…")
        if dlg.update_requested:
            if after["engine"] == "builtin":
                self.install_engine(latest=True)
                return
            if self._is_local():
                self._set_engine_state("busy", "Updating…")
                self.engine.update_engine(after["cli"], after["gpu"],
                                          self._client().port)
                return
        self.check_engine()

    def open_help(self):
        exec_dialog(HelpDialog(self))

    # ===================================================================== run
    def _collect(self):
        """Validate the form. Returns a job spec, or None after telling the
        user what to fix."""
        folder = self.photos_edit.text().strip()
        self._photos_changed()
        if not folder or not os.path.isdir(folder):
            return self._stop("Choose the folder with your drone photos "
                              "(step 2).", self.photos_edit)
        n = len(self._images)
        if n == 0:
            return self._stop("There are no photos in that folder.",
                              self.photos_edit)
        if n < 5:
            answer = QMessageBox.question(
                self, "Very few photos",
                "Only %d photos were found. ODM normally needs many "
                "overlapping photos (dozens or more) to build a map.\n\n"
                "Try anyway?" % n,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                return None
        builtin = self._builtin()
        max_images = None if builtin else \
            (self._engine_info or {}).get("maxImages")
        if max_images and n > max_images:
            return self._stop("This engine accepts at most %d photos per job; "
                              "the folder has %d." % (max_images, n))

        products = dict(orthophoto=self.cb_ortho.isChecked(),
                        dsm=self.cb_dsm.isChecked(),
                        dtm=self.cb_dtm.isChecked(),
                        point_cloud=self.cb_pc.isChecked(),
                        model_3d=self.cb_3d.isChecked())
        if not any(products.values()):
            return self._stop("Tick at least one thing to create (step 3).")

        gcp = self.gcp_edit.text().strip()
        if gcp and not os.path.isfile(gcp):
            return self._stop("The ground control point file was not found:"
                              "\n%s" % gcp, self.gcp_edit)

        extra, problems = presets.parse_extra_options(self.extra_edit.text())
        if problems:
            return self._stop("These extra ODM options could not be "
                              "understood:\n%s\n\nWrite them as name=value, "
                              "separated by semicolons."
                              % "\n".join(problems), self.extra_edit)

        name = self.name_edit.text().strip() or \
            os.path.basename(os.path.normpath(folder)) or "survey"
        out_dir = self.out_edit.text().strip() or \
            os.path.normpath(folder) + "_results"
        out_dir = _free_folder(os.path.abspath(os.path.expanduser(out_dir)))
        try:
            os.makedirs(out_dir, exist_ok=True)
        except OSError as e:
            return self._stop("The results folder could not be created:\n%s"
                              % e, self.out_edit)

        options = presets.build_options(
            quality=self.quality.currentData(),
            ortho_res=self.ortho_res.value() or None,
            dem_res=self.dem_res.value() or None,
            extra=extra, report=self.cb_report.isChecked(), **products)
        if builtin:
            if not self._memory_check(options):
                return None
            auto = "max-concurrency" not in options
            options = self._limit_for_memory(options)
        try:
            import shutil
            free_gb = shutil.disk_usage(out_dir).free / 1073741824.0
            photos_gb = sum(os.path.getsize(p) for p in self._images) \
                / 1073741824.0
            need_gb = max(5.0, photos_gb * 10)
            if free_gb < need_gb:
                answer = QMessageBox.question(
                    self, "Low disk space",
                    "Only %.0f GB is free on the drive with the results "
                    "folder. Processing these photos typically needs about "
                    "%.0f GB of working space.\n\nStart anyway?"
                    % (free_gb, need_gb),
                    QMessageBox.StandardButton.Yes
                    | QMessageBox.StandardButton.No)
                if answer != QMessageBox.StandardButton.Yes:
                    return None
        except OSError:
            pass
        cfg = self._config()
        spec = {"name": name, "images": list(self._images), "gcp": gcp or None,
                "photos_dir": folder, "out_dir": out_dir,
                "remove_after": cfg["remove_after"]}
        if builtin:
            spec.update(engine="native", options=options,
                        odm_dir=native.find_install(cfg["odm_path"]),
                        auto_concurrency=auto,
                        time_factor=self._time_factor())
            return spec
        options, dropped = presets.filter_supported(
            options, self._node_options or [])
        if dropped:
            self._append_log("Note: this engine does not know these options, "
                             "so they were left out: %s" % ", ".join(dropped))
        spec.update(engine="node", options=options, url=cfg["url"],
                    token=cfg["token"], keep_zip=cfg["keep_zip"])
        return spec

    def _limit_for_memory(self, options):
        """Built-in engine: do not let ODM start more parallel work than the
        free memory can carry (unless the user set max-concurrency)."""
        if "max-concurrency" in options:
            return options
        mem = native.memory_status()
        workers = native.safe_concurrency(mem, self.quality.currentData())
        if mem:
            self._append_log(
                "This computer has %.0f GB of memory, %.1f GB free right now."
                % (mem["total"], mem["free"]))
        if workers:
            options = dict(options)
            options["max-concurrency"] = workers
            self._append_log(
                "To stay within that, OpenDroneMap will work on %d photo%s at "
                "a time instead of %d (slower, but far less memory)."
                % (workers, "" if workers == 1 else "s", os.cpu_count() or 1))
        if mem and mem["free"] < 4.0:
            self._message("Only %.1f GB of memory is free. Close other "
                          "programs to give this job a better chance."
                          % mem["free"], "Warning")
        return options

    def _form_options(self):
        """ODM options for what is ticked in the panel right now (used by
        Resume). Returns None after telling the user what to fix."""
        products = dict(orthophoto=self.cb_ortho.isChecked(),
                        dsm=self.cb_dsm.isChecked(),
                        dtm=self.cb_dtm.isChecked(),
                        point_cloud=self.cb_pc.isChecked(),
                        model_3d=self.cb_3d.isChecked())
        if not any(products.values()):
            return self._stop("Tick at least one thing to create (step 3).")
        extra, problems = presets.parse_extra_options(self.extra_edit.text())
        if problems:
            return self._stop("These extra ODM options could not be "
                              "understood:\n%s" % "\n".join(problems),
                              self.extra_edit)
        options = presets.build_options(
            quality=self.quality.currentData(),
            ortho_res=self.ortho_res.value() or None,
            dem_res=self.dem_res.value() or None,
            extra=extra, report=self.cb_report.isChecked(), **products)
        return self._limit_for_memory(options)

    def _stop(self, text, focus=None):
        QMessageBox.information(self, "Easy Drone Mapping", text)
        if focus is not None:
            focus.setFocus()
        return None

    def run(self):
        if self.job is not None:
            return
        if self._builtin():
            self._check_builtin()
            if not self._engine_ready:
                if self.installer.busy:
                    self._run_when_ready = True
                    self._message("The engine is still installing; processing "
                                  "will begin as soon as it is ready.", "Info")
                elif self.install_engine():
                    # One click should be enough: install, then carry on.
                    self._run_when_ready = True
                return
        elif not self._engine_ready:
            if self.engine.busy or time.time() < self._waiting_for_engine_until:
                self._run_when_ready = True
                self._message("The engine is still starting; processing will "
                              "begin as soon as it is ready.", "Info")
                return
            if self._is_local():
                # One click should be enough: start the engine, then carry on.
                self._run_when_ready = True
                self.start_engine()
                return
            return self._stop("The engine at %s is not reachable. Check "
                              "Settings." % self._config()["url"])
        spec = self._collect()
        if spec is None:
            return
        self._save_form()
        self._append_log("Options sent to ODM: %s" % json.dumps(
            spec["options"], sort_keys=True))
        self._start_job(spec)

    def _start_job(self, spec):
        self.done_box.hide()
        self.resume_box.hide()
        self._last_result = None
        job = NativeJob(spec) if spec.get("engine") == "native" \
            else OdmJob(spec)
        job.stageChanged.connect(self.stage_label.setText)
        self._eta = None
        if hasattr(job, "etaChanged"):
            job.etaChanged.connect(self._on_eta)
        job.logLines.connect(self._append_log_lines)
        job.progressChanged.connect(self._on_progress)
        job.taskCreated.connect(self._remember_job)
        job.jobFinished.connect(self._on_job_finished)
        self.job = job
        self._job_started = time.time()
        self.progress.setValue(0)
        self.stage_label.setText("Starting…")
        self.clock_label.setText("")
        self.progress_box.show()
        self.cancel_btn.setEnabled(True)
        self.run_btn.setEnabled(False)
        self.run_btn.setText("Working…")
        self._lock_form(True)
        self._clock.start(1000)
        self._set_engine_state(self._engine_state, self.engine_text.text())
        QgsApplication.taskManager().addTask(job)

    def _lock_form(self, locked):
        for box in (self.box_photos, self.box_products, self.box_output):
            box.setEnabled(not locked)

    def _on_progress(self, value):
        self.progress.setValue(int(value * 10))

    def _on_eta(self, seconds):
        self._eta = (seconds, time.time())

    def _tick_clock(self):
        if self.job is None:
            return
        text = "Elapsed %s" % _human_time(time.time() - self._job_started)
        if self._eta:
            left = self._eta[0] - (time.time() - self._eta[1])
            text += " · %s left" % estimate.human(max(left, 60))
        self.clock_label.setText(
            text + ". You can keep working in QGIS meanwhile.")

    def cancel(self):
        if self.job is None:
            return
        answer = QMessageBox.question(
            self, "Cancel processing",
            "Stop this job? The work done so far will be thrown away.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes or self.job is None:
            return
        self.cancel_btn.setEnabled(False)
        self.stage_label.setText("Canceling…")
        self.job.request_cancel()

    def _on_job_finished(self, result):
        self.job = None
        self._clock.stop()
        self.progress_box.hide()
        self.run_btn.setEnabled(True)
        self.run_btn.setText("Process photos")
        self._lock_form(False)
        self._set_engine_state(self._engine_state, self.engine_text.text())
        self._last_result = result
        took = _human_time(result.get("seconds", 0))

        if result.get("ok"):
            self._forget_saved_job(silent=True)
            self._learn_pace(result)
            added = []
            try:
                added = results.load_results(result["files"], result["name"],
                                             self.iface)
            except Exception as e:  # noqa: BLE001
                log("Loading layers failed: %s" % e, "Critical")
            if added:
                text = ("<b>Done in %s.</b> Added to the map: %s. Tick the "
                        "other layers in the “%s” group to view them."
                        % (took, ", ".join(added), _html(result["name"])))
                if result.get("note"):
                    text += " " + _html(result["note"])
                if "point_cloud" in result["files"] \
                        and "Point cloud" not in added:
                    text += (" The point cloud is saved in the results "
                             "folder, but this QGIS installation could not "
                             "display it.")
            else:
                text = ("<b>Done in %s</b>, but no map layers were found in "
                        "the results. Open the results folder to check."
                        % took)
            self.done_label.setText(text)
            self.open_folder_btn.show()
            self.open_report_btn.setVisible("report" in result["files"])
            self.done_box.show()
            self._message("Drone mapping finished: %s" % result["name"],
                          "Success")
            return

        if result.get("canceled"):
            if result.get("resumable"):
                return  # QGIS is closing; the job carries on in the engine
            self._forget_saved_job(silent=True)
            self.done_label.setText("Canceled.")
            self.open_report_btn.hide()
            self.open_folder_btn.hide()
            self.done_box.show()
            return

        # ---- failure
        if result.get("engine_broken"):
            self._forget_saved_job(silent=True)
            self.done_label.setText(
                "<b>Not started.</b> %s" % _html(result.get("advice", "")))
            self.open_report_btn.hide()
            self.open_folder_btn.setVisible(bool(result.get("log_file")))
            self.done_box.show()
            self.log_btn.setChecked(True)
            answer_lines = "\n".join(result.get("engine_answer") or [])
            if result.get("engine_crash"):
                # A crash is not cured by reinstalling: say what it is.
                box = QMessageBox(self)
                box.setIcon(QMessageBox.Icon.Warning)
                box.setWindowTitle("OpenDroneMap crashed while starting")
                box.setText(result.get("advice", ""))
                page_btn = None
                if result.get("runtime_too_old"):
                    page_btn = box.addButton(
                        "Open Microsoft's download page",
                        QMessageBox.ButtonRole.ActionRole)
                box.addButton(QMessageBox.StandardButton.Close)
                if answer_lines:
                    box.setDetailedText(answer_lines)
                exec_dialog(box)
                if page_btn is not None and box.clickedButton() is page_btn:
                    QDesktopServices.openUrl(QUrl(native.VC_REDIST_PAGE))
            elif native.CAN_AUTO_INSTALL and not self._reinstall_tried:
                self._reinstall_tried = True
                answer = QMessageBox.question(
                    self, "The processing engine needs repairing",
                    "%s\n\nReinstall OpenDroneMap now (about %d MB download) "
                    "and then start this job again automatically?"
                    % (result.get("advice", ""), native.PINNED_SIZE_MB),
                    QMessageBox.StandardButton.Yes
                    | QMessageBox.StandardButton.No)
                if answer == QMessageBox.StandardButton.Yes \
                        and self.install_engine(confirm=False):
                    self._run_when_ready = True
            else:
                box = QMessageBox(self)
                box.setIcon(QMessageBox.Icon.Warning)
                box.setWindowTitle("The processing engine will not start")
                box.setText(
                    "OpenDroneMap still does not start, and reinstalling "
                    "again will not help. The details needed to fix this "
                    "are in the log (%s)."
                    % (result.get("log_file") or "Details, below"))
                if answer_lines:
                    box.setDetailedText(answer_lines)
                exec_dialog(box)
            return
        if result.get("resumable"):
            self._check_saved_job(force_show=True)
        else:
            self._forget_saved_job(silent=True)
        advice = result.get("advice") or ""
        error = result.get("error") or "Something went wrong."
        self.done_label.setText("<b>Not finished.</b> %s" % _html(advice or error))
        self.open_report_btn.hide()
        self.open_folder_btn.setVisible(bool(result.get("log_file")))
        self.done_box.show()
        self.log_btn.setChecked(True)
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Processing did not finish")
        box.setText(advice or error)
        details = []
        if advice:
            details.append("Engine message: %s" % error)
        if result.get("log_file"):
            details.append("Full log saved to: %s" % result["log_file"])
        if result.get("log_tail"):
            details.append("Last lines of the log:\n" +
                           "\n".join(str(l) for l in result["log_tail"]))
        if details:
            box.setDetailedText("\n\n".join(details))
        exec_dialog(box)

    def _open_results_folder(self):
        if self._last_result and self._last_result.get("out_dir"):
            QDesktopServices.openUrl(
                QUrl.fromLocalFile(self._last_result["out_dir"]))

    def _open_report(self):
        path = (self._last_result or {}).get("files", {}).get("report")
        if path:
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def _message(self, text, level="Info"):
        try:
            self.iface.messageBar().pushMessage(
                "Easy Drone Mapping", text, message_level(level), 8)
        except Exception:  # noqa: BLE001
            log(text, level)

    # ================================================================== resume
    def _remember_job(self, uuid):
        spec = self.job.spec if self.job is not None else {}
        saved = {"uuid": uuid, "name": spec.get("name", ""),
                 "out_dir": spec.get("out_dir", ""),
                 "engine": spec.get("engine", "node")}
        if saved["engine"] == "native":
            saved.update(photos_dir=spec.get("photos_dir", ""),
                         gcp=spec.get("gcp"), options=spec.get("options", {}))
        else:
            saved["url"] = spec.get("url", "")
        self._set("active_job", json.dumps(saved))

    def _saved_job(self):
        raw = self._get("active_job", "")
        if not raw:
            return None
        try:
            saved = json.loads(raw)
            return saved if saved.get("uuid") else None
        except ValueError:
            return None

    def _check_saved_job(self, force_show=False):
        saved = self._saved_job()
        if saved is None or self.job is not None:
            self.resume_box.hide()
            return
        is_native = saved.get("engine") == "native"
        if is_native != self._builtin() or (
                not is_native and saved.get("url") != self._config()["url"]):
            self.resume_box.hide()
            return
        if not force_show and self.resume_box.isVisible():
            return
        if is_native:
            self.resume_label.setText(
                "An earlier job, <b>%s</b>, did not finish. Resume carries on "
                "from the last completed step, using the settings shown "
                "below (change them first if you like)."
                % _html(saved["name"]))
        else:
            self.resume_label.setText(
                "An earlier job, <b>%s</b>, is still on the engine. Resume it "
                "to follow its progress and collect the results."
                % _html(saved["name"]))
        self.resume_box.show()

    def _resume_saved_job(self):
        saved = self._saved_job()
        if saved is None or self.job is not None:
            return
        if not self._engine_ready:
            self._stop("The engine has to be ready first (step 1), then click "
                       "Resume.")
            return
        cfg = self._config()
        if saved.get("engine") == "native":
            photos = saved.get("photos_dir") or ""
            images = presets.list_images(photos)
            if not images and not os.path.isdir(
                    os.path.join(saved.get("out_dir", ""), "images")):
                self._forget_saved_job(silent=True)
                self._stop("The photos for that job are no longer at\n%s\nso "
                           "it cannot be resumed." % photos)
                return
            options = self._form_options()
            if options is None:
                return
            self._save_form()
            self._append_log("Resuming with: %s" % json.dumps(
                options, sort_keys=True))
            self._start_job({
                "engine": "native", "name": saved.get("name") or "survey",
                "images": images, "gcp": saved.get("gcp"),
                "photos_dir": photos, "options": options,
                "auto_concurrency": "max-concurrency" not in
                presets.parse_extra_options(self.extra_edit.text())[0],
                "time_factor": self._time_factor(),
                "out_dir": saved["out_dir"],
                "odm_dir": native.find_install(cfg["odm_path"]),
                "remove_after": cfg["remove_after"]})
            return
        try:
            info = self._client().task_info(saved["uuid"])
        except NodeError:
            self._forget_saved_job(silent=True)
            self._stop("That job is no longer on the engine, so it cannot be "
                       "resumed.")
            return
        code = (info.get("status") or {}).get("code")
        if code in (nodeodm.FAILED, nodeodm.CANCELED):
            self._forget_saved_job(silent=True)
            self._stop("That job ended on the engine without results (%s)."
                       % nodeodm.STATUS_TEXT.get(code, "stopped"))
            return
        out_dir = saved.get("out_dir") or ""
        if not out_dir:
            out_dir = QFileDialog.getExistingDirectory(
                self, "Choose where to save the results")
            if not out_dir:
                return
        self._start_job({
            "engine": "node",
            "name": saved.get("name") or "survey", "images": [], "gcp": None,
            "options": {}, "out_dir": out_dir, "url": cfg["url"],
            "token": cfg["token"], "remove_after": cfg["remove_after"],
            "keep_zip": cfg["keep_zip"], "resume_uuid": saved["uuid"]})
        self._remember_job(saved["uuid"])

    def _forget_saved_job(self, *_args, silent=False):
        saved = self._saved_job()
        self._set("active_job", "")
        self.resume_box.hide()
        if saved and not silent and saved.get("engine") == "native":
            return
        if saved and not silent and self._engine_ready:
            # User pressed "Forget it": also free the engine's disk space.
            try:
                client = self._client()
                try:
                    client.task_cancel(saved["uuid"])
                except NodeError:
                    pass
                client.task_remove(saved["uuid"])
            except NodeError:
                pass

    # ============================================================ persistence
    def _save_form(self):
        self._set("quality", self.quality.currentData())
        for key, cb in (("p_ortho", self.cb_ortho), ("p_dsm", self.cb_dsm),
                        ("p_dtm", self.cb_dtm), ("p_pc", self.cb_pc),
                        ("p_3d", self.cb_3d)):
            self._set(key, cb.isChecked())

    def _load_settings(self):
        idx = self.quality.findData(self._get("quality", "balanced"))
        if idx >= 0:
            self.quality.setCurrentIndex(idx)
        for key, cb, default in (("p_ortho", self.cb_ortho, True),
                                 ("p_dsm", self.cb_dsm, True),
                                 ("p_dtm", self.cb_dtm, True),
                                 ("p_pc", self.cb_pc, True),
                                 ("p_3d", self.cb_3d, False)):
            cb.setChecked(self._get(key, default, bool))

    def shutdown(self):
        """Called when the plugin is unloaded."""
        self._probe_timer.stop()
        self._clock.stop()
        self.engine.abort()
        self.installer.cancel()
        if self._probe is not None and self._probe.isRunning():
            self._probe.wait(7000)


# ------------------------------------------------------------------ utilities
def _human_time(seconds):
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return "%d h %02d min" % (h, m)
    if m:
        return "%d min %02d s" % (m, s)
    return "%d s" % s


def _human_mb(mb):
    if mb >= 1024:
        return "%.1f GB" % (mb / 1024.0)
    return "%.0f MB" % mb if mb >= 1 else "under 1 MB"


def _html(text):
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace("\n", "<br>"))


def _free_folder(path):
    """Return path, or path_2, path_3… if it already holds files."""
    candidate, i = path, 2
    while os.path.isdir(candidate) and os.listdir(candidate):
        candidate = "%s_%d" % (path, i)
        i += 1
    return candidate
