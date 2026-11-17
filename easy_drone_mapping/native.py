# -*- coding: utf-8 -*-
"""Built-in engine: OpenDroneMap installed natively, no Docker.

On Windows the plugin can fetch OpenDroneMap's own signed installer from the
project's official GitHub releases and install it silently for the current
user (no administrator rights). On other systems an existing native ODM
folder can be pointed to in Settings.
"""
import os
import platform
import tempfile

from qgis.core import QgsFileDownloader, QgsNetworkContentFetcher
from qgis.PyQt.QtCore import QObject, QProcess, QUrl, pyqtSignal

IS_WINDOWS = platform.system() == "Windows"
# The one-click installer exists for Windows only.
CAN_AUTO_INSTALL = IS_WINDOWS

# A release known to ship a working Windows installer.
PINNED_VERSION = "3.6.2"
PINNED_URL = ("https://github.com/OpenDroneMap/ODM/releases/download/"
              "v3.6.2/ODM_Setup_3.6.2.exe")
PINNED_SIZE_MB = 234
LATEST_API = "https://api.github.com/repos/OpenDroneMap/ODM/releases/latest"
TRUSTED_PREFIX = "https://github.com/OpenDroneMap/ODM/releases/download/"

# ODM brings its own Python, GDAL and PROJ. Anything QGIS (or another plugin)
# has put in the environment for *its* Python must not reach ODM's. The
# dangerous ones are PYTHONEXECUTABLE and __PYVENV_LAUNCHER__: either makes
# ODM's Python believe it lives somewhere else, so it no longer finds its own
# packages ("No module named 'vmem'").
_ENV_PREFIXES_TO_DROP = (
    "PYTHON", "__PYVENV", "_OLD_VIRTUAL", "VIRTUAL_ENV", "CONDA", "PYENV",
    "QGIS", "QT_", "GDAL", "PROJ_", "PDAL", "OSGEO4W", "O4W_", "GEOTIFF",
    "GRASS", "GISBASE", "SSL_CERT", "ODM", "CPL_", "OGR_", "VSI_",
    "USE_PATH_FOR_GDAL", "FONTCONFIG", "GS_LIB", "JPEGMEM", "TCL_", "TK_",
    "SAGA", "OTB_")


def _dropped(name):
    return name.upper().startswith(_ENV_PREFIXES_TO_DROP)


def python_variables_in_qgis():
    """Names of Python-related variables present in QGIS's environment."""
    return sorted(k for k in os.environ
                  if k.upper().startswith(("PYTHON", "__PYVENV", "VIRTUAL_ENV",
                                           "CONDA")))


def default_install_dir():
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    path = os.path.join(base, "EasyDroneMapping", "ODM")
    if IS_WINDOWS and (" " in path or not path.isascii()):
        # ODM's toolchain is happiest in a plain path; this is also where
        # OpenDroneMap's own installer puts it by default.
        return "C:\\ODM"
    return path


def runner_in(folder):
    """Path of ODM's launcher inside an install folder, or None."""
    if not folder:
        return None
    if not os.path.isfile(os.path.join(folder, "run.py")):
        return None
    for name in (("run.bat",) if IS_WINDOWS else ("run.sh", "run")):
        path = os.path.join(folder, name)
        if os.path.isfile(path):
            return path
    return None


def find_install(custom=""):
    """Return the folder of a usable native ODM, or None."""
    candidates = [custom, default_install_dir()]
    if IS_WINDOWS:
        candidates.append("C:\\ODM")
    for folder in candidates:
        if folder and runner_in(folder):
            return folder
    return None


