# -*- coding: utf-8 -*-
"""Starts and stops the local ODM processing engine (NodeODM in a container).

Everything runs through QProcess, so QGIS never freezes while Docker pulls
the (large) engine image the first time.
"""
import os
import platform
import shutil

from qgis.PyQt.QtCore import QObject, QProcess, QTimer, pyqtSignal

CONTAINER = "easy-odm-engine"
VOLUME = "easy-odm-data"
IMAGE_CPU = "opendronemap/nodeodm:latest"
IMAGE_GPU = "opendronemap/nodeodm:gpu"


def find_container_cli(custom=""):
    """Locate the docker (or podman) command, even when it is not on PATH."""
    if custom and os.path.isfile(custom):
        return custom
    for name in ("docker", "podman"):
        found = shutil.which(name)
        if found:
            return found
    candidates = []
    system = platform.system()
    if system == "Windows":
        for env in ("ProgramFiles", "ProgramW6432"):
            base = os.environ.get(env)
            if base:
                candidates.append(os.path.join(
                    base, "Docker", "Docker", "resources", "bin", "docker.exe"))
                candidates.append(os.path.join(
                    base, "RedHat", "Podman", "podman.exe"))
    elif system == "Darwin":
        candidates += ["/usr/local/bin/docker", "/opt/homebrew/bin/docker",
                       "/Applications/Docker.app/Contents/Resources/bin/docker",
                       "/opt/homebrew/bin/podman", "/usr/local/bin/podman"]
    else:
        candidates += ["/usr/bin/docker", "/usr/local/bin/docker",
                       "/snap/bin/docker", "/usr/bin/podman"]
    for c in candidates:
        if os.path.isfile(c):
            return c
    return None


def find_docker_desktop():
    """Path of the Docker Desktop app, if installed (Windows / macOS)."""
    system = platform.system()
    if system == "Windows":
        for env in ("ProgramFiles", "ProgramW6432"):
            base = os.environ.get(env)
            if base:
                p = os.path.join(base, "Docker", "Docker", "Docker Desktop.exe")
                if os.path.isfile(p):
                    return p
    elif system == "Darwin":
        p = "/Applications/Docker.app"
        if os.path.exists(p):
            return p
    return None


def launch_docker_desktop():
    path = find_docker_desktop()
    if not path:
        return False
    if platform.system() == "Darwin":
        return QProcess.startDetached("open", ["-a", path])
    return QProcess.startDetached(path, [])


