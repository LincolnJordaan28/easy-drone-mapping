# -*- coding: utf-8 -*-
"""Background job for the built-in engine: runs ODM directly as a program.

No upload and no download: ODM works straight in the results folder.
"""
import os
import queue
import re
import shutil
import signal
import subprocess
import threading
import time

from qgis.core import QgsTask
from qgis.PyQt.QtCore import pyqtSignal

from . import estimate, native
from .job import explain_failure, find_results
from .nodeodm import Canceled

P_PREPARE = (0.0, 4.0)
P_PROCESS = (4.0, 98.0)


class EngineBroken(Exception):
    """ODM itself will not start: the installation needs repairing."""

FINISHED_MARKER = "ODM app finished"
MARKER_FILE = ".easy_odm_images"

# Rough share of the work done when each ODM stage starts (used only if the
# progress broadcast cannot be heard).
STAGE_START = {"dataset": 0, "split": 1, "merge": 1, "opensfm": 2,
               "openmvs": 40, "odm_filterpoints": 55, "odm_meshing": 58,
               "mvs_texturing": 65, "odm_georeferencing": 82, "odm_dem": 86,
               "odm_orthophoto": 92, "odm_report": 98, "odm_postprocess": 99}
STAGE_LABEL = {"dataset": "reading the photos", "opensfm": "matching photos",
               "openmvs": "building the 3D point cloud",
               "odm_filterpoints": "cleaning the point cloud",
               "odm_meshing": "building the surface",
               "mvs_texturing": "texturing the surface",
               "odm_georeferencing": "placing the model on the map",
               "odm_dem": "creating elevation models",
               "odm_orthophoto": "creating the orthophoto",
               "odm_report": "writing the quality report",
               "odm_postprocess": "finishing"}

# Bulky intermediate folders nobody needs once the products exist.
WORKING_DIRS = ("opensfm", "odm_filterpoints", "odm_meshing",
                "odm_texturing_25d", "submodels")


