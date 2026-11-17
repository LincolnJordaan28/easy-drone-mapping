# -*- coding: utf-8 -*-
"""Loads finished ODM products into the current QGIS project, ready styled."""
import os

from qgis.core import (QgsColorRampShader, QgsCoordinateTransform,
                       QgsHillshadeRenderer, QgsProject, QgsRasterLayer,
                       QgsRasterShader, QgsSingleBandPseudoColorRenderer,
                       QgsStyle, QgsVectorLayer)
from qgis.PyQt.QtCore import QTimer
from qgis.PyQt.QtGui import QColor

from .compat import log

try:
    from qgis.core import QgsPointCloudLayer
except ImportError:  # very old QGIS
    QgsPointCloudLayer = None

# Elevation colours used when the 'Turbo'/'Spectral' ramps are unavailable
_FALLBACK_STOPS = [(0.0, "#30123b"), (0.25, "#28bceb"), (0.5, "#a4fc3c"),
                   (0.75, "#fb7e21"), (1.0, "#7a0403")]


def load_results(files, group_name, iface=None):
    """files: dict from job.find_results(). Returns list of layer names added."""
    project = QgsProject.instance()
    root = project.layerTreeRoot()
    was_empty = not project.mapLayers()
    group = root.insertGroup(0, group_name)
    added = []
    nodes = []

    # Only one layer is switched on to start with, so the map is not a pile
    # of rasters hiding each other. The rest are one tick away.
    primary = next((k for k in ("orthophoto", "dsm", "dtm", "point_cloud")
                    if files.get(k)), None)

    entries = []     # (kind, layer, expanded) in top-to-bottom display order

    def add(layer, kind, expanded=False):
        if layer is not None and layer.isValid():
            entries.append((kind, layer, expanded))

    if files.get("cameras"):
        add(QgsVectorLayer(files["cameras"], "Camera positions", "ogr"),
            "cameras")

    if files.get("point_cloud"):
        add(_point_cloud_layer(files["point_cloud"]), "point_cloud")

    hillshade_source = files.get("dsm") or files.get("dtm")
    if hillshade_source:
        hs = QgsRasterLayer(hillshade_source, "Hillshade (%s)" % (
            "surface" if files.get("dsm") else "terrain"))
        if hs.isValid():
            _try(_style_hillshade, hs)
            add(hs, "hillshade")

    for key, title in (("dtm", "Terrain model (DTM)"),
                       ("dsm", "Surface model (DSM)")):
        if files.get(key):
            dem = QgsRasterLayer(files[key], title)
            if dem.isValid():
                _try(_style_elevation, dem)
                add(dem, key, expanded=True)

    if files.get("orthophoto"):
        add(QgsRasterLayer(files["orthophoto"], "Orthophoto"), "orthophoto")

    for kind, layer, _exp in entries:
        project.addMapLayer(layer, False)
    for kind, layer, expanded in entries:
        node = group.addLayer(layer)   # appended: first added = top of group
        node.setItemVisibilityChecked(kind == primary)
        node.setExpanded(expanded)
        nodes.append(node)
        added.append(layer.name())

    if not added:
        root.removeChildNode(group)
        return added
    if not any(n.itemVisibilityChecked() for n in nodes):
        nodes[-1].setItemVisibilityChecked(True)
    group.setExpanded(True)

    main_layer = next((l for k, l, _e in entries if k == primary), None)

    def finish():
        # A new, empty project should work in the map's own projected
        # coordinate system (QGIS would otherwise copy the camera file's).
        if was_empty and main_layer is not None \
                and main_layer.crs().isValid():
            _try(project.setCrs, main_layer.crs())
        if iface is not None:
            _try(_zoom_to_group, iface, group)

    if was_empty and iface is not None:
        # Let QGIS finish its own first-layer set-up, then apply ours.
        QTimer.singleShot(400, finish)
    else:
        finish()
    return added


def _try(func, *args):
    """Styling is a nicety: never let it stop the layers from loading."""
    try:
        func(*args)
    except Exception as e:  # noqa: BLE001
        log("%s skipped: %s" % (func.__name__, e), "Warning")


def _point_cloud_layer(path):
    if QgsPointCloudLayer is None:
        return None
    providers = ["copc", "pdal"] if path.lower().endswith(".copc.laz") \
        else ["pdal", "copc"]
    for provider in providers:
        try:
            layer = QgsPointCloudLayer(path, "Point cloud", provider)
            if layer.isValid():
                return layer
        except Exception:  # noqa: BLE001
            continue
    log("The point cloud could not be opened by this QGIS build: %s" % path,
        "Warning")
    return None