def installed_version(folder):
    try:
        with open(os.path.join(folder, "VERSION"), encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def python_exe(folder):
    """ODM's own Python interpreter (the location differs between releases)."""
    for rel in (("venv", "Scripts", "python.exe"),
                (".pixi", "envs", "gpu-prod", "python.exe"),
                (".pixi", "envs", "prod", "python.exe")):
        path = os.path.join(folder, *rel)
        if os.path.isfile(path):
            return path
    return None


def _read_home(cfg_path):
    try:
        with open(cfg_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                key, _, value = line.partition("=")
                if key.strip().lower() == "home":
                    return value.strip()
    except OSError:
        pass
    return None


def ensure_ready(folder):
    """Make sure ODM's bundled Python can find its own packages.

    ODM releases up to 3.6.x keep their packages in <ODM>\\venv and rely on a
    small file, venv\\pyvenv.cfg, that the installer writes in its last step.
    If that step did not happen (or the folder was moved), Python starts but
    cannot import ODM's modules. Writing the file is safe to repeat.
    Returns a list of notes about what was fixed.
    """
    notes = []
    scripts = os.path.join(folder, "venv", "Scripts")
    if os.path.isdir(scripts):
        cfg = os.path.join(folder, "venv", "pyvenv.cfg")
        home = _read_home(cfg)
        if home is None or os.path.normcase(os.path.normpath(home)) != \
                os.path.normcase(os.path.normpath(scripts)):
            with open(cfg, "w", encoding="utf-8") as f:
                f.write("home = %s\ninclude-system-site-packages = false\n"
                        % scripts)
            notes.append("Repaired ODM's Python environment file "
                         "(venv\\pyvenv.cfg).")
    return notes


def postinstall_script(folder):
    """ODM's own post-install script, when this release has one."""
    path = os.path.join(folder, "winpostinstall.bat")
    return path if os.path.isfile(path) else None


def diagnose(folder):
    """Plain facts about an install, for the log when ODM will not start."""
    lines = ["--- OpenDroneMap installation check ---", "Folder: %s" % folder,
             "Version file: %s" % (installed_version(folder) or "missing")]
    py = python_exe(folder)
    lines.append("ODM's Python: %s" % (py or "NOT FOUND"))
    cfg = os.path.join(folder, "venv", "pyvenv.cfg")
    if os.path.isdir(os.path.join(folder, "venv")):
        lines.append("venv\\pyvenv.cfg: %s" % (
            "home = %s" % _read_home(cfg) if os.path.isfile(cfg)
            else "MISSING"))
    for rel in (("venv", "Lib", "site-packages"),
                (".pixi", "envs", "gpu-prod", "Lib", "site-packages")):
        sp = os.path.join(folder, *rel)
        if os.path.isdir(sp):
            try:
                names = os.listdir(sp)
            except OSError:
                names = []
            lines.append("%s: %d entries; vmem %s; numpy %s" % (
                os.path.join(*rel), len(names),
                "present" if "vmem" in names else "MISSING",
                "present" if "numpy" in names else "MISSING"))
    for rel in (("SuperBuild", "install", "bin"), ("opendm",), ("stages",)):
        p = os.path.join(folder, *rel)
        lines.append("%s: %s" % (os.path.join(*rel),
                                 "present" if os.path.isdir(p) else "MISSING"))
    if IS_WINDOWS:
        winver = odm_python_winver(folder)
        others = registered_python_paths(winver)
        lines.append("Other Python %s registered in Windows: %s"
                     % (winver, "; ".join(others) if others else "none"))
        st = vc_runtime_status()
        lines.append("Visual C++ runtime in Windows: %s (%s)"
                     % (_ver_text(st["system"]), st["system_path"]))
        lines.append("Visual C++ runtime QGIS uses: %s (%s)"
                     % (_ver_text(st["qgis"]), st["qgis_path"]))
        for rel in (("venv", "Scripts"), ("SuperBuild", "install", "bin")):
            p = os.path.join(folder, *rel, "msvcp140.dll")
            lines.append("Visual C++ runtime in ODM %s: %s"
                         % (os.path.join(*rel), _ver_text(file_version(p))))
    return lines


# ---------------------------------------------------------------------------
# Microsoft Visual C++ runtime
#
# ODM 3.6.x is built with Visual Studio 2022 17.10 or newer. Programs built
# that way crash with an access violation (0xC0000005) when Windows hands them
# an msvcp140.dll older than 14.40. The copy in System32 is whatever Visual
# C++ redistributable happens to be installed, and on many computers that is
# older. QGIS brings its own, newer copy; putting that copy next to ODM's
# programs makes Windows use it for ODM too (no administrator rights needed).
MIN_VC_RUNTIME = (14, 40)
VC_RUNTIME_DLLS = ("msvcp140.dll", "msvcp140_1.dll", "msvcp140_2.dll",
                   "msvcp140_atomic_wait.dll", "msvcp140_codecvt_ids.dll",
                   "vcruntime140.dll", "vcruntime140_1.dll", "concrt140.dll",
                   "vcomp140.dll")
VC_REDIST_PAGE = ("https://learn.microsoft.com/cpp/windows/"
                  "latest-supported-vc-redist")


def file_version(path):
    """(major, minor, build, revision) of a Windows program file, or None."""
    if not IS_WINDOWS or not path or not os.path.isfile(path):
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class FixedInfo(ctypes.Structure):
            _fields_ = [(n, wintypes.DWORD) for n in (
                "dwSignature", "dwStrucVersion", "dwFileVersionMS",
                "dwFileVersionLS", "dwProductVersionMS", "dwProductVersionLS",
                "dwFileFlagsMask", "dwFileFlags", "dwFileOS", "dwFileType",
                "dwFileSubtype", "dwFileDateMS", "dwFileDateLS")]

        ver = ctypes.WinDLL("version")
        ver.GetFileVersionInfoSizeW.argtypes = [wintypes.LPCWSTR,
                                                ctypes.c_void_p]
        ver.GetFileVersionInfoSizeW.restype = wintypes.DWORD
        ver.GetFileVersionInfoW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD,
                                            wintypes.DWORD, ctypes.c_void_p]
        ver.VerQueryValueW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR,
                                       ctypes.POINTER(ctypes.c_void_p),
                                       ctypes.POINTER(wintypes.UINT)]
        size = ver.GetFileVersionInfoSizeW(path, None)
        if not size:
            return None
        buf = ctypes.create_string_buffer(size)
        if not ver.GetFileVersionInfoW(path, 0, size, buf):
            return None
        ptr, length = ctypes.c_void_p(), wintypes.UINT()
        if not ver.VerQueryValueW(buf, "\\", ctypes.byref(ptr),
                                  ctypes.byref(length)) or not ptr.value:
            return None
        info = ctypes.cast(ptr, ctypes.POINTER(FixedInfo)).contents
        return (info.dwFileVersionMS >> 16, info.dwFileVersionMS & 0xFFFF,
                info.dwFileVersionLS >> 16, info.dwFileVersionLS & 0xFFFF)
    except Exception:  # noqa: BLE001
        return None


