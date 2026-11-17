# -*- coding: utf-8 -*-
"""Settings dialog and the first-time setup guide."""
import platform

from qgis.PyQt.QtWidgets import (QCheckBox, QComboBox, QDialog,
                                 QDialogButtonBox, QFileDialog, QFormLayout,
                                 QGroupBox, QHBoxLayout, QLineEdit,
                                 QPushButton, QTextBrowser, QVBoxLayout)

DEFAULT_URL = "http://localhost:3000"

SETUP_HTML = """
<h2>Getting started</h2>
<p>The photos are processed by the <b>OpenDroneMap</b> software, free open-source
photogrammetry software, running on your own computer. Your photos never
leave your machine.</p>

<h3>Windows: nothing to set up</h3>
<ol>
<li>Choose your photo folder and click <b>Process photos</b>.</li>
<li>The first time, the plugin asks to download OpenDroneMap (about 234&nbsp;MB)
from its official GitHub releases and installs it for your user account.
No administrator rights, no Docker, no other program to open.</li>
<li>Results are loaded into QGIS when they are ready.</li>
</ol>
<p>Processing runs while QGIS is open. If you close QGIS mid-job, open the
panel again and click <i>Resume</i>: it carries on from the last completed
step.</p>

<h3>macOS and Linux: Docker engine</h3>
<p>OpenDroneMap publishes a ready-made installer for Windows only, so on
these systems the plugin runs it inside Docker.</p>
<ul>
<li><b>macOS:</b> install
<a href="https://www.docker.com/products/docker-desktop/">Docker Desktop</a>
and start it once.</li>
<li><b>Linux:</b> <code>sudo apt install docker.io</code>, then
<code>sudo usermod -aG docker $USER</code> and log out and in again.</li>
</ul>
<p>Then click <i>Process photos</i>; the plugin starts the engine itself. The
first time it downloads the engine (a few GB).</p>

<h3>Tips for good results</h3>
<ul>
<li>Fly with 70–80&nbsp;% overlap between photos, camera pointing straight
down, and use the original files from the drone (they contain the GPS
positions).</li>
<li>Processing is heavy work. As a rough guide allow 16&nbsp;GB of memory for
a few hundred photos; use <i>Fast preview</i> on smaller machines.</li>
<li>For survey-grade accuracy add a ground control point file
(<code>gcp_list.txt</code>) under <i>More options</i>.</li>
<li>A more powerful computer on your network can do the work instead: choose
the Docker / network engine in Settings and enter its address.</li>
</ul>

<h3>Is this really free and legal?</h3>
<p>Yes. OpenDroneMap is open source (AGPL&nbsp;v3) and free for any use,
including commercial work. This plugin is open source (GPL&nbsp;v3). The
built-in Windows engine needs nothing else. If you use the Docker engine:
Docker Engine on Linux is open source; <b>Docker Desktop</b> (Windows/macOS)
is free for personal use, education, and businesses with fewer than 250
employees <i>and</i> less than US$10&nbsp;million annual revenue.</p>
"""


class HelpDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Easy Drone Mapping — setup guide")
        self.resize(620, 640)
        layout = QVBoxLayout(self)
        browser = QTextBrowser()
        browser.setOpenExternalLinks(True)
        browser.setHtml(SETUP_HTML)
        layout.addWidget(browser)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)


