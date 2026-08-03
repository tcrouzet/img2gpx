"""Génère une carte illustrée et reproductible à partir du GPX et du road book.

Usage :
    venv/bin/python map.py

Sorties :
    _output/<trace>_map.png
    _output/<trace>_map_prompt.json
"""

import argparse
import io
import json
import math
import os
import random
import re
import shutil
import subprocess
from pathlib import Path

import gpxpy
import matplotlib
matplotlib.use("Agg")
import matplotlib.patheffects as path_effects
import matplotlib.pyplot as plt
import numpy as np
import requests
from matplotlib.offsetbox import AnnotationBbox, OffsetImage
from matplotlib.font_manager import FontProperties
from PIL import Image, ImageChops, ImageDraw, ImageFilter
from shapely.geometry import LineString, Point, box
from shapely.ops import linemerge, nearest_points, polygonize, split, substring, unary_union

import cache_manager as cache
import osm_tools as osm
from parameters import assets_dir, gpx_file, gpx_path, output_folder


TOWN_RE = re.compile(
    r"^km(?P<km>\d+)\s+-\s+(?P<body>.+?)\s+(?P<distance>[\d.]+km)$"
)
LINK_RE = re.compile(r"^\[(?P<name>.+?)\]\([^)]*\)(?:\s+\[\(w\)\]\([^)]*\))?$")
META_RE = re.compile(r"^\*(?P<meta>.+)\*$")
POPULATION_RE = re.compile(r"(?P<population>[\d ]+) habitants")


def arguments():
    parser = argparse.ArgumentParser(description="Carte GPX illustrée façon dessin manuel")
    parser.add_argument("--gpx", default=gpx_path)
    parser.add_argument("--road-book", default=None)
    parser.add_argument("--html", default=None, help="HTML associé, source des statistiques")
    parser.add_argument("--output", default=None)
    parser.add_argument("--cities", type=int, default=11, help="Nombre maximal de grandes villes en plus du départ/arrivée")
    parser.add_argument("--seed", type=int, default=727)
    parser.add_argument("--no-water", action="store_true", help="N'interroge pas OSM pour l'eau")
    return parser.parse_args()


def read_gpx(path):
    with open(path, encoding="utf-8") as source:
        gpx = gpxpy.parse(source)
    points = []
    title = None
    for track in gpx.tracks:
        title = title or track.name
        for segment in track.segments:
            points.extend((point.latitude, point.longitude) for point in segment.points)
    if len(points) < 2:
        raise ValueError("La trace GPX ne contient pas assez de points")
    return title or Path(path).stem.lstrip("_"), points


def cumulative_km(points):
    distances = [0.0]
    for first, second in zip(points, points[1:]):
        lat1, lon1 = map(math.radians, first)
        lat2, lon2 = map(math.radians, second)
        dlat, dlon = lat2 - lat1, lon2 - lon1
        value = (
            math.sin(dlat / 2) ** 2
            + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
        )
        distances.append(distances[-1] + 12742 * math.asin(math.sqrt(value)))
    return np.asarray(distances)


def plain_town_name(body):
    match = LINK_RE.match(body.strip())
    if match:
        return match.group("name")
    return re.sub(r"\s+\[\(w\)\]\([^)]*\)$", "", body).strip()


def read_road_book(path):
    entries = []
    current = None
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        town = TOWN_RE.match(line.strip())
        if town:
            current = {
                "name": plain_town_name(town.group("body")),
                "km": int(town.group("km")),
                "distance_in_town": float(town.group("distance").removesuffix("km")),
                "population": 0,
                "status": "",
            }
            entries.append(current)
            continue
        metadata = META_RE.match(line.strip())
        if metadata and current:
            text = metadata.group("meta")
            population = POPULATION_RE.search(text)
            if population:
                current["population"] = int(population.group("population").replace(" ", ""))
            parts = [part.strip() for part in text.split("·")]
            current["status"] = " · ".join(
                part for part in parts if "habitants" not in part
            )
    return entries


def read_html_summary(path):
    if not path or not Path(path).exists():
        return None
    source = Path(path).read_text(encoding="utf-8")
    pattern = re.compile(
        r"</h3>\s*(?P<distance>\d+)km<br/>\s*\+(?P<elevation>\d+)m<br/>\s*"
        r"(?P<asphalt>\d+)% asphalt<br/>\s*(?P<tracks>\d+)% tracks<br/>\s*"
        r"(?P<single>\d+)% single tracks",
        re.IGNORECASE,
    )
    match = pattern.search(source)
    if not match:
        return None
    values = match.groupdict()
    return (
        f"{int(values['distance']):,} km / {int(values['elevation']):,} m D+\n"
        f"{values['asphalt']} % asphalte\n"
        f"{values['tracks']} % pistes\n"
        f"{values['single']} % sentiers"
    ).replace(",", " ")


def select_towns(entries, major_count):
    if not entries:
        return []

    selected = [{**entries[0], "role": "start"}]
    endpoint_names = {normalize_label(entries[0]["name"])}
    if normalize_label(entries[-1]["name"]) != normalize_label(entries[0]["name"]):
        selected.append({**entries[-1], "role": "finish"})
        endpoint_names.add(normalize_label(entries[-1]["name"]))

    seen = set(endpoint_names)
    # Conserve tous les candidats : la sélection définitive doit aussi pouvoir
    # couvrir une portion moins peuplée de la trace.
    for town in entries:
        key = normalize_label(town["name"])
        if key in seen:
            continue
        selected.append({**town, "role": "major"})
        seen.add(key)
    return sorted(selected, key=lambda item: item["km"])


def normalize_label(value):
    return re.sub(r"[^a-z0-9]", "", value.lower())


def coordinates_at_km(entries, points, distances):
    result = []
    for town in entries:
        index = int(np.abs(distances - town["km"]).argmin())
        result.append({**town, "lat": points[index][0], "lon": points[index][1]})
    return result


