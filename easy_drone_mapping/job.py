# -*- coding: utf-8 -*-
"""Background job: upload photos -> process -> download -> unpack.

Runs as a QgsTask, so it shows up in the QGIS task manager (status bar) and
QGIS stays fully usable while it works.
"""
import os
import time
import zipfile

from qgis.core import QgsTask
from qgis.PyQt.QtCore import pyqtSignal

from . import nodeodm
from .nodeodm import Canceled, NodeClient, NodeError

# Share of the overall progress bar given to each phase
P_UPLOAD = (0.0, 12.0)
P_PROCESS = (12.0, 90.0)
P_DOWNLOAD = (90.0, 97.0)
P_EXTRACT = (97.0, 100.0)

RESULT_FILES = {
    "orthophoto": ["odm_orthophoto/odm_orthophoto.tif"],
    "dsm": ["odm_dem/dsm.tif"],
    "dtm": ["odm_dem/dtm.tif"],
    "point_cloud": ["odm_georeferencing/odm_georeferenced_model.copc.laz",
                    "odm_georeferencing/odm_georeferenced_model.laz"],
    "cameras": ["odm_report/shots.geojson"],
    "report": ["odm_report/report.pdf"],
    "model_3d": ["odm_texturing/odm_textured_model_geo.obj",
                 "odm_texturing/odm_textured_model.obj"],
}


def find_results(out_dir):
    """Map result kind -> existing, non-empty file path."""
    found = {}
    for kind, candidates in RESULT_FILES.items():
        for rel in candidates:
            path = os.path.join(out_dir, *rel.split("/"))
            if os.path.isfile(path) and os.path.getsize(path) > 0:
                found[kind] = path
                break
    return found


def explain_failure(lines, native=False, model_3d=False):
    """Turn the tail of an ODM log into advice a non-expert can act on."""
    text = "\n".join(lines[-400:]).lower()
    if "cannot determine reader for input file" in text or (
            "bad allocation" in text and "will split dem generation into 0 "
            "tiles" in text):
        return ("The terrain model (DTM) could not be made because no ground "
                "points were found. With OpenDroneMap 3.6 this happens when "
                "the folder path contains spaces. Click Resume: the plugin "
                "now works around that and redoes only the last steps.")
    if any(s in text for s in ("memoryerror", "bad_alloc", "out of memory",
                               "not enough memory", "cannot allocate memory",
                               "insufficient memory", "ran out of memory",
                               "child returned 3221226505",
                               "exited with code 137", "child returned 137",
                               "killed")):
        if native:
            texturing = "texrecon" in text or "mvstex" in text
            model_3d = texturing and model_3d
            tips = ["close other programs first (web browsers especially)"]
            if model_3d:
                tips.insert(0, "untick '3D textured model' unless you need "
                               "it: that is the step that ran out")
            tips.append("keep plenty of free space on drive C: (Windows uses "
                        "it as overflow memory)")
            return ("The computer ran out of memory%s. To get further: %s. "
                    "Then click Resume: it carries on from the last finished "
                    "step, using the settings shown in the panel."
                    % (" while texturing" if texturing else "",
                       "; ".join(tips)))
        return ("The computer ran out of memory. Try the 'Fast preview' "
                "quality, close other programs, or give Docker more memory "
                "(Docker Desktop → Settings → Resources).")
    if "no space left" in text or "disk quota" in text:
        if native:
            return ("The disk filled up during processing. Free up space on "
                    "the drive with the results folder and try again.")
        return ("The disk filled up during processing. Free up space (Docker "
                "stores its data on the system drive) and try again.")
    if any(s in text for s in ("not enough images", "not enough features",
                               "could not find any match", "no reconstruction",
                               "reconstruction could not be", "not enough overlap",
                               "the program could not process this dataset")):
        return ("ODM could not stitch these photos together. This usually "
                "means too little overlap between photos (aim for 70–80 %), "
                "too few photos, or very uniform ground such as water or "
                "sand. Check the photos all belong to one flight.")
    if "illegal instruction" in text or "child returned 132" in text:
        return ("This computer's processor is too old for the standard ODM "
                "engine.")
    if "does not have gps" in text or "no gps" in text:
        return ("The photos have no GPS positions, so the map cannot be "
                "placed on Earth. Add a ground control point file, or use "
                "photos straight from the drone (not edited copies).")
    return ("Processing stopped with an error. The last lines of the log "
            "below usually say why.")