class SettingsDialog(QDialog):
    """Edits a plain dict of settings; the caller saves it."""

    def __init__(self, values, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Easy Drone Mapping — settings")
        self.setMinimumWidth(520)
        self.update_requested = False

        layout = QVBoxLayout(self)

        top = QFormLayout()
        layout.addLayout(top)
        self.engine = QComboBox()
        self.engine.addItem("Built-in OpenDroneMap (no Docker)", "builtin")
        self.engine.addItem("Docker on this computer, or another computer "
                            "on the network", "docker")
        self.engine.setCurrentIndex(
            max(0, self.engine.findData(values.get("engine", "builtin"))))
        self.engine.setToolTip(
            "Built-in: OpenDroneMap runs directly on this computer. On "
            "Windows the plugin installs it for you.\n"
            "Docker / network: uses a NodeODM engine, started in Docker on "
            "this computer or running on another machine.")
        self.engine.currentIndexChanged.connect(self._engine_changed)
        top.addRow("Processing engine", self.engine)

        # ---- built-in
        self.builtin_box = QGroupBox("Built-in engine")
        bform = QFormLayout(self.builtin_box)
        odm_row = QHBoxLayout()
        self.odm_path = QLineEdit(values.get("odm_path", ""))
        self.odm_path.setPlaceholderText(
            "chosen automatically" if platform.system() == "Windows"
            else "folder of a native OpenDroneMap install")
        self.odm_path.setToolTip(
            "Where OpenDroneMap is (or will be) installed. Leave empty to "
            "let the plugin decide.")
        odm_browse = QPushButton("Browse…")
        odm_browse.clicked.connect(self._browse_odm)
        odm_row.addWidget(self.odm_path)
        odm_row.addWidget(odm_browse)
        bform.addRow("OpenDroneMap folder", odm_row)
        layout.addWidget(self.builtin_box)

        # ---- docker / network
        self.docker_box = QGroupBox("Docker / network engine")
        form = QFormLayout(self.docker_box)
        layout.addWidget(self.docker_box)

        self.url = QLineEdit(values.get("url", DEFAULT_URL))
        self.url.setPlaceholderText(DEFAULT_URL)
        self.url.setToolTip(
            "Where the ODM engine listens. Leave as is to use this computer.\n"
            "To use a more powerful computer on your network, enter its "
            "address, e.g. http://192.168.1.50:3000")
        form.addRow("Engine address", self.url)

        self.token = QLineEdit(values.get("token", ""))
        self.token.setEchoMode(QLineEdit.EchoMode.Password)
        self.token.setPlaceholderText("usually empty")
        self.token.setToolTip("Only needed for engines protected by a token.")
        form.addRow("Access token", self.token)

        self.gpu = QCheckBox("Use NVIDIA graphics card (needs the NVIDIA "
                             "Container Toolkit)")
        self.gpu.setChecked(bool(values.get("gpu", False)))
        form.addRow("Speed", self.gpu)

        cli_row = QHBoxLayout()
        self.cli = QLineEdit(values.get("cli", ""))
        self.cli.setPlaceholderText("found automatically")
        self.cli.setToolTip("Only fill this in if Docker is installed in an "
                            "unusual place.")
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse_cli)
        cli_row.addWidget(self.cli)
        cli_row.addWidget(browse)
        form.addRow("Docker program", cli_row)

        self.keep_zip = QCheckBox("Keep the downloaded all.zip next to the "
                                  "results")
        self.keep_zip.setChecked(bool(values.get("keep_zip", False)))
        form.addRow("", self.keep_zip)

        form = QFormLayout()
        layout.addLayout(form)
        self.remove_after = QCheckBox(
            "Delete working files once the results are saved (frees a lot "
            "of disk space)")
        self.remove_after.setChecked(bool(values.get("remove_after", True)))
        form.addRow("Clean-up", self.remove_after)

        row = QHBoxLayout()
        self.update_btn = QPushButton("Update engine to the newest ODM…")
        self.update_btn.setToolTip("Downloads and installs the latest "
                                   "OpenDroneMap for the chosen engine.")
        self.update_btn.clicked.connect(self._request_update)
        row.addWidget(self.update_btn)
        row.addStretch(1)
        layout.addLayout(row)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save
                                   | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._engine_changed()

    def _engine_changed(self, *_):
        builtin = self.engine.currentData() == "builtin"
        self.builtin_box.setVisible(builtin)
        self.docker_box.setVisible(not builtin)
        self.update_btn.setVisible(
            not builtin or platform.system() == "Windows")
        self.adjustSize()

    def _browse_odm(self):
        folder = QFileDialog.getExistingDirectory(
            self, "Choose the OpenDroneMap folder", self.odm_path.text())
        if folder:
            self.odm_path.setText(folder)

    def _browse_cli(self):
        path, _ = QFileDialog.getOpenFileName(self, "Find the docker program")
        if path:
            self.cli.setText(path)

    def _request_update(self):
        self.update_requested = True
        self.accept()

    def values(self):
        return {
            "engine": self.engine.currentData(),
            "odm_path": self.odm_path.text().strip(),
            "url": self.url.text().strip() or DEFAULT_URL,
            "token": self.token.text().strip(),
            "gpu": self.gpu.isChecked(),
            "cli": self.cli.text().strip(),
            "remove_after": self.remove_after.isChecked(),
            "keep_zip": self.keep_zip.isChecked(),
        }
