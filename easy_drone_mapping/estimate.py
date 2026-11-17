# -*- coding: utf-8 -*-
"""Time estimates for OpenDroneMap jobs, and how many cores each step can use.

The base figures are seconds per photo with one core, measured on a real
123-photo DJI Phantom 4 Pro survey (20 MP photos, 'Balanced' quality, ODM
3.6.2 on a 12-core Windows laptop with 16 GB memory). After every finished job
the plugin compares its estimate with what really happened and keeps a
correction factor, so estimates adapt to each computer.

Pure Python: no QGIS needed, so it can be tested on its own.
"""
import math

STAGES = ["dataset", "split", "merge", "opensfm", "openmvs",
          "odm_filterpoints", "odm_meshing", "mvs_texturing",
          "odm_georeferencing", "odm_dem", "odm_orthophoto", "odm_report",
          "odm_postprocess"]

# Which ODM run ("phase") each step belongs to when a job is split so that
# memory-light steps can use more cores (see phases()).
PHASE_END = {"A": "opensfm", "B": "odm_meshing", "C": "odm_postprocess"}


def phase_of(stage):
    i = STAGES.index(stage)
    if i <= STAGES.index("opensfm"):
        return "A"
    if i <= STAGES.index("odm_meshing"):
        return "B"
    return "C"


# seconds per photo with 1 core, and the share of the work that can use more
# cores (the rest runs on one core whatever the setting).
_BASE = {
    "dataset": (0.03, 0.0), "split": (0.0, 0.0), "merge": (0.0, 0.0),
    "opensfm": (18.4, 0.75), "openmvs": (33.5, 0.9),
    "odm_filterpoints": (0.21, 0.5), "odm_meshing_25d": (1.25, 0.5),
    "odm_meshing_3d": (1.30, 0.7), "mvs_texturing_25d": (3.28, 0.6),
    "mvs_texturing_3d": (6.36, 0.6), "odm_georeferencing": (0.14, 0.3),
    "classify": (0.5, 0.6), "copc": (0.2, 0.5), "dem": (0.46, 0.7),
    "odm_orthophoto": (0.99, 0.5), "odm_report": (0.5, 0.2),
    "odm_postprocess": (0.0, 0.0),
}

# Memory one extra core needs in the dense point cloud step, by point-cloud
# quality (GB). Photo matching and texturing need far more (see native).
DENSE_GB_PER_CORE = {"lowest": 0.3, "low": 0.4, "medium": 0.8, "high": 2.0,
                     "ultra": 4.0}


def _scale(seconds_one_core, parallel, cores):
    cores = max(1, cores)
    return seconds_one_core * ((1 - parallel) + parallel / cores ** 0.9)


def stage_seconds(options, n_photos, cores_a=1, cores_b=None, factor=1.0):
    """Estimated seconds for each ODM step, as {stage: seconds}.

    options: the ODM options for the job. cores_a: cores for photo matching
    and texturing; cores_b: cores for the dense point cloud and meshing.
    """
    n = max(1, n_photos)
    cores_b = cores_b or cores_a
    o = options or {}
    fast_ortho = bool(o.get("fast-orthophoto"))
    feature = {"ultra": 1.6, "high": 1.0, "medium": 0.45, "low": 0.25,
               "lowest": 0.15}.get(o.get("feature-quality", "high"), 1.0)
    pc = o.get("pc-quality", "medium")
    dense = {"ultra": 12.0, "high": 3.5, "medium": 1.0, "low": 0.3,
             "lowest": 0.1}.get(pc, 1.0)
    points = {"ultra": 3.0, "high": 1.6, "medium": 1.0, "low": 0.6,
              "lowest": 0.4}.get(pc, 1.0)
    ortho_res = float(o.get("orthophoto-resolution") or 5)
    dem_res = float(o.get("dem-resolution") or 5)
    ortho_f = min(3.0, (5.0 / max(ortho_res, 0.5)) ** 2)
    dem_f = min(3.0, (5.0 / max(dem_res, 0.5)) ** 2)
    model_3d = not (o.get("skip-3dmodel") or fast_ortho)
    n_dems = int(bool(o.get("dsm"))) + int(bool(o.get("dtm")))

    def t(key, cores, mult=1.0):
        s, p = _BASE[key]
        return _scale(s * n * mult, p, cores)

    est = {
        "dataset": t("dataset", 1), "split": 0.0, "merge": 0.0,
        "opensfm": t("opensfm", cores_a, feature),
        "openmvs": 0.0 if fast_ortho else t("openmvs", cores_b, dense),
        "odm_filterpoints": t("odm_filterpoints", cores_b,
                              0.3 if fast_ortho else points),
        "odm_meshing": t("odm_meshing_25d", cores_b,
                         0.5 if fast_ortho else points)
        + (t("odm_meshing_3d", cores_b, points) if model_3d else 0.0),
        "mvs_texturing": t("mvs_texturing_25d", cores_a)
        + (t("mvs_texturing_3d", cores_a) if model_3d else 0.0),
        "odm_georeferencing": t("odm_georeferencing", cores_a, points)
        + (t("classify", cores_a, points) if o.get("dtm") else 0.0)
        + (t("copc", cores_a, points) if o.get("pc-copc") else 0.0),
        "odm_dem": n_dems * t("dem", cores_a, points * dem_f),
        "odm_orthophoto": 0.0 if o.get("skip-orthophoto")
        else t("odm_orthophoto", cores_a, ortho_f),
        "odm_report": 0.0 if o.get("skip-report") else t("odm_report", 1),
        "odm_postprocess": 0.0,
    }
    f = max(0.2, min(5.0, factor or 1.0))
    return {k: v * f for k, v in est.items()}