def loaded_module_path(name):
    """Full path of a DLL that QGIS itself has loaded, or None."""
    if not IS_WINDOWS:
        return None
    try:
        import ctypes
        k32 = ctypes.WinDLL("kernel32")
        k32.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]
        k32.GetModuleHandleW.restype = ctypes.c_void_p
        k32.GetModuleFileNameW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p,
                                           ctypes.c_uint32]
        handle = k32.GetModuleHandleW(name)
        if not handle:
            return None
        buf = ctypes.create_unicode_buffer(1024)
        if not k32.GetModuleFileNameW(handle, buf, 1024):
            return None
        return buf.value
    except Exception:  # noqa: BLE001
        return None


def _system32():
    return os.path.join(os.environ.get("SystemRoot", "C:\\Windows"),
                        "System32")


def _ver_text(version):
    return ".".join(str(n) for n in version) if version else "not found"


def vc_runtime_status():
    """Versions of the Visual C++ runtime Windows has and QGIS uses."""
    system_path = os.path.join(_system32(), "msvcp140.dll")
    qgis_path = loaded_module_path("msvcp140.dll")
    return {"system": file_version(system_path), "system_path": system_path,
            "qgis": file_version(qgis_path), "qgis_path": qgis_path}


def vc_runtime_too_old(status=None):
    status = status or vc_runtime_status()
    return status["system"] is None or \
        tuple(status["system"][:2]) < MIN_VC_RUNTIME


def ensure_vc_runtime(odm_dir):
    """Give ODM a new enough Visual C++ runtime when Windows' own is too old.

    Copies QGIS's own (newer) runtime files next to ODM's programs. Does
    nothing when the system runtime is already new enough. Returns notes.
    """
    if not IS_WINDOWS:
        return []
    status = vc_runtime_status()
    if not vc_runtime_too_old(status):
        return []
    notes = ["Windows' Visual C++ runtime is version %s; OpenDroneMap needs "
             "%d.%d or newer." % ((_ver_text(status["system"]),)
                                  + MIN_VC_RUNTIME)]
    source_dir = None
    if status["qgis"] and tuple(status["qgis"][:2]) >= MIN_VC_RUNTIME \
            and status["qgis_path"]:
        candidate = os.path.dirname(status["qgis_path"])
        if os.path.normcase(candidate) != os.path.normcase(_system32()):
            source_dir = candidate
    if source_dir is None:
        notes.append("QGIS has no newer copy to lend (QGIS uses %s from %s)."
                     % (_ver_text(status["qgis"]), status["qgis_path"]))
        return notes
    copied = 0
    for target in (os.path.join(odm_dir, "venv", "Scripts"),
                   os.path.join(odm_dir, "SuperBuild", "install", "bin")):
        if not os.path.isdir(target):
            continue
        for name in VC_RUNTIME_DLLS:
            src = os.path.join(source_dir, name)
            dst = os.path.join(target, name)
            if not os.path.isfile(src):
                continue
            new, old = file_version(src), file_version(dst)
            if old is not None and new is not None and old >= new:
                continue
            try:
                import shutil
                shutil.copy2(src, dst)
                copied += 1
            except OSError as e:
                notes.append("Could not copy %s: %s" % (name, e))
    notes.append("Gave OpenDroneMap QGIS's newer runtime (%s, %d files "
                 "copied from %s)." % (_ver_text(status["qgis"]), copied,
                                       source_dir))
    return notes


