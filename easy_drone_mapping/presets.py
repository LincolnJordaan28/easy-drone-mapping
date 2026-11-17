# -*- coding: utf-8 -*-
"""Plain-language presets translated into OpenDroneMap options."""
import os

IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".tif", ".tiff", ".dng")

# key, label, description, options
QUALITY_PRESETS = [
    ("fast", "Fast preview",
     "Quickest result. Good for checking a flight worked before you leave "
     "site, or for a first look.",
     {"feature-quality": "medium", "pc-quality": "low",
      "orthophoto-resolution": 5, "dem-resolution": 10}),
    ("balanced", "Balanced (recommended)",
     "Good detail in reasonable time. The right choice for most jobs.",
     {"feature-quality": "high", "pc-quality": "medium",
      "orthophoto-resolution": 5, "dem-resolution": 5}),
    ("high", "High detail",
     "Sharpest maps and densest point cloud. Takes roughly four times longer "
     "and needs much more memory.",
     {"feature-quality": "high", "pc-quality": "high",
      "orthophoto-resolution": 2, "dem-resolution": 2}),
]


def preset_keys():
    return [p[0] for p in QUALITY_PRESETS]


def preset(key):
    for p in QUALITY_PRESETS:
        if p[0] == key:
            return p
    return QUALITY_PRESETS[1]


def build_options(quality="balanced", orthophoto=True, dsm=True, dtm=True,
                  point_cloud=True, model_3d=False, ortho_res=None,
                  dem_res=None, extra=None, report=False):
    """Return the dict of ODM options for the user's choices."""
    opts = dict(preset(quality)[3])

    if ortho_res:
        opts["orthophoto-resolution"] = ortho_res
    if dem_res:
        opts["dem-resolution"] = dem_res

    only_ortho = orthophoto and not (dsm or dtm or point_cloud or model_3d)
    if only_ortho:
        # Skips the dense point cloud entirely: much faster.
        opts["fast-orthophoto"] = True
        opts.pop("pc-quality", None)
        opts.pop("dem-resolution", None)
    else:
        if dsm:
            opts["dsm"] = True
        if dtm:
            opts["dtm"] = True   # ODM switches on ground classification itself
        if point_cloud:
            # Cloud-optimised point cloud: opens instantly in QGIS
            opts["pc-copc"] = True
        if not model_3d:
            opts["skip-3dmodel"] = True
        if not orthophoto:
            opts["skip-orthophoto"] = True
            opts.pop("orthophoto-resolution", None)
        if not (dsm or dtm):
            opts.pop("dem-resolution", None)

    # Cloud-optimised GeoTIFFs carry overviews, so big rasters pan smoothly.
    opts["cog"] = True

    if not report:
        # The PDF quality report is the slowest-to-fail, least-needed step.
        opts["skip-report"] = True

    opts.update(extra or {})
    return opts


def parse_extra_options(text):
    """Parse 'name=value; other-name=value' (also one per line).

    A bare name means 'switch this on'. Returns (dict, list_of_problems).
    """
    result, problems = {}, []
    for raw in (text or "").replace("\n", ";").split(";"):
        item = raw.strip()
        if not item:
            continue
        if "=" in item:
            name, value = item.split("=", 1)
        else:
            name, value = item, "true"
        name = name.strip().lstrip("-").strip()
        value = value.strip()
        if not name or " " in name:
            problems.append(item)
            continue
        result[name] = _convert(value)
    return result, problems


def _convert(value):
    low = value.lower()
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        return value


def filter_supported(options, node_options):
    """Keep only options this engine knows. Returns (kept, dropped_names)."""
    known = {o.get("name") for o in node_options if isinstance(o, dict)}
    if not known:
        return dict(options), []
    kept = {k: v for k, v in options.items() if k in known}
    dropped = [k for k in options if k not in known]
    return kept, dropped


def list_images(folder):
    """Sorted list of the image files in a folder that ODM can read."""
    found = []
    if not folder:
        return found
    try:
        for name in os.listdir(folder):
            path = os.path.join(folder, name)
            if name.lower().endswith(IMAGE_EXTENSIONS) \
                    and not name.startswith(".") and os.path.isfile(path):
                found.append(path)
    except OSError:
        return []
    return sorted(found)