class OdmJob(QgsTask):
    stageChanged = pyqtSignal(str)          # short status line
    logLines = pyqtSignal(list)             # new log lines
    taskCreated = pyqtSignal(str)           # engine task id (for resuming)
    jobFinished = pyqtSignal(dict)          # result summary (main thread)

    def __init__(self, spec):
        """spec keys: name, images, gcp, options, out_dir, url, token,
        resume_uuid, remove_after, keep_zip"""
        super().__init__("Drone mapping: %s" % spec.get("name", "job"),
                         QgsTask.Flag.CanCancel)
        self.spec = dict(spec)
        self.user_canceled = False
        self.uuid = spec.get("resume_uuid") or None
        self.result = {"ok": False, "canceled": False, "error": "",
                       "advice": "", "resumable": False, "files": {},
                       "out_dir": spec.get("out_dir", ""),
                       "name": spec.get("name", ""), "log_tail": [],
                       "seconds": 0}
        self._log = []
        self._started = time.time()
        self._created_here = False
        self._committed = bool(self.uuid)
        self._failed_on_engine = False

    # ----------------------------------------------------------------- helpers
    def request_cancel(self):
        """Called from the Cancel button (as opposed to QGIS shutting down)."""
        self.user_canceled = True
        self.cancel()

    def _phase(self, phase, fraction):
        lo, hi = phase
        self.setProgress(lo + (hi - lo) * max(0.0, min(1.0, fraction)))

    def _say(self, text):
        self.stageChanged.emit(text)

    def _add_log(self, lines):
        if lines:
            self._log.extend(lines)
            self.logLines.emit(list(lines))

    def _sleep(self, seconds):
        end = time.time() + seconds
        while time.time() < end:
            if self.isCanceled():
                raise Canceled()
            time.sleep(0.2)

    # --------------------------------------------------------------------- run
    def run(self):
        spec = self.spec
        client = None
        try:
            client = NodeClient(spec["url"], spec.get("token", ""))
            if not self.uuid:
                self._upload(client)
            self._monitor(client)
            zip_path = self._download(client)
            self._extract(zip_path)
            if not spec.get("keep_zip"):
                try:
                    os.remove(zip_path)
                except OSError:
                    pass
            if spec.get("remove_after", True):
                try:
                    client.task_remove(self.uuid)
                except NodeError:
                    pass
            self.result["files"] = find_results(spec["out_dir"])
            self.result["ok"] = True
            self.setProgress(100)
            return True
        except Canceled:
            self.result["canceled"] = True
            self._cleanup_after_cancel()
            return False
        except NodeError as e:
            self.result["error"] = str(e)
            if self._failed_on_engine and spec.get("remove_after", True):
                try:  # a failed job only wastes disk space on the engine
                    client.task_remove(self.uuid)
                except NodeError:
                    pass
            return False
        except Exception as e:  # never let a background thread die silently
            self.result["error"] = "Unexpected problem: %s: %s" % (
                type(e).__name__, e)
            return False
        finally:
            self.result["seconds"] = int(time.time() - self._started)
            self.result["log_tail"] = self._log[-25:]
            self.result["uuid"] = self.uuid or ""

    def _cleanup_after_cancel(self):
        if not self.user_canceled:
            # QGIS is closing: leave the job running on the engine so it can
            # be picked up again next time.
            self.result["resumable"] = self._committed
            return
        if self.uuid:
            try:
                client = NodeClient(self.spec["url"], self.spec.get("token", ""))
                if self._committed:
                    client.task_cancel(self.uuid)
                client.task_remove(self.uuid)
            except NodeError:
                pass

    # ------------------------------------------------------------------ phases
    def _upload(self, client):
        spec = self.spec
        files = list(spec["images"])
        if spec.get("gcp"):
            files.append(spec["gcp"])
        total_bytes = sum(os.path.getsize(f) for f in files) or 1

        self._say("Creating job on the engine…")
        self.uuid = client.task_init(spec["name"], spec["options"])
        self._created_here = True

        sent = 0
        n = len(files)
        for i, path in enumerate(files, 1):
            if self.isCanceled():
                raise Canceled()
            self._say("Uploading photos… %d of %d" % (i, n))
            client.task_upload(self.uuid, path, is_canceled=self.isCanceled)
            sent += os.path.getsize(path)
            self._phase(P_UPLOAD, sent / total_bytes)

        self._say("Starting processing…")
        client.task_commit(self.uuid)
        self._committed = True
        self.taskCreated.emit(self.uuid)

    def _monitor(self, client):
        line = 0
        failures = 0
        last_stage = ""
        while True:
            if self.isCanceled():
                raise Canceled()
            try:
                info = client.task_info(self.uuid)
                new = client.task_output(self.uuid, line)
                failures = 0
            except NodeError as e:
                failures += 1
                if "not found" in str(e).lower() or "invalid" in str(e).lower():
                    raise NodeError("The engine no longer knows this job. It "
                                    "may have been removed or the engine was "
                                    "reset.")
                if failures >= 60:
                    self.result["resumable"] = True
                    raise NodeError(
                        "Lost contact with the engine for several minutes. "
                        "If it is still working, reopen this panel later and "
                        "choose Resume.")
                self._say("Engine not answering — retrying…")
                self._sleep(5)
                continue

            if new:
                line += len(new)
                self._add_log(new)

            status = info.get("status") or {}
            code = status.get("code")
            progress = float(info.get("progress") or 0)

            if code == nodeodm.QUEUED:
                stage = "Waiting in the engine's queue…"
            elif code == nodeodm.RUNNING:
                stage = "Processing on the engine…"
                self._phase(P_PROCESS, progress / 100.0)
            elif code == nodeodm.COMPLETED:
                self._phase(P_PROCESS, 1.0)
                return
            elif code == nodeodm.CANCELED:
                raise NodeError("The job was canceled on the engine.")
            elif code == nodeodm.FAILED:
                self._failed_on_engine = True
                self._save_failure_log()
                self.result["advice"] = explain_failure(self._log)
                detail = status.get("errorMessage") or "Processing failed."
                raise NodeError(detail)
            else:
                stage = "Processing…"
            if stage != last_stage:
                self._say(stage)
                last_stage = stage
            self._sleep(2)

    def _download(self, client):
        out_dir = self.spec["out_dir"]
        os.makedirs(out_dir, exist_ok=True)
        zip_path = os.path.join(out_dir, "all.zip")
        last = [0.0]

        def progress(received, total):
            now = time.time()
            if now - last[0] < 0.3 and received != total:
                return
            last[0] = now
            if total:
                self._phase(P_DOWNLOAD, received / total)
                self._say("Downloading results… %d of %d MB"
                          % (received // 1048576, max(total // 1048576, 1)))
            else:
                self._say("Downloading results… %d MB" % (received // 1048576))

        self._say("Downloading results…")
        error = None
        for attempt in range(3):
            try:
                return client.download_all(self.uuid, zip_path, progress,
                                           self.isCanceled)
            except NodeError as e:
                error = e
                self._sleep(3 * (attempt + 1))
        self.result["resumable"] = True
        raise error

    def _extract(self, zip_path):
        out_dir = os.path.realpath(self.spec["out_dir"])
        self._say("Unpacking results…")
        try:
            with zipfile.ZipFile(zip_path) as z:
                members = z.infolist()
                total = sum(m.file_size for m in members) or 1
                done = 0
                for m in members:
                    if self.isCanceled():
                        raise Canceled()
                    target = os.path.realpath(os.path.join(out_dir, m.filename))
                    if not (target == out_dir
                            or target.startswith(out_dir + os.sep)):
                        continue  # never write outside the results folder
                    z.extract(m, out_dir)
                    done += m.file_size
                    self._phase(P_EXTRACT, done / total)
        except zipfile.BadZipFile:
            self.result["resumable"] = True
            raise NodeError("The downloaded results file is damaged. "
                            "Choose Resume to download it again.")
        except OSError as e:
            self.result["resumable"] = True
            raise NodeError("Could not unpack the results into %s (%s). "
                            "Is the disk full?" % (out_dir, e))

    def _save_failure_log(self):
        try:
            out_dir = self.spec["out_dir"]
            os.makedirs(out_dir, exist_ok=True)
            path = os.path.join(out_dir, "processing_log.txt")
            with open(path, "w", encoding="utf-8") as f:
                f.write("\n".join(self._log))
            self.result["log_file"] = path
        except OSError:
            pass

    # ---------------------------------------------------------------- finished
    def finished(self, _ok):
        """Runs on the main thread once run() returns."""
        self.jobFinished.emit(self.result)