def remove_nearby_towns(towns, mean_lat, major_count, extent, pixel_size,
                        minimum_pixels=360):
    """Priorise la population puis comble les grands intervalles du parcours."""
    if not towns:
        return []
    factor = math.cos(math.radians(mean_lat))
    xy = np.asarray([(town["lon"] * factor, town["lat"]) for town in towns])
    width, height = extent[1] - extent[0], extent[3] - extent[2]
    pixel_xy = np.column_stack((
        (xy[:, 0] - extent[0]) / width * pixel_size[0],
        (xy[:, 1] - extent[2]) / height * pixel_size[1],
    ))
    indexed = list(zip(towns, pixel_xy))
    indexed = [
        item for item in indexed
        if item[0].get("role") in {"start", "finish"}
        or item[0].get("distance_in_town", 0) >= 2.0
    ]
    kept = []

    def add_best(candidates):
        for town, point in sorted(candidates, key=lambda item: item[0].get("population", 0), reverse=True):
            if all(np.linalg.norm(point - old[1]) >= minimum_pixels for old in kept):
                kept.append((town, point))
                return True
        return False

    for item in indexed:
        if item[0].get("role") in {"start", "finish"}:
            add_best([item])

    # D'abord les villes réellement importantes : une petite commune ne peut
    # plus évincer Avignon simplement parce qu'elle tombe dans un secteur.
    for item in sorted(indexed, key=lambda item: item[0].get("population", 0), reverse=True):
        if len(kept) >= max(4, major_count // 2 + 1):
            break
        add_best([item])

    # Puis on coupe toujours le plus long intervalle kilométrique encore vide,
    # en y prenant la commune la plus peuplée compatible avec l'écart en px.
    end_km = max(town["km"] for town in towns)
    while len(kept) < major_count + 1:
        marks = sorted([0, end_km] + [item[0]["km"] for item in kept])
        gaps = sorted(zip(marks, marks[1:]), key=lambda pair: pair[1] - pair[0], reverse=True)
        added = False
        for low, high in gaps:
            candidates = [item for item in indexed if low < item[0]["km"] < high]
            if add_best(candidates):
                added = True
                break
        if not added:
            break

    # Dernière vérification locale : dans un même voisinage visuel, conserve
    # la ville la plus peuplée. Cela remplace par exemple Tuchan par Leucate.
    for index, (town, point) in enumerate(list(kept)):
        if town.get("role") in {"start", "finish"}:
            continue
        nearby = [
            item for item in indexed
            if np.linalg.norm(item[1] - point) < minimum_pixels
            and item[0].get("population", 0) > town.get("population", 0)
        ]
        for replacement in sorted(
            nearby, key=lambda item: item[0].get("population", 0), reverse=True
        ):
            if all(
                other_index == index
                or np.linalg.norm(replacement[1] - other[1]) >= minimum_pixels
                for other_index, other in enumerate(kept)
            ):
                kept[index] = replacement
                break
    return sorted((item[0] for item in kept), key=lambda town: town["km"])


def water_geometries(points):
    lats = [point[0] for point in points]
    lons = [point[1] for point in points]
    margin = max(0.12, max(max(lats) - min(lats), max(lons) - min(lons)) * .18)
    bbox = (min(lats) - margin, min(lons) - margin, max(lats) + margin, max(lons) + margin)
    query = f"""[out:json][timeout:600];
(
way["natural"="coastline"]{bbox};
way["waterway"~"^(river|canal)$"]{bbox};
way["natural"="water"]{bbox};
relation["natural"="water"]{bbox};
way["landuse"="reservoir"]{bbox};
relation["landuse"="reservoir"]{bbox};
way["natural"="wood"]{bbox};
relation["natural"="wood"]{bbox};
way["landuse"="forest"]{bbox};
relation["landuse"="forest"]{bbox};
);
out body geom;
"""
    try:
        elements = osm.overpass(query)
    except (Exception, SystemExit) as error:
        legacy_bbox = (min(lats) - .12, min(lons) - .12, max(lats) + .12, max(lons) + .12)
        legacy_query = query.replace(str(bbox), str(legacy_bbox))
        try:
            elements = osm.overpass(legacy_query)
            print(f"Extension OSM indisponible, ancien fond en cache conservé : {error}")
        except (Exception, SystemExit):
            print(f"Eau OSM indisponible, carte produite sans fond hydrographique : {error}")
            return []

    result = []
    for element in elements:
        tags = element.get("tags", {})
        kind = tags.get("natural") or tags.get("waterway") or tags.get("landuse")
        geometries = [element.get("geometry") or []]
        if element.get("type") == "relation":
            member_geometries = [
                member.get("geometry") or [] for member in element.get("members", [])
                if member.get("type") == "way" and member.get("role") in {"outer", ""}
            ]
            lines = [
                LineString((point["lon"], point["lat"]) for point in geometry)
                for geometry in member_geometries if len(geometry) >= 2
            ]
            polygons = list(polygonize(unary_union(lines))) if lines else []
            geometries = [
                [{"lat": lat, "lon": lon} for lon, lat in polygon.exterior.coords]
                for polygon in polygons
            ]
        for geometry in geometries:
            if len(geometry) < 2:
                continue
            result.append(
                {
                    "kind": "water" if kind == "reservoir" else kind,
                    "name": tags.get("name", ""),
                    "points": [(point["lat"], point["lon"]) for point in geometry],
                }
            )
    return result


def sea_polygons(water, route_xy, extent, mean_lat):
    """Découpe le cadre avec les côtes et retourne les surfaces côté mer."""
    coastlines = []
    for item in water:
        if item["kind"] != "coastline":
            continue
        xy = project(item["points"], mean_lat)
        if len(xy) >= 2:
            coastlines.append(LineString(xy))
    if not coastlines:
        return []

    frame = box(extent[0], extent[2], extent[1], extent[3])
    boundary = frame.boundary
    merged = linemerge(unary_union(coastlines))
    lines = list(merged.geoms) if hasattr(merged, "geoms") else [merged]
    clipped_lines = []
    for line in lines:
        clipped = line.intersection(frame)
        parts = list(clipped.geoms) if hasattr(clipped, "geoms") else [clipped]
        clipped_lines.extend(part for part in parts if isinstance(part, LineString))
    if not clipped_lines:
        return []

    # Le grand trait de côte suffit à distinguer terre et mer à l'échelle de
    # cette carte. Sa simplification évite de polygoniser des milliers de nœuds.
    coastline = max(clipped_lines, key=lambda line: line.length)
    coastline = coastline.simplify(max(extent[1] - extent[0], extent[3] - extent[2]) * .0007)
    coords = list(coastline.coords)
    start = nearest_points(Point(coords[0]), boundary)[1]
    finish = nearest_points(Point(coords[-1]), boundary)[1]
    cutter = LineString([start.coords[0], *coords, finish.coords[0]])
    try:
        regions = list(split(frame, cutter).geoms)
    except ValueError:
        return []

    route_samples = [Point(point) for point in route_xy[::max(1, len(route_xy) // 100)]]
    scored = [(sum(region.buffer(1e-9).contains(point) for point in route_samples), region)
              for region in regions]
    if len(scored) < 2:
        return []
    land = max(scored, key=lambda item: item[0])[1]
    return [region for _, region in scored if region != land]


def font_properties(size, bold=False):
    candidates = (
        "/System/Library/Fonts/MarkerFelt.ttc",
        "/System/Library/Fonts/Supplemental/Bradley Hand Bold.ttf",
        os.path.join(assets_dir, "Geneva.ttf"),
    )
    path = next((candidate for candidate in candidates if os.path.exists(candidate)), None)
    return FontProperties(fname=path, size=size, weight="bold" if bold else "normal")


def script_font(size):
    candidates = (
        "/System/Library/Fonts/Supplemental/Bradley Hand Bold.ttf",
        "/System/Library/Fonts/MarkerFelt.ttc",
        os.path.join(assets_dir, "Geneva.ttf"),
    )
    path = next((candidate for candidate in candidates if os.path.exists(candidate)), None)
    return FontProperties(fname=path, size=size, weight="bold")


def project(points, mean_lat):
    factor = math.cos(math.radians(mean_lat))
    return np.asarray([(lon * factor, lat) for lat, lon in points])


def map_layout(points):
    """Retourne la projection, l'emprise et les pixels exacts du PNG maître."""
    mean_lat = sum(point[0] for point in points) / len(points)
    route_xy = project(points, mean_lat)
    span_x, span_y = np.ptp(route_xy[:, 0]), np.ptp(route_xy[:, 1])
    padding_x, padding_y = span_x * .14, span_y * .14
    extent = [route_xy[:, 0].min() - padding_x, route_xy[:, 0].max() + padding_x,
              route_xy[:, 1].min() - padding_y, route_xy[:, 1].max() + padding_y]
    width, height = extent[1] - extent[0], extent[3] - extent[2]
    if width / height > 1.65:
        extra = (width / 1.65 - height) / 2
        extent[2] -= extra
        extent[3] += extra
    ratio = (extent[1] - extent[0]) / (extent[3] - extent[2])
    return mean_lat, route_xy, tuple(extent), 4320, round(4320 / ratio)


def smooth_route(route_xy):
    """Allège le GPX sans déformer ses grandes boucles."""
    tolerance = max(np.ptp(route_xy[:, 0]), np.ptp(route_xy[:, 1])) * .0018
    simplified = LineString(route_xy).simplify(tolerance, preserve_topology=False)
    smoothed = np.asarray(simplified.coords)
    # Deux passes de Chaikin arrondissent les cassures créées par la
    # simplification tout en conservant les extrémités du parcours.
    for _ in range(2):
        first = smoothed[:-1] * .75 + smoothed[1:] * .25
        second = smoothed[:-1] * .25 + smoothed[1:] * .75
        interleaved = np.empty((len(first) * 2, 2))
        interleaved[0::2] = first
        interleaved[1::2] = second
        smoothed = np.vstack((smoothed[0], interleaved, smoothed[-1]))
    return smoothed


def terrain_grid(points, zoom=8, legacy_margin=False):
    """Charge un relief Terrarium précis et persistant pour l'emprise GPX."""
    lats = [point[0] for point in points]
    lons = [point[1] for point in points]
    margin = .12 if legacy_margin else max(
        .12, max(max(lats) - min(lats), max(lons) - min(lons)) * .18
    )
    north, south = max(lats) + margin, min(lats) - margin
    west, east = min(lons) - margin, max(lons) + margin
    tiles = 2 ** zoom

    def tile_xy(lat, lon):
        lat_radians = math.radians(lat)
        x = (lon + 180) / 360 * tiles
        y = (1 - math.asinh(math.tan(lat_radians)) / math.pi) / 2 * tiles
        return x, y

    x0, y0 = tile_xy(north, west)
    x1, y1 = tile_xy(south, east)
    xmin, xmax = math.floor(x0), math.floor(x1)
    ymin, ymax = math.floor(y0), math.floor(y1)
    terrain_dir = Path(cache.get_foler()) / "terrain"
    terrain_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    try:
        for tile_y in range(ymin, ymax + 1):
            row = []
            for tile_x in range(xmin, xmax + 1):
                tile_path = terrain_dir / f"terrarium-{zoom}-{tile_x}-{tile_y}.png"
                if not tile_path.exists():
                    url = f"https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{zoom}/{tile_x}/{tile_y}.png"
                    response = requests.get(url, timeout=30)
                    response.raise_for_status()
                    tile_path.write_bytes(response.content)
                row.append(np.asarray(Image.open(tile_path).convert("RGB"), dtype=float))
            rows.append(np.concatenate(row, axis=1))
    except Exception as error:
        if not legacy_margin and margin > .12:
            print(f"Extension du relief indisponible, ancien cache conservé : {error}")
            return terrain_grid(points, zoom=zoom, legacy_margin=True)
        print(f"Relief indisponible, carte produite sans zones montagneuses : {error}")
        return None

    rgb = np.concatenate(rows, axis=0)
    elevation = rgb[..., 0] * 256 + rgb[..., 1] + rgb[..., 2] / 256 - 32768
    pixel_x = np.arange(elevation.shape[1]) + xmin * 256 + .5
    pixel_y = np.arange(elevation.shape[0]) + ymin * 256 + .5
    longitudes = pixel_x / (256 * tiles) * 360 - 180
    mercator = math.pi * (1 - 2 * pixel_y / (256 * tiles))
    latitudes = np.degrees(np.arctan(np.sinh(mercator)))
    return longitudes, latitudes, elevation


def rough_line(ax, xy, rng, color, linewidth, zorder, jitter=0.0012, passes=3, alpha=1):
    if len(xy) < 2:
        return
    for draw in range(passes):
        noise = rng.normal(0, jitter, size=xy.shape)
        noise[0] = noise[-1] = 0
        ax.plot(
            xy[:, 0] + noise[:, 0],
            xy[:, 1] + noise[:, 1],
            color=color,
            linewidth=linewidth * (1 + rng.uniform(-0.08, 0.08)),
            alpha=alpha / passes if passes > 1 else alpha,
            solid_capstyle="round",
            solid_joinstyle="round",
            zorder=zorder,
        )


def watercolor(ax, route_xy, rng):
    indices = rng.integers(0, len(route_xy), size=240)
    span_x = np.ptp(route_xy[:, 0])
    span_y = np.ptp(route_xy[:, 1])
    for index in indices:
        center = route_xy[index]
        x = center[0] + rng.normal(0, span_x * 0.045)
        y = center[1] + rng.normal(0, span_y * 0.045)
        size = rng.uniform(180, 1600)
        color = rng.choice(["#b9d99b", "#cfe6b0", "#9fc98b", "#d9eabc"])
        ax.scatter(x, y, s=size, color=color, alpha=rng.uniform(0.025, 0.09), linewidth=0, zorder=0)


def draw_mountains(ax, x, y, scale, rng):
    angles = np.linspace(0, 2 * math.pi, 73)
    raw = np.asarray([rng.uniform(3.4, 5.0) for _ in angles])
    padded = np.r_[raw[-5:], raw, raw[:5]]
    radii = np.convolve(padded, np.ones(11) / 11, mode="valid")[:len(angles)] * scale
    zone_x = x + np.cos(angles) * radii
    zone_y = y + np.sin(angles) * radii * .55
    ax.fill(zone_x, zone_y, color="#cbb878", alpha=.09, linewidth=0, zorder=1.5)
    ax.fill(zone_x + scale * .35, zone_y, color="#a9c78d", alpha=.075,
            linewidth=0, zorder=1.6)
    ax.scatter([x], [y], s=1900, color="#b9d99b", alpha=.16, linewidth=0, zorder=2)
    peaks = ((-1.1, .75), (0, 1.2), (1.0, .82))
    for offset, height in peaks:
        px = x + offset * scale
        ax.plot(
            [px - scale, px, px + scale],
            [y, y + scale * height, y],
            color="#233923",
            lw=rng.uniform(1.8, 2.5),
            solid_capstyle="round",
            zorder=3,
        )
        ax.plot(
            [px - scale * .25, px, px + scale * .22],
            [y + scale * height * .72, y + scale * height, y + scale * height * .74],
            color="#fffdf6",
            lw=1.2,
            zorder=4,
        )


def torn_paper_edges(ax, extent, rng):
    """Masque les limites géométriques par quatre déchirures irrégulières."""
    left, right, bottom, top = extent
    width, height = right - left, top - bottom
    samples = 180
    xs = np.linspace(left, right, samples)
    ys = np.linspace(bottom, top, samples)
    def soft_noise(size):
        noise = rng.uniform(0, 1, size + 12)
        return np.convolve(noise, np.ones(13) / 13, mode="valid")[:size]

    top_edge = top - height * (.010 + soft_noise(samples) * .020)
    bottom_edge = bottom + height * (.010 + soft_noise(samples) * .020)
    left_edge = left + width * (.009 + soft_noise(samples) * .017)
    right_edge = right - width * (.009 + soft_noise(samples) * .017)
    paper = "#fffdf6"
    ax.fill_between(xs, top_edge, top + height * .03, color=paper, zorder=20)
    ax.fill_between(xs, bottom - height * .03, bottom_edge, color=paper, zorder=20)
    ax.fill_betweenx(ys, left - width * .03, left_edge, color=paper, zorder=20)
    ax.fill_betweenx(ys, right_edge, right + width * .03, color=paper, zorder=20)
    for alpha, linewidth in ((.30, 8), (.14, 18), (.06, 34)):
        ax.plot(xs, top_edge, color=paper, alpha=alpha, linewidth=linewidth, zorder=19)
        ax.plot(xs, bottom_edge, color=paper, alpha=alpha, linewidth=linewidth, zorder=19)
        ax.plot(left_edge, ys, color=paper, alpha=alpha, linewidth=linewidth, zorder=19)
        ax.plot(right_edge, ys, color=paper, alpha=alpha, linewidth=linewidth, zorder=19)


def draw_star(ax, x, y, radius, rng):
    angles = np.linspace(math.pi / 2, math.pi / 2 + 2 * math.pi, 17)[:-1]
    radii = np.asarray([radius if index % 2 == 0 else radius * .42 for index in range(16)])
    radii *= rng.uniform(.94, 1.06, size=16)
    xy = np.column_stack((x + np.cos(angles) * radii, y + np.sin(angles) * radii))
    xy = np.vstack((xy, xy[0]))
    ax.fill(xy[:, 0], xy[:, 1], color="#fffdf6", edgecolor="#111", linewidth=2.8, zorder=10)
    ax.scatter([x], [y], s=18, color="#111", zorder=11)


def draw_city_icon(ax, x, y, icon_path, size_points=36):
    """Pose une variante de point manuscrit exactement sur la trace."""
    icon_path = str(icon_path)
    icon = plt.imread(icon_path)
    if icon.ndim == 2:
        icon = np.repeat(icon[..., None], 3, axis=2)
    if np.issubdtype(icon.dtype, np.integer):
        icon = icon.astype(float) / np.iinfo(icon.dtype).max
    rgb = icon[..., :3]
    alpha = icon[..., 3] if icon.shape[2] == 4 else np.clip((1 - np.min(rgb, axis=2)) * 4, 0, 1)
    rgba = np.dstack((rgb, alpha))
    zoom = size_points / max(rgba.shape[:2])
    ax.add_artist(AnnotationBbox(
        OffsetImage(rgba, zoom=zoom, interpolation="bilinear"),
        (x, y), frameon=False, box_alignment=(.5, .5), zorder=11,
    ))


def place_labels(ax, towns, mean_lat, extent, rng):
    factor = math.cos(math.radians(mean_lat))
    width, height = extent[1] - extent[0], extent[3] - extent[2]
    figure_width, figure_height = ax.figure.get_size_inches()
    unit_x = width / (figure_width * 72)
    unit_y = height / (figure_height * 72)
    renderer = ax.figure.canvas.get_renderer()
    pixel_x = width / (figure_width * ax.figure.dpi)
    pixel_y = height / (figure_height * ax.figure.dpi)
    display_positions = {}
    endpoint_indices = [
        index for index, town in enumerate(towns)
        if town.get("role") in {"start", "finish"}
    ]
    if len(endpoint_indices) == 2:
        first, second = endpoint_indices
        first_xy = np.asarray((towns[first]["lon"] * factor, towns[first]["lat"]))
        second_xy = np.asarray((towns[second]["lon"] * factor, towns[second]["lat"]))
        if np.linalg.norm(first_xy - second_xy) < max(width, height) * .055:
            display_positions[first] = first_xy + np.asarray((44 * unit_x, 24 * unit_y))
            display_positions[second] = second_xy + np.asarray((-44 * unit_x, -24 * unit_y))

    compass_width = 170 * unit_x
    compass_height = 170 * unit_y
    compass_left = extent[1] - width * .035 - compass_width
    compass_bottom = extent[2] + height * .035
    occupied = [(
        compass_left - width * .015,
        compass_left + compass_width + width * .015,
        compass_bottom - height * .012,
        compass_bottom + compass_height + height * .012,
    ), (
        extent[0] + width * .018,
        extent[0] + width * .48,
        extent[3] - height * .31,
        extent[3] - height * .018,
    )]
    icon_paths = sorted(Path(assets_dir).glob("handot_*.png"))
    if not icon_paths:
        raise FileNotFoundError("Aucun assets/handot_*.png disponible")
    rng.shuffle(icon_paths)
    icon_cursor = 0
    for index, town in enumerate(towns):
        true_x, true_y = town["lon"] * factor, town["lat"]
        x, y = display_positions.get(index, (true_x, true_y))
        is_endpoint = town.get("role") in {"start", "finish"}
        # Une taille visuelle doit suivre le petit côté du canevas. Avec le
        # grand côté, les icônes explosaient sur les parcours compacts/larges.
        radius_x = 22 * unit_x
        radius_y = 22 * unit_y
        if is_endpoint:
            if index in display_positions:
                ax.plot([true_x, x], [true_y, y], color="#111", lw=1.6,
                        linestyle=(0, (2, 2)), zorder=8)
                ax.scatter([true_x], [true_y], s=28, color="#111", zorder=9)

        # Tourne réellement autour du point. Selon l'angle, le point touche le
        # début, le milieu ou la fin du nom ; les autres ancrages restent des
        # solutions de repli pour les zones cartographiques encombrées.
        candidates = []
        for distance in (1.15, 1.55, 2.05, 2.45):
            for angle in np.linspace(0, 2 * math.pi, 24, endpoint=False):
                dx = math.cos(angle) * radius_x * distance
                dy = math.sin(angle) * radius_y * distance
                natural = "left" if math.cos(angle) > .30 else "right" if math.cos(angle) < -.30 else "center"
                candidates.append((dx, dy, natural))
                candidates.extend((dx, dy, anchor) for anchor in ("left", "center", "right") if anchor != natural)
        rng.shuffle(candidates)
        midpoint = (extent[0] + extent[1]) / 2
        route_center = ROUTE_FOR_LABELS.mean(axis=0)
        outward = np.asarray((x, y)) - route_center
        map_diagonal = math.hypot(width, height)
        near_sea = SEA_FOR_LABELS and min(
            polygon.distance(Point(x, y)) for polygon in SEA_FOR_LABELS
        ) < map_diagonal * .045

        def candidate_priority(candidate):
            label_point = Point(x + candidate[0], y + candidate[1])
            toward_sea = any(polygon.contains(label_point) for polygon in SEA_FOR_LABELS)
            if near_sea and toward_sea:
                return -1000 - np.dot(np.asarray(candidate[:2]), outward)
            return -np.dot(np.asarray(candidate[:2]), outward)

        if town.get("role") != "start":
            candidates.sort(key=candidate_priority)
        label = town["name"]
        chosen = None
        is_start = town.get("role") == "start"
        font_size = 70 if is_start else (42 if is_endpoint else 42)
        label_font = script_font(font_size) if is_endpoint else font_properties(font_size, True)
        measured_width, measured_height, _ = renderer.get_text_width_height_descent(
            label, label_font, ismath=False
        )
        text_width = (measured_width + 18) * pixel_x
        text_height = (measured_height + 18) * pixel_y

        def candidate_box(candidate):
            tx, ty = x + candidate[0], y + candidate[1]
            if candidate[2] == "left":
                return (tx, tx + text_width, ty - text_height / 2, ty + text_height / 2)
            if candidate[2] == "right":
                return (tx - text_width, tx, ty - text_height / 2, ty + text_height / 2)
            return (tx - text_width / 2, tx + text_width / 2,
                    ty - text_height / 2, ty + text_height / 2)

        def route_clearance(candidate):
            box = candidate_box(candidate)
            dx = np.maximum.reduce((
                box[0] - ROUTE_FOR_LABELS[:, 0],
                np.zeros(len(ROUTE_FOR_LABELS)),
                ROUTE_FOR_LABELS[:, 0] - box[1],
            )) / pixel_x
            dy = np.maximum.reduce((
                box[2] - ROUTE_FOR_LABELS[:, 1],
                np.zeros(len(ROUTE_FOR_LABELS)),
                ROUTE_FOR_LABELS[:, 1] - box[3],
            )) / pixel_y
            return float(np.min(np.hypot(dx, dy)))

        # Parmi toutes les positions circulaires, essaie d'abord celle dont le
        # rectangle de texte est le plus éloigné de l'ensemble de la trace.
        candidates.sort(key=route_clearance, reverse=True)
        for candidate in candidates:
            tx, ty = x + candidate[0], y + candidate[1]
            box = candidate_box(candidate)
            overlaps_name = any(
                not (box[1] < old[0] or box[0] > old[1] or box[3] < old[2] or box[2] > old[3])
                for old in occupied
            )
            route_margin_x, route_margin_y = 18 * pixel_x, 18 * pixel_y
            route_inside = np.any(
                (ROUTE_FOR_LABELS[:, 0] >= box[0] - route_margin_x)
                & (ROUTE_FOR_LABELS[:, 0] <= box[1] + route_margin_x)
                & (ROUTE_FOR_LABELS[:, 1] >= box[2] - route_margin_y)
                & (ROUTE_FOR_LABELS[:, 1] <= box[3] + route_margin_y)
            )
            edge_x, edge_y = width * .055, height * .035
            inside_map = (
                box[0] > extent[0] + edge_x and box[1] < extent[1] - edge_x
                and box[2] > extent[2] + edge_y and box[3] < extent[3] - edge_y
            )
            if not overlaps_name and not route_inside and inside_map:
                chosen = candidate
                occupied.append(box)
                break
        if chosen is None:
            continue
        icon_size = 54 if town.get("role") == "start" else 36
        draw_city_icon(ax, x, y, icon_paths[icon_cursor % len(icon_paths)], icon_size)
        icon_cursor += 1
        dx, dy, alignment = chosen
        if abs(dx) > radius_x * 2.5:
            end_x = x + dx - math.copysign(radius_x * .35, dx)
            ax.plot([x, end_x], [y, y + dy], color="#333", linewidth=1.2,
                    linestyle=(0, (2, 3)), alpha=.65, zorder=8)
        text = ax.text(
            x + dx,
            y + dy,
            label,
            ha=alignment,
            va="center",
            color="#101010",
            fontproperties=label_font,
            zorder=9,
        )
        text.set_path_effects([path_effects.withStroke(linewidth=4, foreground="#fffdf6", alpha=.95)])
        if is_start:
            text.set_path_effects([
                path_effects.withStroke(linewidth=5, foreground="#fffdf6", alpha=.96),
                path_effects.withStroke(linewidth=1.5, foreground="#101010"),
            ])


def apply_torn_alpha(path, seed):
    """Découpe physiquement le PNG suivant un bord de papier irrégulier."""
    image = Image.open(path).convert("RGBA")
    width, height = image.size
    rng = random.Random(seed + 91)
    step = max(14, min(width, height) // 125)
    depth = max(12, min(width, height) // 86)
    corner = int(depth * 1.25)

    def inset():
        return rng.randint(max(3, depth // 5), int(depth * 1.30))

    top = [(corner, corner)]
    top += [(x, inset()) for x in range(corner + step, width - corner, step)]
    top += [(width - corner, corner)]
    right = [(width - inset(), y) for y in range(corner + step, height - corner, step)]
    right += [(width - corner, height - corner)]
    bottom = [(x, height - inset()) for x in range(width - corner - step, corner, -step)]
    bottom += [(corner, height - corner)]
    left = [(inset(), y) for y in range(height - corner - step, corner, -step)]
    mask = Image.new("L", image.size, 0)
    ImageDraw.Draw(mask).polygon(top + right + bottom + left, fill=255)
    mask = mask.filter(ImageFilter.GaussianBlur(max(1, depth // 12)))
    image.putalpha(ImageChops.multiply(image.getchannel("A"), mask))

    # Filet ombré fin et continu qui épouse exactement la découpe.
    expanded = mask.filter(ImageFilter.MaxFilter(9))
    outline = ImageChops.subtract(expanded, mask)
    outline = outline.filter(ImageFilter.GaussianBlur(1.1)).point(
        lambda value: int(value * .52)
    )
    shadow = Image.new("RGBA", image.size, (66, 57, 44, 0))
    shadow.putalpha(outline)
    Image.alpha_composite(shadow, image).save(path)


def write_webp(png_path, webp_path, maximum_bytes=400_000):
    """Produit un WebP aussi grand que possible sous la limite demandée."""
    cwebp = shutil.which("cwebp")
    if cwebp:
        quality = 48
        for _ in range(4):
            subprocess.run(
                [cwebp, "-quiet", "-q", str(quality), "-m", "6",
                 str(png_path), "-o", str(webp_path)],
                check=True,
            )
            size = Path(webp_path).stat().st_size
            if size <= maximum_bytes:
                return size, Image.open(png_path).size, quality
            quality = max(20, min(quality - 2, int(quality * (maximum_bytes / size) ** 3)))

    image = Image.open(png_path).convert("RGBA")
    resampling = getattr(Image, "Resampling", Image).LANCZOS
    quality_steps = (84, 72, 60, 48, 42, 38, 35)
    while True:
        for quality in quality_steps:
            buffer = io.BytesIO()
            image.save(buffer, format="WEBP", quality=quality, method=6)
            payload = buffer.getvalue()
            if len(payload) <= maximum_bytes:
                Path(webp_path).write_bytes(payload)
                return len(payload), image.size, quality
        new_size = (max(640, int(image.width * .88)), max(640, int(image.height * .88)))
        if new_size == image.size:
            raise ValueError("Impossible de produire un WebP inférieur à 400 ko")
        image = image.resize(new_size, resampling)


def draw_route_and_arrows(ax, route_xy, extent, towns, mean_lat, rng, jitter):
    """Dessine la route déjà découpée, puis les triangles dans ses trous."""
    width, height = extent[1] - extent[0], extent[3] - extent[2]
    figure_width, figure_height = ax.figure.get_size_inches()
    unit = np.asarray((width / (figure_width * 72), height / (figure_height * 72)))
    screen_xy = route_xy / unit
    factor = math.cos(math.radians(mean_lat))
    town_screen = np.asarray([
        (town["lon"] * factor / unit[0], town["lat"] / unit[1])
        for town in towns
    ])
    lengths = np.r_[0, np.cumsum(np.linalg.norm(np.diff(screen_xy, axis=0), axis=1))]
    route_line = LineString(screen_xy)
    triangle_length = 30
    half_base = 13

    def straight_center(fraction, search=0):
        target = lengths[-1] * fraction
        if not search:
            return int(np.abs(lengths - target).argmin())
        candidates = np.where(
            (lengths >= lengths[-1] * (fraction - search))
            & (lengths <= lengths[-1] * (fraction + search))
        )[0]
        scored = []
        for index in candidates:
            if index < 5 or index >= len(route_xy) - 5:
                continue
            before = route_xy[index] - route_xy[index - 5]
            after = route_xy[index + 5] - route_xy[index]
            before /= np.linalg.norm(before) or 1
            after /= np.linalg.norm(after) or 1
            bend = math.acos(np.clip(np.dot(before, after), -1, 1))
            town_distance = (
                np.min(np.linalg.norm(town_screen - screen_xy[index], axis=1))
                if len(town_screen) else 9999
            )
            # Écarte fortement les emplacements proches d'un point ou de son
            # nom ; parmi les autres, privilégie le tronçon le plus droit.
            penalty = max(0, 240 - town_distance) / 20
            scored.append((bend + penalty, index))
        return min(scored)[1] if scored else int(np.abs(lengths - target).argmin())

    gaps = []
    triangles = []
    for center in (straight_center(.07, .04), straight_center(.55, .08)):
        target = lengths[center]
        base_distance = max(0, target - triangle_length / 2)
        tip_distance = min(lengths[-1], target + triangle_length / 2)
        # Le trou transparent dépasse légèrement le triangle : les extrémités
        # irrégulières des deux passes de la trace ne peuvent plus ressortir.
        gap_start = max(0, base_distance - 5)
        gap_end = min(lengths[-1], tip_distance + 5)
        base = np.asarray(route_line.interpolate(base_distance).coords[0])
        tip = np.asarray(route_line.interpolate(tip_distance).coords[0])
        direction = tip - base
        direction /= np.linalg.norm(direction) or 1
        normal = np.asarray((-direction[1], direction[0]))
        triangle_screen = np.asarray([
            tip,
            base + normal * half_base,
            base - normal * half_base,
        ])
        gaps.append((gap_start, gap_end))
        triangles.append(triangle_screen * unit)

    # Aucun masque blanc : les deux morceaux sous les triangles ne sont tout
    # simplement jamais dessinés. Le fond cartographique reste donc intact.
    cursor = 0
    for gap_start, gap_end in sorted(gaps):
        segment = substring(route_line, cursor, gap_start)
        segment_xy = np.asarray(segment.coords) * unit
        rough_line(ax, segment_xy, rng, "#0d0d0d", 8.8, 6,
                   jitter=jitter, passes=2, alpha=1.9)
        cursor = gap_end
    segment = substring(route_line, cursor, lengths[-1])
    segment_xy = np.asarray(segment.coords) * unit
    rough_line(ax, segment_xy, rng, "#0d0d0d", 8.8, 6,
               jitter=jitter, passes=2, alpha=1.9)

    for triangle in triangles:
        ax.fill(
            triangle[:, 0], triangle[:, 1],
            facecolor="#050505", edgecolor="none", zorder=13,
        )


def render(title, summary, points, towns, water, output, seed):
    global ROUTE_FOR_LABELS, SEA_FOR_LABELS
    rng = np.random.default_rng(seed)
    py_rng = random.Random(seed)
    mean_lat, route_xy, extent, _, _ = map_layout(points)
    ROUTE_FOR_LABELS = route_xy
    drawn_route_xy = smooth_route(route_xy)
    span_x, span_y = np.ptp(route_xy[:, 0]), np.ptp(route_xy[:, 1])
    width, height = extent[1] - extent[0], extent[3] - extent[2]
    ratio = (extent[1] - extent[0]) / (extent[3] - extent[2])
    # PNG maître haute définition. Le WebP est redimensionné séparément pour
    # respecter sa limite de poids, sans appauvrir l'original cartographique.
    figure_dpi = 240
    figure_width = 18
    figsize = (figure_width, figure_width / ratio)
    fig, ax = plt.subplots(figsize=figsize, dpi=figure_dpi, facecolor="#fffdf6")
    ax.set_facecolor("#fffdf6")
    watercolor(ax, route_xy, rng)

    terrain = terrain_grid(points)
    if terrain is not None:
        longitudes, latitudes, elevation = terrain
        terrain_x = longitudes * math.cos(math.radians(mean_lat))
        ax.contourf(
            terrain_x, latitudes, elevation,
            levels=[250, 500, 900, 1400, 9000],
            colors=["#d9d6a7", "#c9c38d", "#b3ae77", "#99935f"],
            alpha=.20,
            antialiased=True,
            zorder=.8,
        )

    SEA_FOR_LABELS = sea_polygons(water, route_xy, extent, mean_lat)
    for polygon in SEA_FOR_LABELS:
        x, y = polygon.exterior.xy
        ax.fill(x, y, color="#a9dfe7", alpha=.27, linewidth=0, zorder=.7)

    for item in water:
        xy = project(item["points"], mean_lat)
        if item["kind"] in {"wood", "forest"}:
            if abs(np.ptp(xy[:, 0]) * np.ptp(xy[:, 1])) < span_x * span_y * .00002:
                continue
            ax.fill(xy[:, 0], xy[:, 1], color="#67a861", alpha=.17,
                    linewidth=0, zorder=.95)
        elif item["kind"] == "water":
            if abs(np.ptp(xy[:, 0]) * np.ptp(xy[:, 1])) < span_x * span_y * .000035:
                continue
            ax.fill(xy[:, 0], xy[:, 1], color="#8fd5e2", alpha=.50, zorder=1)
            rough_line(ax, xy, rng, "#63b7c8", .8, 2,
                       jitter=max(span_x, span_y) * .00008, passes=1, alpha=.7)
        elif item["kind"] == "coastline":
            rough_line(ax, xy, rng, "#63b7c8", 1.1, 2,
                       jitter=max(span_x, span_y) * .0001, passes=1, alpha=.75)
        else:
            if not item.get("name") or np.ptp(xy[:, 0]) + np.ptp(xy[:, 1]) < (span_x + span_y) * .018:
                continue
            rough_line(ax, xy, rng, "#68bdcb", 1.35 if item["kind"] == "river" else .9, 2,
                       jitter=max(span_x, span_y) * .00016, passes=2, alpha=.42)

    draw_route_and_arrows(
        ax, drawn_route_xy, extent, towns, mean_lat, rng,
        max(span_x, span_y) * .00007,
    )
    place_labels(ax, towns, mean_lat, extent, py_rng)

    title_text = ax.text(
        extent[0] + (extent[1] - extent[0]) * .03,
        extent[3] - (extent[3] - extent[2]) * .035,
        title,
        ha="left",
        va="top",
        fontproperties=font_properties(62, True),
        color="#111111",
        zorder=10,
    )
    title_text.set_path_effects([path_effects.withStroke(linewidth=4, foreground="#fffdf6")])
    if summary:
        legend = ax.text(
            extent[0] + (extent[1] - extent[0]) * .03,
        extent[3] - (extent[3] - extent[2]) * .125,
            summary,
            ha="left", va="top",
            fontproperties=font_properties(28, True),
            color="#222222", zorder=10,
            linespacing=1.25,
        )
        legend.set_path_effects([path_effects.withStroke(linewidth=3, foreground="#fffdf6")])

    # Rose des vents fournie, détourée automatiquement sur le papier.
    compass_path = os.path.join(assets_dir, "compass.png")
    if os.path.exists(compass_path):
        compass = plt.imread(compass_path)
        if compass.ndim == 2:
            compass = np.repeat(compass[..., None], 3, axis=2)
        if np.issubdtype(compass.dtype, np.integer):
            compass = compass.astype(float) / np.iinfo(compass.dtype).max
        dark = np.min(compass[..., :3], axis=2) < .92
        rows, columns = np.where(dark)
        original_alpha = compass[..., 3] if compass.shape[2] == 4 else None
        compass = compass[max(0, rows.min() - 10):rows.max() + 11,
                          max(0, columns.min() - 10):columns.max() + 11, :3]
        if original_alpha is not None:
            alpha = original_alpha[max(0, rows.min() - 10):rows.max() + 11,
                                   max(0, columns.min() - 10):columns.max() + 11]
        else:
            alpha = np.clip((1 - np.min(compass, axis=2)) * 3.5, 0, 1)
        rgba = np.dstack((compass, alpha))
        compass_points = 170
        zoom = compass_points / max(rgba.shape[:2])
        ax.add_artist(AnnotationBbox(
            OffsetImage(rgba, zoom=zoom, interpolation="bilinear"),
            (extent[1] - width * .035, extent[2] + height * .035),
            frameon=False, box_alignment=(1, 0), zorder=12,
        ))

    ax.set_xlim(extent[0], extent[1])
    ax.set_ylim(extent[2], extent[3])
    ax.set_aspect("equal")
    ax.axis("off")
    fig.subplots_adjust(0, 0, 1, 1)
    fig.savefig(output, dpi=figure_dpi, facecolor="#fffdf6", pad_inches=.08)
    plt.close(fig)
    apply_torn_alpha(output, seed)


def write_prompt(path, title, towns, map_output):
    payload = {
        "use_case": "infographic-diagram",
        "reference_image": "assets/map-exemple.jpg",
        "geometry_source": str(map_output),
        "primary_request": "Transformer la carte algorithmique en carte bikepacking dessinée à la main sans modifier la géographie.",
        "style": "encre noire irrégulière, lavis aquarelle vert pâle, eau bleu clair, papier blanc cassé, typographie manuscrite",
        "title_exact": title,
        "towns_exact": [town["name"] for town in towns],
        "constraints": [
            "conserver exactement le tracé, l'ordre et la position relative des villes",
            "ne jamais inventer ni corriger les noms",
            "conserver les mers et grandes rivières",
            "aucune route secondaire, aucune frontière administrative",
            "aucun photoréalisme, aucune 3D, aucun filigrane",
        ],
    }
    Path(path).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def main():
    options = arguments()
    stem = Path(options.gpx).stem
    road_book = options.road_book or os.path.join(output_folder, f"{stem}_road_book.md")
    output = options.output or os.path.join(output_folder, f"{stem}_map.png")
    webp_output = str(Path(output).with_suffix(".webp"))
    prompt = os.path.join(output_folder, f"{stem}_map_prompt.json")
    html_path = options.html or os.path.join(output_folder, f"{stem}.html")

    title, points = read_gpx(options.gpx)
    distances = cumulative_km(points)
    mean_lat, _, extent, pixel_width, pixel_height = map_layout(points)
    towns = remove_nearby_towns(
        coordinates_at_km(
            select_towns(read_road_book(road_book), options.cities), points, distances
        ),
        mean_lat,
        options.cities,
        extent,
        (pixel_width, pixel_height),
    )

    cache.init_cache(f"map-{Path(options.gpx).name}")
    water = [] if options.no_water else water_geometries(points)
    render(title, read_html_summary(html_path), points, towns, water, output, options.seed)
    webp_bytes, webp_size, webp_quality = write_webp(output, webp_output)
    write_prompt(prompt, title, towns, output)
    cache.close_cache()
    print(f"Carte générée : {output}")
    print(
        f"Carte WebP : {webp_output} "
        f"({webp_bytes / 1000:.0f} ko, {webp_size[0]}×{webp_size[1]}, qualité {webp_quality})"
    )
    print(f"Prompt IA optionnel : {prompt}")


if __name__ == "__main__":
    main()