class EngineManager(QObject):
    """Runs a short sequence of container commands, one after the other."""

    log = pyqtSignal(str)            # raw text for the log window
    status = pyqtSignal(str)         # one-line human status
    finished = pyqtSignal(bool, str)  # ok, message

    DAEMON_WAIT_SECONDS = 150

    def __init__(self, parent=None):
        super().__init__(parent)
        self._proc = None
        self._steps = []
        self._cli = None
        self._busy = False
        self._buffer = ""
        self._daemon_waited = 0
        self._ok_message = ""

    @property
    def busy(self):
        return self._busy

    # ------------------------------------------------------------- public API
    def start_engine(self, cli_path="", gpu=False, port=3000):
        if self._busy:
            return
        self._cli = find_container_cli(cli_path)
        if not self._cli:
            self.finished.emit(False, "NO_DOCKER")
            return
        image = IMAGE_GPU if gpu else IMAGE_CPU
        run = ["run", "-d", "--name", CONTAINER, "--restart", "unless-stopped",
               "-p", "127.0.0.1:%d:3000" % int(port),
               "-v", "%s:/var/www/data" % VOLUME]
        if gpu:
            run += ["--gpus", "all"]
        run.append(image)
        self._ok_message = "Engine started."
        self._daemon_waited = 0
        self._steps = [
            ("Checking Docker…", ["ps", "-q"], "daemon"),
            ("Preparing engine…", ["rm", "-f", CONTAINER], "ignore"),
            ("Looking for the ODM engine image…",
             ["image", "inspect", "--format", "ok", image], "have_image"),
            ("Downloading the ODM engine (first time only, several GB — "
             "this can take a while)…", ["pull", image], "pull"),
            ("Starting engine…", run, "must"),
        ]
        self._busy = True
        self._next()

    def stop_engine(self, cli_path=""):
        if self._busy:
            return
        self._cli = find_container_cli(cli_path)
        if not self._cli:
            self.finished.emit(False, "NO_DOCKER")
            return
        self._ok_message = "Engine stopped."
        self._steps = [("Stopping engine…", ["stop", CONTAINER], "must")]
        self._busy = True
        self._next()

    def update_engine(self, cli_path="", gpu=False, port=3000):
        """Pull the newest engine image, then restart with it."""
        if self._busy:
            return
        self.start_engine(cli_path, gpu, port)
        if self._busy:
            # force the pull even though an image is already present
            self._steps = [s for s in self._steps if s[2] != "have_image"]
            self._ok_message = "Engine updated and started."

    def abort(self):
        self._steps = []
        if self._proc is not None:
            self._proc.kill()
        self._busy = False

    # --------------------------------------------------------------- internals
    def _next(self):
        if not self._steps:
            self._busy = False
            self.finished.emit(True, self._ok_message)
            return
        label, args, mode = self._steps.pop(0)
        self._mode = mode
        self._args = args
        self._label = label
        self.status.emit(label)
        if not (mode == "daemon" and self._daemon_waited):
            self.log.emit("$ %s %s" % (os.path.basename(self._cli),
                                       " ".join(args)))
        self._buffer = ""
        proc = QProcess(self)
        proc.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        proc.readyRead.connect(self._on_output)
        proc.finished.connect(self._on_finished)
        proc.errorOccurred.connect(self._on_error)
        self._proc = proc
        proc.start(self._cli, args)

    def _on_output(self):
        if self._proc is None:
            return
        text = self._proc.readAll().data().decode("utf-8", "replace")
        self._buffer = (self._buffer + text)[-6000:]
        for line in text.replace("\r", "\n").split("\n"):
            line = line.strip()
            if line:
                self.log.emit(line)

    def _on_error(self, error):
        # Only "failed to start" arrives without a finished() signal.
        if error != QProcess.ProcessError.FailedToStart:
            return
        proc, self._proc = self._proc, None
        if proc is not None:
            proc.deleteLater()
        self._fail("Could not run '%s'. Is Docker installed correctly?"
                   % self._cli)

    def _on_finished(self, exit_code, _exit_status=None):
        proc, self._proc = self._proc, None
        if proc is None:
            return
        proc.deleteLater()
        ok = (exit_code == 0)
        mode = self._mode

        if mode == "daemon" and not ok:
            self._wait_for_daemon()
            return
        if mode == "have_image":
            if ok:
                # Image already downloaded: skip the pull step.
                self._steps = [s for s in self._steps if s[2] != "pull"]
            self._next()
            return
        if mode == "ignore" or ok:
            self._next()
            return
        self._fail(self._explain(self._buffer))

    def _wait_for_daemon(self):
        """Docker is installed but not running: try to open it and wait."""
        if self._daemon_waited == 0:
            launched = launch_docker_desktop()
            if launched:
                self.status.emit("Opening Docker Desktop — waiting for it to "
                                 "be ready…")
            elif not _looks_like_daemon_down(self._buffer):
                self._fail(self._explain(self._buffer))
                return
            elif find_docker_desktop() is None:
                self._fail("DOCKER_NOT_RUNNING")
                return
        self._daemon_waited += 5
        if self._daemon_waited > self.DAEMON_WAIT_SECONDS:
            self._fail("DOCKER_NOT_RUNNING")
            return
        self._steps.insert(0, (self._label, self._args, "daemon"))
        QTimer.singleShot(5000, self._retry_if_busy)

    def _retry_if_busy(self):
        if self._busy:
            self._next()

    def _fail(self, message):
        self._steps = []
        self._busy = False
        self.finished.emit(False, message)

    @staticmethod
    def _explain(output):
        low = output.lower()
        if "permission denied" in low and "docker.sock" in low:
            return ("Docker is installed, but your user is not allowed to use "
                    "it. On Linux run:  sudo usermod -aG docker $USER  then "
                    "log out and back in.")
        if "port is already allocated" in low or "address already in use" in low \
                or "ports are not available" in low:
            return ("Port 3000 is already used by another program (maybe "
                    "WebODM or another engine). Stop the other program, or "
                    "set the engine address in Settings to another port, for "
                    "example http://localhost:3001")
        if "could not select device driver" in low or "nvidia-container" in low:
            return ("Docker could not use the NVIDIA GPU. Install the NVIDIA "
                    "Container Toolkit, or untick 'Use NVIDIA GPU' in Settings.")
        if "no space left" in low:
            return "The disk is full. Free up space and try again."
        if "tls handshake" in low or "no such host" in low \
                or "i/o timeout" in low or "connection refused" in low:
            if _looks_like_daemon_down(output):
                return "DOCKER_NOT_RUNNING"
            return ("The engine could not be downloaded. Check the internet "
                    "connection and try again.")
        if _looks_like_daemon_down(output):
            return "DOCKER_NOT_RUNNING"
        tail = [l for l in output.strip().splitlines() if l.strip()][-3:]
        return "Docker reported a problem:\n" + "\n".join(tail)


def _looks_like_daemon_down(output):
    low = output.lower()
    return any(s in low for s in (
        "cannot connect to the docker daemon", "is the docker daemon running",
        "docker_engine", "failed to connect to the docker api",
        "error during connect", "docker desktop is not running",
        "the system cannot find the file specified", "cannot connect to podman"))