def _elevation_range(layer):
    provider = layer.dataProvider()
    try:
        lo, hi = provider.cumulativeCut(1, 0.02, 0.98, layer.extent(), 250000)
        if lo == lo and hi == hi and hi > lo:  # not NaN
            return lo, hi
    except Exception:  # noqa: BLE001
        pass
    try:
        from qgis.core import Qgis
        flag = Qgis.RasterBandStatistic.All
    except Exception:  # noqa: BLE001
        from qgis.core import QgsRasterBandStats
        flag = QgsRasterBandStats.All
    stats = provider.bandStatistics(1, flag, layer.extent(), 250000)
    return stats.minimumValue, stats.maximumValue


def _style_elevation(layer):
    lo, hi = _elevation_range(layer)
    if not hi > lo:
        return
    ramp = None
    style = QgsStyle.defaultStyle()
    for name in ("Turbo", "Spectral"):
        ramp = style.colorRamp(name)
        if ramp is not None:
            if name == "Spectral":
                ramp.invert()
            break

    shader_fn = QgsColorRampShader(lo, hi)   # interpolated by default
    steps = 9
    items = []
    for i in range(steps):
        t = i / (steps - 1.0)
        value = lo + (hi - lo) * t
        color = ramp.color(t) if ramp is not None else _fallback_color(t)
        items.append(QgsColorRampShader.ColorRampItem(
            value, color, "%.1f m" % value))
    shader_fn.setColorRampItemList(items)
    shader = QgsRasterShader()
    shader.setRasterShaderFunction(shader_fn)
    renderer = QgsSingleBandPseudoColorRenderer(layer.dataProvider(), 1, shader)
    renderer.setClassificationMin(lo)
    renderer.setClassificationMax(hi)
    layer.setRenderer(renderer)
    layer.triggerRepaint()


def _fallback_color(t):
    for (t0, c0), (t1, c1) in zip(_FALLBACK_STOPS, _FALLBACK_STOPS[1:]):
        if t <= t1:
            f = 0 if t1 == t0 else (t - t0) / (t1 - t0)
            a, b = QColor(c0), QColor(c1)
            return QColor(int(a.red() + (b.red() - a.red()) * f),
                          int(a.green() + (b.green() - a.green()) * f),
                          int(a.blue() + (b.blue() - a.blue()) * f))
    return QColor(_FALLBACK_STOPS[-1][1])


def _style_hillshade(layer):
    renderer = QgsHillshadeRenderer(layer.dataProvider(), 1, 315, 45)
    renderer.setMultiDirectional(True)
    layer.setRenderer(renderer)
    layer.triggerRepaint()


def _zoom_to_group(iface, group):
    canvas = iface.mapCanvas()
    dest = canvas.mapSettings().destinationCrs()
    extent = None
    for node in group.findLayers():
        layer = node.layer()
        # Zoom to what is actually switched on (normally the orthophoto).
        if layer is None or not node.itemVisibilityChecked() \
                or layer.extent().isEmpty() or not layer.crs().isValid():
            continue
        tr = QgsCoordinateTransform(layer.crs(), dest, QgsProject.instance())
        rect = tr.transformBoundingBox(layer.extent())
        if extent is None:
            extent = rect
        else:
            extent.combineExtentWith(rect)
    if extent is not None and not extent.isEmpty():
        extent.scale(1.05)
        canvas.setExtent(extent)
        canvas.refresh()


def photo_positions_layer(image_paths, progress=None):
    """Memory point layer with one point per GPS-tagged photo.

    progress(i, n) may return False to stop early. Returns (layer, n_with_gps).
    """
    from qgis.core import QgsExifTools, QgsFeature, QgsGeometry, QgsPointXY
    layer = QgsVectorLayer(
        "Point?crs=EPSG:4326&field=photo:string(200)&field=altitude_m:double",
        "Photo positions", "memory")
    provider = layer.dataProvider()

    features = []
    n = len(image_paths)
    for i, path in enumerate(image_paths):
        if progress and progress(i, n) is False:
            break
        try:
            res = QgsExifTools.getGeoTag(path)
            point, ok = res if isinstance(res, tuple) else (res, True)
        except Exception:  # noqa: BLE001
            continue
        if not ok or point is None or point.isEmpty():
            continue
        if point.x() == 0 and point.y() == 0:
            continue
        feat = QgsFeature(layer.fields())
        feat.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(point.x(), point.y())))
        z = point.z() if point.is3D() else None
        feat.setAttributes([os.path.basename(path),
                            z if z is not None and z == z else None])
        features.append(feat)
    provider.addFeatures(features)
    layer.updateExtents()
    return layer, len(features)