def is_crash_code(code):
    """True for Windows 'the program crashed' exit codes (0xC0000005 etc.)."""
    try:
        return (int(code) & 0xFFFFFFFF) >= 0xC0000000
    except (TypeError, ValueError):
        return False


def crash_text(code):
    code = int(code) & 0xFFFFFFFF
    names = {0xC0000005: "access violation", 0xC000001D: "illegal instruction",
             0xC0000135: "a required DLL was not found",
             0xC0000139: "DLL entry point not found",
             0xC0000409: "stack buffer overrun",
             0xC00000FD: "stack overflow"}
    return "Windows error 0x%08X%s" % (
        code, " (%s)" % names[code] if code in names else "")


def odm_python_winver(odm_dir):
    """'3.12' for an ODM whose Python is python312.dll (or None)."""
    scripts = os.path.join(odm_dir, "venv", "Scripts")
    try:
        for name in os.listdir(scripts):
            low = name.lower()
            if low.startswith("python3") and low.endswith(".dll") \
                    and low[7:-4].isdigit() and low != "python3.dll":
                return "3.%s" % low[7:-4]
    except OSError:
        pass
    return None


def registered_python_paths(winver):
    """Folders that another Python of this version has registered in Windows.

    Python adds these to its module search path ahead of its own folder, so a
    different Python installation can end up supplying ODM's compiled modules.
    """
    if not IS_WINDOWS or not winver:
        return []
    found = []
    try:
        import winreg
        for hive, label in ((winreg.HKEY_CURRENT_USER, "HKCU"),
                            (winreg.HKEY_LOCAL_MACHINE, "HKLM")):
            try:
                key = winreg.OpenKey(
                    hive, "Software\\Python\\PythonCore\\%s\\PythonPath"
                    % winver)
            except OSError:
                continue
            try:
                try:
                    value = winreg.QueryValue(key, None)
                    if value:
                        found.append("%s: %s" % (label, value))
                except OSError:
                    pass
                i = 0
                while True:
                    try:
                        sub = winreg.EnumKey(key, i)
                    except OSError:
                        break
                    try:
                        value = winreg.QueryValue(key, sub)
                        if value:
                            found.append("%s\\%s: %s" % (label, sub, value))
                    except OSError:
                        pass
                    i += 1
            finally:
                winreg.CloseKey(key)
    except Exception:  # noqa: BLE001
        pass
    return found


# ---------------------------------------------------------------------------
# Folder names with spaces
#
# ODM 3.6.x runs some of its helper programs without quoting file names (the
# ground-classification step, for one). With a space anywhere in the results
# folder's path, e.g. "D:\\Rooipoort\\Raw Photos\\...", that step silently
# fails, no ground points are found and the terrain model (DTM) cannot be
# made. So ODM is given a plain, space-free name for the same folder: a link
# (a "junction" on Windows; no administrator rights needed). The files still
# live in the results folder the user chose.
# Written once a run that classifies ground points (needed for the DTM) has
# gone through a space-free path.
CLASSIFIED_MARKER = ".easy_odm_ground_classified"


def path_is_plain(path):
    return " " not in path and path.isascii()


def _same_folder(a, b):
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def remove_link(link):
    """Remove a link made by plain_path_for (never the folder it points to)."""
    for remover in (os.rmdir, os.unlink):
        try:
            remover(link)   # on a link/junction these remove only the link
            return
        except OSError:
            continue