class NativeJob(QgsTask):
    stageChanged = pyqtSignal(str)
    etaChanged = pyqtSignal(float)          # estimated seconds left
    logLines = pyqtSignal(list)
    taskCreated = pyqtSignal(str)
    jobFinished = pyqtSignal(dict)

    def __init__(self, spec):
        """spec keys: name, images, gcp, options, out_dir, odm_dir,
        remove_after"""
        super().__init__("Drone mapping: %s" % spec.get("name", "job"),
                         QgsTask.Flag.CanCancel)
        self.spec = dict(spec)
        self.user_canceled = False
        self.result = {"ok": False, "canceled": False, "error": "",
                       "advice": "", "resumable": False, "files": {},
                       "out_dir": spec.get("out_dir", ""),
                       "name": spec.get("name", ""), "log_tail": [],
                       "seconds": 0, "uuid": ""}
        self._log = []
        self._log_file = None
        self._started = time.time()
        self._proc = None
        self._process_started = False
        self._direct = 0            # start method: 0 standard, 1-2 fallbacks
        self._last_code = 0
        self._work_dir = spec.get("out_dir", "")   # plain-path alias, see run()
        self._last_stage = ""
        self._tracker = None
        self._shown = 0.0

    # ----------------------------------------------------------------- helpers
    def request_cancel(self):
        self.user_canceled = True
        self.cancel()

    def _phase(self, phase, fraction):
        lo, hi = phase
        self.setProgress(lo + (hi - lo) * max(0.0, min(1.0, fraction)))

    def _say(self, text):
        self.stageChanged.emit(text)

    def _add_log(self, lines):
        if not lines:
            return
        self._log.extend(lines)
        if len(self._log) > 3000:
            del self._log[:-2000]
        if self._log_file is not None:
            try:
                self._log_file.write("\n".join(lines) + "\n")
                self._log_file.flush()
            except OSError:
                pass
        self.logLines.emit(list(lines))

    # --------------------------------------------------------------------- run
    def run(self):
        spec = self.spec
        out_dir = spec["out_dir"]
        try:
            os.makedirs(out_dir, exist_ok=True)
            log_path = os.path.join(out_dir, "processing_log.txt")
            self._log_file = open(log_path, "a", encoding="utf-8")
            self.result["log_file"] = log_path

            self._self_test()
            self._use_plain_path()
            self._prepare_images()
            self.taskCreated.emit("native")
            ok = self._process()
            if not ok and self._last_stage in ("odm_report",
                                               "odm_postprocess") \
                    and not native.is_crash_code(self._last_code):
                made = find_results(out_dir)
                if any(k in made for k in ("orthophoto", "dsm", "dtm",
                                           "point_cloud")):
                    # The map products are finished; only ODM's PDF quality
                    # report could not be written. Not worth failing the job.
                    ok = True
                    self.result["note"] = ("OpenDroneMap could not write "
                                           "its PDF quality report; the map "
                                           "products themselves are complete.")
                    self._add_log([self.result["note"]])
            if not ok:
                opts = spec.get("options") or {}
                self.result["advice"] = explain_failure(
                    self._log, native=True,
                    model_3d=not (opts.get("skip-3dmodel")
                                  or opts.get("fast-orthophoto")))
                self.result["error"] = "OpenDroneMap stopped with an error."
                if native.is_crash_code(self._last_code):
                    self.result["error"] = "OpenDroneMap crashed (%s)." % \
                        native.crash_text(self._last_code)
                    self.result["advice"] = (
                        "%s The lines just before the crash, in the log, "
                        "show which step it was in."
                        % self.result["error"])
                self.result["resumable"] = True
                return False
            if spec.get("remove_after", True):
                self._say("Tidying up working files…")
                self._tidy()
            self.result["files"] = find_results(out_dir)
            self.result["ok"] = True
            self.setProgress(100)
            return True
        except Canceled:
            self.result["canceled"] = True
            self._kill()
            if self.user_canceled:
                self._tidy()            # throw the half-done work away
            else:
                # QGIS is closing: keep the work so far, so Resume can pick
                # up from the last finished stage.
                self.result["resumable"] = self._process_started
            return False
        except EngineBroken as e:
            self.result["engine_broken"] = True
            self.result["error"] = str(e)
            if self.result.get("runtime_too_old"):
                self.result["advice"] = (
                    "OpenDroneMap crashed while starting because this "
                    "computer's Microsoft Visual C++ runtime is too old for "
                    "it. Install the latest \"Microsoft Visual C++ "
                    "Redistributable (x64)\" from Microsoft (free; needs "
                    "administrator rights), restart QGIS and try again.")
            elif self.result.get("engine_crash"):
                self.result["advice"] = (
                    "%s Reinstalling will not help; the log shows where it "
                    "crashed." % e)
            else:
                self.result["advice"] = (
                    "OpenDroneMap is installed but will not start, so the "
                    "installation is incomplete or damaged. Reinstalling it "
                    "normally fixes this.")
            return False
        except OSError as e:
            self.result["error"] = "File problem: %s" % e
            return False
        except Exception as e:  # noqa: BLE001
            self.result["error"] = "Unexpected problem: %s: %s" % (
                type(e).__name__, e)
            return False
        finally:
            self._kill()
            self.result["seconds"] = int(time.time() - self._started)
            self.result["log_tail"] = self._log[-25:]
            if self._log_file is not None:
                try:
                    self._log_file.close()
                except OSError:
                    pass

    # ------------------------------------------------------------------ phases
    def _use_plain_path(self):
        """Give ODM a space-free name for the results folder (see native)."""
        out_dir = os.path.abspath(self.spec["out_dir"])
        work, note = native.plain_path_for(out_dir)
        self._work_dir = work
        if note:
            self._add_log([note])
        marker = os.path.join(out_dir, native.CLASSIFIED_MARKER)
        plain_now = native.path_is_plain(work)
        opts = self.spec.get("options") or {}
        laz = os.path.join(out_dir, "odm_georeferencing",
                           "odm_georeferenced_model.laz")
        dtm = os.path.join(out_dir, "odm_dem", "dtm.tif")
        if (opts.get("dtm") and os.path.isfile(laz)
                and not os.path.isfile(dtm) and not os.path.isfile(marker)
                and "rerun-from" not in opts and "rerun-all" not in opts):
            # The existing point cloud has no ground points marked (it was
            # made without the terrain model ticked, or while spaces in the
            # folder name broke that step). Redo from the step that marks
            # them; everything before it is kept.
            opts = dict(opts)
            opts["rerun-from"] = "odm_georeferencing"
            self.spec["options"] = opts
            self._add_log(["The existing point cloud has no ground points "
                           "marked, which the terrain model needs, so the "
                           "last steps are redone from the point-cloud step. "
                           "The long steps before it are kept."])
        if plain_now and opts.get("dtm"):
            try:
                with open(marker, "w") as f:
                    f.write("ground classification run via a plain path\n")
            except OSError:
                pass

    def _prepare_images(self):
        """ODM wants the photos in <project>/images. Hard links cost no disk
        space; if those are impossible (other drive), copy."""
        images = list(self.spec.get("images") or [])
        out_dir = self.spec["out_dir"]
        target = os.path.join(out_dir, "images")
        os.makedirs(target, exist_ok=True)
        with open(os.path.join(out_dir, MARKER_FILE), "w") as f:
            f.write("images folder created by Easy Drone Mapping\n")
        n = len(images) or 1
        copied = 0
        for i, src in enumerate(images, 1):
            if self.isCanceled():
                raise Canceled()
            dst = os.path.join(target, os.path.basename(src))
            if not (os.path.exists(dst)
                    and os.path.getsize(dst) == os.path.getsize(src)):
                if os.path.exists(dst):
                    os.remove(dst)
                try:
                    os.link(src, dst)
                except OSError:
                    shutil.copy2(src, dst)
                    copied += 1
            if i % 5 == 0 or i == n:
                self._say("Preparing photos… %d of %d" % (i, n))
                self._phase(P_PREPARE, i / n)
        if copied:
            self._add_log(["%d photos were copied into the results folder "
                           "(different drive)." % copied])

    def _command(self, overrides=None):
        spec = self.spec
        out_dir = os.path.abspath(self._work_dir or spec["out_dir"])
        runner = native.runner_in(spec["odm_dir"])
        if not runner:
            raise OSError("OpenDroneMap was not found in %s" % spec["odm_dir"])
        args = ["--project-path", os.path.dirname(out_dir),
                os.path.basename(out_dir)]
        opts = dict(spec.get("options") or {})
        opts.update(overrides or {})
        args += native.option_args(opts)
        if spec.get("gcp"):
            gcp = spec["gcp"]
            if not native.path_is_plain(gcp):
                # same reason: hand ODM a copy under the plain path
                copy = os.path.join(spec["out_dir"], "gcp_list.txt")
                if not (os.path.exists(copy) and os.path.samefile(gcp, copy)):
                    shutil.copy2(gcp, copy)
                gcp = os.path.join(out_dir, "gcp_list.txt")
            args += ["--gcp", gcp]
        return runner, args

    def _spawn(self, runner, args, wrapper_name):
        """Start ODM's launcher with a clean environment."""
        out_dir = self._work_dir or self.spec["out_dir"]
        env = native.clean_environment()
        env["no_ansiesc"] = "1"          # plain log text, no colour codes
        kwargs = dict(stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                      stdin=subprocess.DEVNULL, env=env, cwd=out_dir)
        if native.IS_WINDOWS:
            target = os.path.join(out_dir, wrapper_name)
            wrapper = None
            if self._direct:
                wrapper = native.write_direct_wrapper(
                    target, self.spec["odm_dir"], args,
                    with_site=(self._direct == 2))
            if wrapper is None:
                wrapper = native.write_windows_wrapper(target, runner, args)
            return self._popen_batch(wrapper, kwargs)
        return subprocess.Popen([runner] + args, start_new_session=True,
                                **kwargs)

    @staticmethod
    def _popen_batch(batch_file, kwargs):
        comspec = os.environ.get("COMSPEC", "cmd.exe")
        return subprocess.Popen(
            'cmd.exe /d /s /c ""%s""' % batch_file, executable=comspec,
            creationflags=0x08000000,   # no console window
            **kwargs)

    def _probe(self):
        """Ask ODM's Python how it sees itself (for the log)."""
        if not native.IS_WINDOWS:
            return []
        out_dir = self.spec["out_dir"]
        path = os.path.join(out_dir, "run_odm_probe.bat")
        try:
            native.write_probe(path, self.spec["odm_dir"])
            proc = self._popen_batch(path, dict(
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, env=native.clean_environment(),
                cwd=out_dir))
            out, _ = proc.communicate(timeout=60)
            lines = [l.rstrip() for l in out.decode("utf-8", "replace")
                     .splitlines() if l.strip()]
            return ["--- How ODM's Python sees itself ---"] + lines[:60]
        except Exception as e:  # noqa: BLE001
            return ["(probe failed: %s)" % e]
        finally:
            try:
                os.remove(path)
            except OSError:
                pass

    def _odm_starts(self, runner):
        """Ask ODM for its version: quick proof that it can start at all."""
        try:
            proc = self._spawn(runner, ["--version"], "run_odm_check.bat")
            out, _ = proc.communicate(timeout=180)
        except subprocess.TimeoutExpired:
            proc.kill()
            return False, ["(no answer from OpenDroneMap within 3 minutes)"]
        except OSError as e:
            return False, ["(could not start: %s)" % e]
        finally:
            try:
                os.remove(os.path.join(self.spec["out_dir"],
                                       "run_odm_check.bat"))
            except OSError:
                pass
        text = _ANSI.sub("", out.decode("utf-8", "replace"))
        lines = [l.rstrip() for l in text.splitlines() if l.strip()]
        self._last_code = proc.returncode
        # "ODM x.y.z" is printed only after every module has loaded, so it is
        # proof of a working install.
        if re.search(r"^ODM \d+\.\d+", text, re.MULTILINE):
            return True, lines
        # Clear evidence of a broken start: a Python error, or a crash.
        bad = native.is_crash_code(proc.returncode) or any(
            marker in text for marker in (
                "Traceback (most recent call last)", "ModuleNotFoundError",
                "Windows fatal exception",
                "is not recognized as an internal or external command"))
        if native.is_crash_code(proc.returncode):
            lines.append("(OpenDroneMap crashed while starting: %s)"
                         % native.crash_text(proc.returncode))
        return (not bad), lines

    def _self_test(self):
        """Check ODM starts before spending time on a job; repair if not."""
        odm_dir = self.spec["odm_dir"]
        runner = native.runner_in(odm_dir) if odm_dir else None
        if not runner:
            raise EngineBroken("OpenDroneMap was not found in %s" % odm_dir)
        self._say("Checking the engine…")
        try:
            notes = native.ensure_ready(odm_dir)
        except OSError as e:
            notes = ["Could not repair the install: %s" % e]
        self._add_log(notes)
        try:
            self._add_log(native.ensure_vc_runtime(odm_dir))
        except Exception as e:  # noqa: BLE001
            self._add_log(["(Visual C++ runtime check failed: %s)" % e])
        strays = native.python_variables_in_qgis()
        if strays:
            self._add_log(["Kept away from OpenDroneMap (QGIS's own Python "
                           "settings): " + ", ".join(strays)])
        ok, lines = self._odm_starts(runner)
        crashed = native.is_crash_code(self._last_code)
        if not ok and not crashed:
            # Repeat ODM's own post-install step, then try once more.
            script = native.postinstall_script(odm_dir)
            if script and native.IS_WINDOWS:
                self._add_log(["OpenDroneMap did not start; repeating its "
                               "post-install step…"])
                try:
                    subprocess.run(
                        [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c",
                         script], stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                        creationflags=0x08000000, timeout=120,
                        env=native.clean_environment())
                except Exception as e:  # noqa: BLE001
                    self._add_log(["(post-install step failed: %s)" % e])
                try:
                    self._add_log(native.ensure_ready(odm_dir))
                except OSError:
                    pass
                ok, lines = self._odm_starts(runner)
        if not ok and native.IS_WINDOWS:
            # Fallback start methods: ODM's own compiled modules first on the
            # search path (1), plus its package folder named explicitly (2).
            first_lines, first_code = lines, self._last_code
            for method in (1, 2):
                self._direct = method
                ok2, lines2 = self._odm_starts(runner)
                if ok2:
                    self._add_log([
                        "The normal start failed (%s), but OpenDroneMap "
                        "starts with start method %d; using that."
                        % (native.crash_text(first_code)
                           if native.is_crash_code(first_code)
                           else "Python error", method)]
                        + _crash_excerpt(first_lines))
                    return
            self._direct = 0
            lines, self._last_code = first_lines, first_code
        if ok:
            return
        crashed = native.is_crash_code(self._last_code)
        self.result["engine_answer"] = _crash_excerpt(lines) or lines[-12:]
        self._add_log(["OpenDroneMap will not start. Its answer was:"]
                      + lines[-60:] + native.diagnose(odm_dir)
                      + self._probe())
        if crashed:
            self.result["engine_crash"] = True
            self.result["runtime_too_old"] = bool(
                native.IS_WINDOWS and native.vc_runtime_too_old())
            raise EngineBroken("OpenDroneMap crashed while starting (%s)."
                               % native.crash_text(self._last_code))
        raise EngineBroken("OpenDroneMap will not start.")

    def _plan(self):
        """Cores for each part of the job.

        Photo matching and texturing need a lot of memory per core, so the
        panel limits them to what the free memory allows. The dense point
        cloud and meshing steps need much less, so when that limit is low
        they are run separately with more cores. ODM keeps finished steps,
        so splitting the job into consecutive runs costs only seconds.
        """
        opts = self.spec.get("options") or {}
        cores = os.cpu_count() or 1
        light = int(opts.get("max-concurrency") or cores)
        dense = light
        if self.spec.get("auto_concurrency") and not opts.get("fast-orthophoto") \
                and "rerun-from" not in opts and light < cores:
            mem = native.memory_status()
            dense = max(light, estimate.dense_cores(
                mem and mem["free"], opts.get("pc-quality"), cores))
        return light, dense

    def _process(self):
        opts = self.spec.get("options") or {}
        light, dense = self._plan()
        n_photos = len(self.spec.get("images") or []) or 100
        self._tracker = estimate.Tracker(
            estimate.stage_seconds(opts, n_photos, light, dense,
                                   self.spec.get("time_factor", 1.0)),
            time.time())
        self.result["estimate"] = estimate.total(self._tracker.est)
        self._shown = 0.0
        self._add_log(["Running OpenDroneMap %s"
                       % native.installed_version(self.spec["odm_dir"])])
        if dense > light:
            self._add_log([
                "Photo matching and texturing use %d core%s (limited by free "
                "memory); the 3D point cloud steps use %d of %d cores."
                % (light, "" if light == 1 else "s", dense,
                   os.cpu_count() or 1)])
            runs = [("opensfm", light), ("odm_meshing", dense), (None, light)]
        else:
            runs = [(None, light)]
        self._add_log(["Estimated time: %s"
                       % estimate.human(self.result["estimate"])])
        for end_with, cores in runs:
            overrides = {"max-concurrency": cores}
            if end_with:
                overrides["end-with"] = end_with
            if not self._run_once(overrides):
                return False
        return True

    def _run_once(self, overrides):
        runner, args = self._command(overrides)
        self._add_log(["Options: " + " ".join(args[3:])])
        self._proc = self._spawn(runner, args, "run_odm.bat")
        self._process_started = True

        lines = queue.Queue()
        reader = threading.Thread(target=_pump, args=(self._proc.stdout, lines),
                                  daemon=True)
        reader.start()

        tracker = self._tracker
        finished_ok = False
        last_eta = 0.0
        if not self._last_stage:
            self._say("Processing…")
        while True:
            if self.isCanceled():
                raise Canceled()
            batch = []
            try:
                while len(batch) < 200:
                    batch.append(lines.get_nowait())
            except queue.Empty:
                pass
            ended = None in batch
            batch = [b for b in batch if b is not None]
            now = time.time()
            for line in batch:
                if FINISHED_MARKER in line:
                    finished_ok = True
                if "Running " in line and " stage" in line:
                    name = line.split("Running ", 1)[1].split(" stage")[0].strip()
                    if name in STAGE_START and tracker.start(name, now):
                        self._last_stage = name
                        self._say("Processing: %s…"
                                  % STAGE_LABEL.get(name, name))
                elif "Finished " in line and " stage" in line:
                    name = line.split("Finished ", 1)[1].split(" stage")[0]
                    tracker.finish(name.strip(), now)
            self._add_log(batch)

            self._shown = max(self._shown, tracker.progress(now))
            self._phase(P_PROCESS, self._shown)
            if now - last_eta > 2:
                last_eta = now
                self.etaChanged.emit(tracker.seconds_left(now))

            if ended:
                break
            if not batch:
                time.sleep(0.25)

        code = self._proc.wait()
        self._last_code = code
        self._proc = None
        self.result["calibration"] = tracker.calibration()
        if finished_ok:
            return True
        self._add_log(["OpenDroneMap exited with code %s" % code])
        return False

    def _tidy(self):
        out_dir = self.spec["out_dir"]
        doomed = list(WORKING_DIRS)
        if os.path.isfile(os.path.join(out_dir, MARKER_FILE)):
            doomed.append("images")     # only ever the folder we created
        for name in doomed:
            shutil.rmtree(os.path.join(out_dir, name), ignore_errors=True)
        if self._work_dir and os.path.abspath(self._work_dir) != \
                os.path.abspath(out_dir):
            native.remove_link(self._work_dir)
        for name in (MARKER_FILE, "run_odm.bat", native.CLASSIFIED_MARKER,
                     ".easy_odm_plain_path"):
            try:
                os.remove(os.path.join(out_dir, name))
            except OSError:
                pass

    def _kill(self):
        proc, self._proc = self._proc, None
        if proc is None or proc.poll() is not None:
            return
        try:
            if native.IS_WINDOWS:
                subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                               stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL,
                               creationflags=0x08000000, timeout=30)
            else:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except Exception:  # noqa: BLE001
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass

    # ---------------------------------------------------------------- finished
    def finished(self, _ok):
        self.jobFinished.emit(self.result)


def _crash_excerpt(lines):
    """The useful part of a crash report: the newest calls come first."""
    for i, line in enumerate(lines):
        if "Windows fatal exception" in line:
            return lines[i:i + 14]
    return []


_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _pump(stream, out_queue):
    """Reader thread: forward the program's output line by line."""
    try:
        for raw in iter(stream.readline, b""):
            out_queue.put(_ANSI.sub("", raw.decode("utf-8", "replace")
                                    .rstrip("\r\n")))
    except Exception:  # noqa: BLE001
        pass
    finally:
        out_queue.put(None)