def total(est):
    return sum(est.values())


def dense_cores(free_gb, pc_quality, cores):
    """Cores the dense point cloud step can use with this much free memory."""
    per = DENSE_GB_PER_CORE.get(pc_quality or "medium", 0.8)
    if free_gb is None:
        return 1
    return max(1, min(cores, int(max(free_gb - 1.5, 0.0) // per)))


def human(seconds):
    """'about 2 h 15 min' style text, rounded so it does not look exact."""
    seconds = max(0.0, float(seconds))
    if seconds < 90:
        return "about a minute"
    minutes = seconds / 60.0
    if minutes < 60:
        step = 1 if minutes < 15 else 5
        return "about %d min" % max(step, int(round(minutes / step) * step))
    h, m = divmod(int(round(minutes / 5.0) * 5), 60)
    return "about %d h %02d min" % (h, m) if m else "about %d h" % h


class Tracker:
    """Follows a running job's steps; gives progress and time left.

    Progress is the share of the *estimated* work done, so each step moves
    the bar in proportion to how long it really takes. Inside a step the bar
    moves with time at the expected pace, slowing down rather than stopping
    if the step runs long, and never passes the end of the step early.
    """

    def __init__(self, est, now):
        self.est = dict(est)
        self.total = max(1.0, total(est))
        self.done = set()
        self.actual = {}
        self.current = None
        self.since = now
        self.pace = 1.0          # actual / estimated, from finished steps
        self._real_work = [0.0, 0.0]

    def start(self, stage, now):
        if stage not in self.est or stage in self.done:
            return False          # a repeat (later phase): nothing new
        if self.current and self.current != stage:
            self.finish(self.current, now)
        self.current, self.since = stage, now
        return True

    def finish(self, stage, now):
        if stage != self.current or stage in self.done:
            return
        took = now - self.since
        self.done.add(stage)
        self.actual[stage] = took
        self.current = None
        expected = self.est.get(stage, 0.0)
        # Steps that took far less than expected were already done (Resume):
        # they say nothing about this computer's pace.
        if expected > 30 and took > 0.15 * expected:
            self._real_work[0] += took
            self._real_work[1] += expected
            self.pace = max(0.3, min(3.0, self._real_work[0]
                                     / self._real_work[1]))

    def _in_stage(self, now):
        if not self.current:
            return 0.0, 0.0
        expected = max(1.0, self.est.get(self.current, 0.0) * self.pace)
        f = (now - self.since) / expected
        if f > 0.9:   # running long: keep creeping, never reach the end
            f = 0.9 + 0.09 * (1 - math.exp(-(f - 0.9) / 0.5))
        return f, expected

    def progress(self, now):
        """0.0 – 1.0 of the whole job."""
        done = sum(self.est[s] for s in self.done)
        f, _ = self._in_stage(now)
        if self.current:
            done += self.est.get(self.current, 0.0) * f
        return max(0.0, min(0.995, done / self.total))

    def seconds_left(self, now):
        left = sum(v for k, v in self.est.items()
                   if k not in self.done and k != self.current) * self.pace
        if self.current:
            _, expected = self._in_stage(now)
            left += max(expected * 0.1, expected - (now - self.since))
        return left

    def calibration(self):
        """(actual seconds, estimated seconds) of the real work done."""
        return tuple(self._real_work)