def plain_path_for(out_dir):
    """A space-free path that leads to out_dir, or out_dir itself.

    Returns (path, note). 'note' is text for the log, or "".
    """
    out_dir = os.path.abspath(out_dir)
    if path_is_plain(out_dir):
        return out_dir, ""
    import hashlib
    name = "job_" + hashlib.md5(
        os.path.normcase(out_dir).encode("utf-8")).hexdigest()[:10]
    bases = []
    local = os.environ.get("LOCALAPPDATA")
    if local:
        bases.append(os.path.join(local, "EasyDroneMapping", "jobs"))
    drive = os.path.splitdrive(out_dir)[0]
    if drive:
        bases.append(drive + os.sep + "EasyDroneMapping_jobs")
    if IS_WINDOWS:
        bases.append("C:\\EasyDroneMapping_jobs")
    else:
        bases.append(os.path.join(os.path.expanduser("~"), ".easy_odm_jobs"))
        bases.append(os.path.join(tempfile.gettempdir(), "easy_odm_jobs"))
    for base in bases:
        link = os.path.join(base, name)
        if not path_is_plain(link):
            continue
        try:
            os.makedirs(base, exist_ok=True)
            if os.path.lexists(link) and not _same_folder(link, out_dir):
                remove_link(link)
            if not os.path.lexists(link):
                if IS_WINDOWS:
                    import subprocess
                    subprocess.run(
                        [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c",
                         "mklink", "/J", link, out_dir],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        stdin=subprocess.DEVNULL, creationflags=0x08000000,
                        timeout=30)
                else:
                    os.symlink(out_dir, link, target_is_directory=True)
            if os.path.isdir(link) and _same_folder(link, out_dir):
                return link, ("The results folder's name contains spaces, "
                              "which OpenDroneMap cannot handle in every "
                              "step, so it is given this plain name for the "
                              "same folder: %s" % link)
        except Exception:  # noqa: BLE001
            continue
    return out_dir, ("Warning: the results folder's path contains spaces and "
                     "no plain link could be made. Some steps (the terrain "
                     "model in particular) may fail; a folder without spaces "
                     "in its path avoids this.")


def memory_status():
    """{'total': GB, 'free': GB} of physical memory, or None if unknown."""
    try:
        if IS_WINDOWS:
            import ctypes

            class Status(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong),
                            ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong),
                            ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong),
                            ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong),
                            ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
            st = Status()
            st.dwLength = ctypes.sizeof(Status)
            if not ctypes.windll.kernel32.GlobalMemoryStatusEx(
                    ctypes.byref(st)):
                return None
            return {"total": st.ullTotalPhys / 1073741824.0,
                    "free": st.ullAvailPhys / 1073741824.0}
        info = {}
        with open("/proc/meminfo") as f:
            for line in f:
                key, _, rest = line.partition(":")
                info[key] = float(rest.split()[0]) / 1048576.0   # kB -> GB
        return {"total": info["MemTotal"],
                "free": info.get("MemAvailable", info.get("MemFree", 0.0))}
    except Exception:  # noqa: BLE001
        return None


def top_memory_users(limit=5):
    """[(program name, GB)] of the programs using the most memory (Windows).

    Programs with several processes (web browsers) are added together.
    """
    if not IS_WINDOWS:
        return []
    try:
        import csv
        import io
        import subprocess
        out = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"], capture_output=True,
            timeout=15, creationflags=0x08000000).stdout
        totals = {}
        for row in csv.reader(io.StringIO(out.decode("mbcs", "replace"))):
            if len(row) < 5:
                continue
            kb = "".join(ch for ch in row[4] if ch.isdigit())
            if kb:
                name = row[0]
                totals[name] = totals.get(name, 0) + int(kb)
        ranked = sorted(totals.items(), key=lambda kv: -kv[1])
        skip = {"system", "memory compression", "registry", "svchost.exe",
                "dwm.exe", "explorer.exe"}
        return [(n, kb / 1048576.0) for n, kb in ranked
                if n.lower() not in skip and kb > 200 * 1024][:limit]
    except Exception:  # noqa: BLE001
        return []


def safe_concurrency(memory, quality="balanced", cores=None):
    """How many photos ODM should work on at once with this much free memory.

    ODM's default is one per processor core, which on a many-core computer
    with modest memory runs out of memory in the first minutes. This is a
    cautious rule of thumb (about 2 GB of free memory per worker, 3 GB for
    'High detail'), not a guarantee. Returns None when nothing needs limiting.
    """
    cores = cores or os.cpu_count() or 1
    if not memory:
        return None
    per_worker = 3.0 if quality == "high" else 2.0
    workers = int(max(memory["free"] - 1.0, 0.0) // per_worker)
    workers = max(1, min(cores, workers))
    return workers if workers < cores else None


def clean_environment():
    """Copy of the environment without QGIS's Python/GDAL/Qt settings."""
    env = {k: v for k, v in os.environ.items() if not _dropped(k)}
    path_key = next((k for k in env if k.upper() == "PATH"), "PATH")
    kept = []
    for part in env.get(path_key, "").split(os.pathsep):
        low = part.lower()
        if "qgis" in low or "osgeo4w" in low:
            continue
        kept.append(part)
    env[path_key] = os.pathsep.join(kept)
    # If ODM's Python crashes, make it say where; and never hold output back.
    env["PYTHONFAULTHANDLER"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    # ODM draws its report diagrams with matplotlib. Without this it tries to
    # open a window toolkit (Tk), which fails when another Python of the same
    # version is installed on the computer. "Agg" draws straight to files.
    env["MPLBACKEND"] = "Agg"
    return env


def option_args(options):
    """{'dsm': True, 'pc-quality': 'high'} -> ['--dsm', '--pc-quality', 'high']"""
    args = []
    for name, value in options.items():
        if value is None or value is False:
            continue
        args.append("--" + name)
        if value is not True:
            if isinstance(value, float) and value.is_integer():
                value = int(value)
            args.append(str(value))
    return args


def _bat_quote(arg):
    arg = str(arg).replace("%", "%%")
    if arg and not any(c in arg for c in ' \t"&()^|<>=;,!\''):
        return arg
    return '"%s"' % arg.replace('"', '""')


def write_windows_wrapper(path, run_bat, args):
    """A small batch file with the full command, so quoting is under our
    control whatever characters the folders contain."""
    line = "call %s %s" % (_bat_quote(run_bat),
                           " ".join(_bat_quote(a) for a in args))
    with open(path, "w", encoding="utf-8", newline="\r\n") as f:
        f.write("@echo off\nchcp 65001 >nul\n%s\nexit /b %%ERRORLEVEL%%\n"
                % line)
    return path


def _bat_text(text):
    return str(text).replace("%", "%%")


def write_direct_wrapper(path, odm_dir, args, with_site=False):
    """Self-contained launcher for the venv layout (ODM up to 3.6.x).

    Uses ODM's own win32env.bat for the environment, then puts the folder
    holding ODM's own Python extension modules FIRST on PYTHONPATH and names
    ODM's Python by its full path.

    Why: Python on Windows also adds the folders of any *other* Python of the
    same version that is registered on the computer (python.org installer,
    Anaconda, ...) to its search path, ahead of its own folder. ODM's Python
    then loads that other Python's compiled modules (for example _ctypes) and
    crashes with an access violation. PYTHONPATH entries come before those
    registered folders, so ODM's own modules win again. Child processes
    inherit the setting.

    with_site=True additionally names ODM's package folder, for the case
    where Python does not detect its environment file.
    Returns None when this install does not have that layout.
    """
    scripts = os.path.join(odm_dir, "venv", "Scripts")
    python = os.path.join(scripts, "python.exe")
    site = os.path.join(odm_dir, "venv", "Lib", "site-packages")
    env_bat = os.path.join(odm_dir, "win32env.bat")
    run_py = os.path.join(odm_dir, "run.py")
    if not all(os.path.exists(p) for p in (python, site, env_bat, run_py)):
        return None
    first = [scripts] + ([site] if with_site else [])
    lines = [
        "@echo off",
        "chcp 65001 >nul",
        'cd /d "%s"' % _bat_text(odm_dir),
        'call "%s"' % _bat_text(env_bat),
        'set "PYTHONPATH=%s;%%PYTHONPATH%%"' % _bat_text(";".join(first)),
        '"%s" -X utf8 "%s" %s <NUL' % (
            _bat_text(python), _bat_text(run_py),
            " ".join(_bat_quote(a) for a in args)),
        "exit /b %ERRORLEVEL%", ""]
    with open(path, "w", encoding="utf-8", newline="\r\n") as f:
        f.write("\n".join(lines))
    return path


def write_probe(path, odm_dir):
    """Batch file that reports how ODM's Python sees itself."""
    env_bat = os.path.join(odm_dir, "win32env.bat")
    code = ("import sys,os;print('executable:',sys.executable);"
            "print('prefix:',sys.prefix);"
            "print('no_site:',sys.flags.no_site,'isolated:',sys.flags.isolated);"
            "[print('path:',p) for p in sys.path];"
            "import importlib.machinery as m;"
            "s=m.PathFinder.find_spec('_ctypes');"
            "print('_ctypes from:',s.origin if s else None);"
            "[print('env:',k,'=',v) for k,v in sorted(os.environ.items()) "
            "if k.upper().startswith(('PYTHON','__PYVENV','VIRTUAL','PATH'))]")
    lines = ["@echo off", "chcp 65001 >nul",
             'cd /d "%s"' % _bat_text(odm_dir),
             'call "%s"' % _bat_text(env_bat),
             '"%SystemRoot%\\System32\\where.exe" python',
             'python -c "%s"' % code, ""]
    with open(path, "w", encoding="utf-8", newline="\r\n") as f:
        f.write("\n".join(lines))
    return path


class NativeInstaller(QObject):
    """Downloads and silently installs ODM for Windows. All asynchronous."""

    status = pyqtSignal(str)
    log = pyqtSignal(str)
    finished = pyqtSignal(bool, str)     # ok, message / folder

    MIN_BYTES = 50 * 1024 * 1024         # a real installer is ~234 MB
    UNINSTALLER = "unins000.exe"
    UNINSTALL_WAIT_SECONDS = 240

    def __init__(self, parent=None):
        super().__init__(parent)
        self._busy = False
        self._canceled = False
        self._downloader = None
        self._fetcher = None
        self._proc = None
        self._target = ""
        self._file = ""
        self._last_mb = -1

    @property
    def busy(self):
        return self._busy

    # ------------------------------------------------------------------ public
    def install(self, target_dir="", use_latest=False):
        if self._busy:
            return
        self._busy = True
        self._canceled = False
        self._target = target_dir or default_install_dir()
        if use_latest:
            self.status.emit("Looking for the newest OpenDroneMap…")
            fetcher = QgsNetworkContentFetcher()
            fetcher.setParent(self)
            fetcher.finished.connect(self._on_latest)
            self._fetcher = fetcher
            fetcher.fetchContent(QUrl(LATEST_API))
        else:
            self._download(PINNED_URL)

    def cancel(self):
        if not self._busy:
            return
        self._canceled = True
        if self._downloader is not None:
            try:
                self._downloader.cancelDownload()
            except RuntimeError:
                pass
        elif self._proc is None:
            self._done(False, "CANCELED")
        # (a running installer is left to finish: stopping it half-way would
        # leave a broken install)

    # --------------------------------------------------------------- internals
    def _on_latest(self):
        fetcher, self._fetcher = self._fetcher, None
        url = PINNED_URL
        try:
            import json
            data = json.loads(fetcher.contentAsString())
            for asset in data.get("assets", []):
                link = asset.get("browser_download_url", "")
                name = asset.get("name", "")
                if name.startswith("ODM_Setup_") and name.endswith(".exe") \
                        and link.startswith(TRUSTED_PREFIX):
                    url = link
                    break
            else:
                self.log.emit("The newest release has no Windows installer; "
                              "using version %s." % PINNED_VERSION)
        except Exception as e:  # noqa: BLE001
            self.log.emit("Could not look up the newest version (%s); using "
                          "version %s." % (e, PINNED_VERSION))
        if fetcher is not None:
            fetcher.deleteLater()
        if self._canceled:
            self._done(False, "CANCELED")
            return
        self._download(url)

    def _download(self, url):
        name = url.rsplit("/", 1)[-1] or "ODM_Setup.exe"
        self._file = os.path.join(tempfile.gettempdir(), name)
        try:
            if os.path.exists(self._file):
                os.remove(self._file)
        except OSError:
            pass
        self.log.emit("Downloading %s" % url)
        self.status.emit("Downloading OpenDroneMap…")
        self._last_mb = -1
        dl = QgsFileDownloader(QUrl(url), self._file, "", True)
        # Owned by Qt, not Python: the downloader deletes itself when done,
        # and must not be destroyed while it is still emitting its signals.
        dl.setParent(self)
        dl.downloadProgress.connect(self._on_progress)
        dl.downloadError.connect(self._on_download_error)
        dl.downloadCanceled.connect(self._on_download_canceled)
        dl.downloadCompleted.connect(self._on_downloaded)
        self._downloader = dl
        dl.startDownload()

    def _on_progress(self, received, total):
        mb = int(received // 1048576)
        if mb == self._last_mb:
            return
        self._last_mb = mb
        if total > 0:
            self.status.emit("Downloading OpenDroneMap… %d of %d MB"
                             % (mb, max(int(total // 1048576), 1)))
        else:
            self.status.emit("Downloading OpenDroneMap… %d MB" % mb)

    def _on_download_error(self, errors):
        self._downloader = None
        self._done(False, "The download failed: %s\nCheck the internet "
                   "connection and try again." % "; ".join(errors or []))

    def _on_download_canceled(self):
        self._downloader = None
        self._done(False, "CANCELED")

    def _on_downloaded(self, *_):
        self._downloader = None
        if not self._busy:
            return
        try:
            size = os.path.getsize(self._file)
        except OSError:
            size = 0
        if size < self.MIN_BYTES:
            self._done(False, "The download is incomplete (%d MB). Try again."
                       % (size // 1048576))
            return
        if IS_WINDOWS:
            self.status.emit("Checking the installer's digital signature…")
            self._run(["powershell", "-NoProfile", "-NonInteractive",
                       "-Command",
                       "(Get-AuthenticodeSignature -LiteralPath '%s').Status"
                       % self._file.replace("'", "''")],
                      self._on_signature)
        else:
            self._install()

    def _on_signature(self, exit_code, output):
        verdict = output.strip().splitlines()[-1].strip() if output.strip() \
            else ""
        if exit_code == 0 and verdict and verdict != "Valid":
            self._done(False, "The downloaded installer does not carry a "
                       "valid digital signature (%s), so it was not run. "
                       "Try again later." % verdict)
            return
        if verdict == "Valid":
            self.log.emit("Installer signature: valid.")
        else:
            self.log.emit("Installer signature could not be checked "
                          "(PowerShell answered: %s); continuing, as the "
                          "file was downloaded over HTTPS from the official "
                          "OpenDroneMap releases."
                          % (" ".join(output.split())[:160] or "nothing"))
        self._install()

    def installer_command(self):
        """The silent-install command line (Inno Setup switches)."""
        log_file = os.path.join(tempfile.gettempdir(), "odm_install.log")
        return [self._file, "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART",
                "/SP-", "/NOICONS", "/CURRENTUSER",
                "/DIR=%s" % self._target, "/LOG=%s" % log_file]

    def uninstaller_command(self, path):
        return [path, "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"]

    def _install(self):
        if self._canceled:
            self._done(False, "CANCELED")
            return
        # An earlier copy is removed first, and we wait until that has really
        # finished. (Left to itself the installer removes the old copy in the
        # background while it is already writing the new files, which can
        # leave the new install with files missing.)
        old = os.path.join(self._target, self.UNINSTALLER)
        if os.path.isfile(old):
            self.status.emit("Removing the previous copy of OpenDroneMap…")
            self._waited = 0
            self._run(self.uninstaller_command(old),
                      lambda _c, _o: self._wait_uninstalled())
            return
        self._run_installer()

    def _wait_uninstalled(self):
        from qgis.PyQt.QtCore import QTimer
        old = os.path.join(self._target, self.UNINSTALLER)
        if os.path.isfile(old) and self._waited < self.UNINSTALL_WAIT_SECONDS:
            self._waited += 1
            QTimer.singleShot(1000, self._wait_uninstalled)
            return
        if os.path.isfile(old):
            self.log.emit("The previous copy could not be removed completely; "
                          "installing over it.")
        QTimer.singleShot(1500, self._run_installer)

    def _run_installer(self):
        if not self._busy:
            return
        self.status.emit("Installing OpenDroneMap (a few minutes)…")
        self._run(self.installer_command(), self._on_installed)

    def _on_installed(self, exit_code, output):
        try:
            os.remove(self._file)
        except OSError:
            pass
        if runner_in(self._target):
            self._finish_setup()
            return
        tail = "\n".join(output.strip().splitlines()[-3:])
        self._done(False, "The installer finished (code %s) but OpenDroneMap "
                   "was not found in %s.\n%s" % (exit_code, self._target, tail))

    def postinstall_command(self, script):
        return [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", script]

    def _finish_setup(self):
        """Repeat ODM's own post-install step ourselves, so the install is
        complete whatever happened inside the silent installer."""
        script = postinstall_script(self._target)
        if script:
            self.status.emit("Finishing set-up…")
            self._run(self.postinstall_command(script),
                      lambda _c, _o: self._verify())
        else:
            self._verify()

    def _verify(self):
        try:
            for note in ensure_ready(self._target):
                self.log.emit(note)
        except OSError as e:
            self.log.emit("Could not check the install: %s" % e)
        self._done(True, self._target)

    def _run(self, command, callback):
        self.log.emit("$ " + " ".join(command[:2]) + (" …" if len(command) > 2
                                                      else ""))
        proc = QProcess(self)
        proc.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        self._proc = proc
        self._callback = callback

        def finished(code, _status=None):
            if self._proc is not proc:
                return
            out = proc.readAll().data().decode("utf-8", "replace")
            self._proc = None
            proc.deleteLater()
            callback(code, out)

        def error(err):
            if err != QProcess.ProcessError.FailedToStart \
                    or self._proc is not proc:
                return
            self._proc = None
            proc.deleteLater()
            callback(-1, "could not start %s" % command[0])

        proc.finished.connect(finished)
        proc.errorOccurred.connect(error)
        proc.start(command[0], command[1:])

    def _done(self, ok, message):
        if not self._busy:
            return
        self._busy = False
        self.finished.emit(ok, message)
