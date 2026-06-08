#!/usr/bin/env python3
"""
amap2osm — Crawl Amap (Gaode) building footprints inside a polygon,
write SHP, optionally undo GCJ-02 encryption, and emit OSM .osm/.osc.

Usage examples
--------------
  # boundary from a GeoJSON file, write all outputs
  python amap2osm.py --boundary area.geojson --out out/ --zoom 16

  # boundary from an SHP, only emit a .osm file
  python amap2osm.py --boundary area.shp --out out/ --osm-format osm

  # boundary from raw lon,lat vertices (closes the ring automatically)
  python amap2osm.py --vertices "116.39,39.90 116.41,39.90 116.41,39.92 116.39,39.92"
"""
from __future__ import annotations

import json
import math
import sys
import time
import random
import logging
import argparse
import xml.etree.ElementTree as ET
import concurrent.futures as cf
from pathlib import Path
from typing import Iterator

import requests
import mercantile
import fiona
from fiona.crs import from_epsg
from shapely.geometry import shape, mapping, Polygon, MultiPolygon, box
from shapely.ops import unary_union, transform as shp_transform


# ---------------------------------------------------------------------------
# Amap tile endpoint
# ---------------------------------------------------------------------------
# Amap's building endpoint is not officially documented; the de-facto URL used
# by community building-extractors is the AOI/building dataservice. It returns
# JSON with a list of features, each carrying a polygon path in GCJ-02.
# Override with --tile-url if Amap changes the path.
DEFAULT_TILE_URL = (
    "https://ditu.amap.com/dataservice/buildings"
    "?z={z}&x={x}&y={y}"
)
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


# ---------------------------------------------------------------------------
# GCJ-02  <->  WGS-84
# ---------------------------------------------------------------------------
# Reverse-engineered Mars-coordinate offset used inside mainland China.
GCJ_A = 6378245.0
GCJ_EE = 0.00669342162296594323


def _out_of_china(lon: float, lat: float) -> bool:
    return not (72.004 < lon < 137.8347 and 0.8293 < lat < 55.8271)


def _transform_lat(x: float, y: float) -> float:
    r = -100 + 2*x + 3*y + 0.2*y*y + 0.1*x*y + 0.2*math.sqrt(abs(x))
    r += (20*math.sin(6*x*math.pi) + 20*math.sin(2*x*math.pi)) * 2/3
    r += (20*math.sin(y*math.pi) + 40*math.sin(y/3*math.pi)) * 2/3
    r += (160*math.sin(y/12*math.pi) + 320*math.sin(y*math.pi/30)) * 2/3
    return r


def _transform_lon(x: float, y: float) -> float:
    r = 300 + x + 2*y + 0.1*x*x + 0.1*x*y + 0.1*math.sqrt(abs(x))
    r += (20*math.sin(6*x*math.pi) + 20*math.sin(2*x*math.pi)) * 2/3
    r += (20*math.sin(x*math.pi) + 40*math.sin(x/3*math.pi)) * 2/3
    r += (150*math.sin(x/12*math.pi) + 300*math.sin(x/30*math.pi)) * 2/3
    return r


def _gcj_offset(lon: float, lat: float) -> tuple[float, float]:
    """Return (dlon, dlat) the GCJ-02 obfuscation adds at (lon, lat) WGS-84."""
    dlat = _transform_lat(lon - 105.0, lat - 35.0)
    dlon = _transform_lon(lon - 105.0, lat - 35.0)
    rad = lat / 180.0 * math.pi
    magic = 1 - GCJ_EE * math.sin(rad) ** 2
    sqrtm = math.sqrt(magic)
    dlat = (dlat * 180.0) / ((GCJ_A * (1 - GCJ_EE)) / (magic * sqrtm) * math.pi)
    dlon = (dlon * 180.0) / (GCJ_A / sqrtm * math.cos(rad) * math.pi)
    return dlon, dlat


def wgs84_to_gcj02(lon: float, lat: float) -> tuple[float, float]:
    """Forward Mars-coord obfuscation. Used to project a WGS-84 query area
    into GCJ-02 space so we fetch the correct Amap tiles."""
    if _out_of_china(lon, lat):
        return lon, lat
    dlon, dlat = _gcj_offset(lon, lat)
    return lon + dlon, lat + dlat


def gcj02_to_wgs84(lon: float, lat: float) -> tuple[float, float]:
    """Inverse of the GCJ-02 obfuscation (Mars coords -> real WGS-84).
    Iterates the forward offset to converge to sub-millimetre precision."""
    if _out_of_china(lon, lat):
        return lon, lat
    wlon, wlat = lon, lat
    for _ in range(4):
        dlon, dlat = _gcj_offset(wlon, wlat)
        wlon, wlat = lon - dlon, lat - dlat
    return wlon, wlat


def is_china_bbox(geom) -> bool:
    """Cheap heuristic: any polygon with its centroid inside mainland China
    is assumed to be served by Amap in GCJ-02."""
    c = geom.centroid
    return not _out_of_china(c.x, c.y)


def project_wgs_to_gcj(geom):
    return shp_transform(lambda x, y, z=None: wgs84_to_gcj02(x, y), geom)


def project_gcj_to_wgs(geom):
    return shp_transform(lambda x, y, z=None: gcj02_to_wgs84(x, y), geom)


# ---------------------------------------------------------------------------
# Boundary loading
# ---------------------------------------------------------------------------
def load_boundary(path: str | None, vertices: str | None):
    if vertices:
        coords = [tuple(map(float, p.split(","))) for p in vertices.split()]
        if coords[0] != coords[-1]:
            coords.append(coords[0])
        return Polygon(coords)
    p = Path(path)
    if p.suffix.lower() in {".geojson", ".json"}:
        data = json.loads(p.read_text())
        if data.get("type") == "FeatureCollection":
            geoms = [shape(f["geometry"]) for f in data["features"]]
        elif data.get("type") == "Feature":
            geoms = [shape(data["geometry"])]
        else:
            geoms = [shape(data)]
        return unary_union(geoms)
    if p.suffix.lower() == ".shp":
        with fiona.open(str(p)) as src:
            return unary_union([shape(f["geometry"]) for f in src])
    sys.exit(f"unsupported boundary format: {p.suffix}")


# ---------------------------------------------------------------------------
# Tile crawl
# ---------------------------------------------------------------------------
def tiles_for(geom, zoom: int) -> Iterator[mercantile.Tile]:
    minx, miny, maxx, maxy = geom.bounds
    for t in mercantile.tiles(minx, miny, maxx, maxy, zooms=[zoom]):
        tb = box(*mercantile.bounds(t))
        if tb.intersects(geom):
            yield t


def fetch_tile(session: requests.Session, url_tpl: str, t: mercantile.Tile,
               retries: int = 3) -> dict | None:
    url = url_tpl.format(z=t.z, x=t.x, y=t.y)
    for attempt in range(retries):
        try:
            r = session.get(url, timeout=20, headers={"User-Agent": UA,
                                                     "Referer": "https://ditu.amap.com/"})
            if r.status_code == 200 and r.content:
                try:
                    return r.json()
                except ValueError:
                    return {"_raw": r.text}
            if r.status_code in (403, 429):
                time.sleep(2 ** attempt + random.random())
                continue
            return None
        except requests.RequestException:
            time.sleep(2 ** attempt + random.random())
    logging.warning("failed tile %s", t)
    return None


# ---------------------------------------------------------------------------
# Feature extraction from Amap responses
# ---------------------------------------------------------------------------
# Amap building responses come in a few historical shapes. We probe common
# field names ("paths" / "points" / "shape" / GeoJSON-ish "geometry") and
# return clean (polygon, attrs) pairs.
def _parse_path_string(s: str) -> list[tuple[float, float]]:
    # "lon,lat;lon,lat" or "lon,lat|lon,lat"
    sep = ";" if ";" in s else "|"
    out = []
    for pair in s.split(sep):
        if "," in pair:
            lon, lat = pair.split(",")[:2]
            out.append((float(lon), float(lat)))
    return out


def extract_buildings(payload: dict) -> Iterator[tuple[Polygon, dict]]:
    if not payload:
        return
    if "_raw" in payload:
        return
    candidates = []
    for key in ("data", "buildings", "features", "list", "result"):
        v = payload.get(key)
        if isinstance(v, list):
            candidates = v
            break
    if not candidates and isinstance(payload, list):
        candidates = payload
    for f in candidates:
        if not isinstance(f, dict):
            continue
        geom = f.get("geometry")
        if isinstance(geom, dict):
            try:
                g = shape(geom)
            except Exception:
                continue
            if isinstance(g, (Polygon, MultiPolygon)):
                for poly in (g.geoms if isinstance(g, MultiPolygon) else [g]):
                    yield poly, _attrs(f)
            continue
        for fld in ("paths", "shape", "points", "path"):
            raw = f.get(fld)
            if isinstance(raw, str):
                pts = _parse_path_string(raw)
            elif isinstance(raw, list):
                pts = [tuple(p[:2]) for p in raw if isinstance(p, (list, tuple))]
            else:
                continue
            if len(pts) >= 3:
                if pts[0] != pts[-1]:
                    pts.append(pts[0])
                try:
                    yield Polygon(pts), _attrs(f)
                except Exception:
                    pass
                break


def _attrs(f: dict) -> dict:
    pick = {}
    for k in ("name", "height", "floor", "levels", "type", "id"):
        v = f.get(k)
        if v is None or isinstance(v, (dict, list)):
            continue
        pick[k] = v
    return pick


# Amap 'type' / category strings we can map to a valid OSM building=* value.
# Anything not in the table falls back to building=yes (the safest default).
AMAP_TYPE_TO_OSM_BUILDING = {
    "residential": "residential",
    "apartment":   "apartments",
    "apartments":  "apartments",
    "house":       "house",
    "commercial":  "commercial",
    "office":      "office",
    "retail":      "retail",
    "shop":        "retail",
    "industrial":  "industrial",
    "factory":     "industrial",
    "warehouse":   "warehouse",
    "school":      "school",
    "university":  "university",
    "hospital":    "hospital",
    "church":      "church",
    "temple":      "temple",
    "mosque":      "mosque",
    "garage":      "garage",
    "hotel":       "hotel",
    "stadium":     "stadium",
    "train":       "train_station",
    "station":     "train_station",
}


def clean_osm_tags(attrs: dict) -> dict:
    """Return a dict of OSM-conformant tags for a building.
    Strictly drops empty, zero, or malformed values; normalises numbers to
    plain numeric strings (height in metres, levels as integer).
    """
    tags: dict[str, str] = {"building": "yes", "source": "AMap"}

    raw_type = str(attrs.get("type") or "").strip().lower()
    if raw_type in AMAP_TYPE_TO_OSM_BUILDING:
        tags["building"] = AMAP_TYPE_TO_OSM_BUILDING[raw_type]

    name = attrs.get("name")
    if isinstance(name, str) and name.strip():
        tags["name"] = name.strip()

    h = attrs.get("height")
    try:
        hv = float(h) if h not in (None, "") else 0.0
    except (TypeError, ValueError):
        hv = 0.0
    if hv > 0:
        tags["height"] = f"{hv:g}"  # bare number, OSM convention = metres

    lv = attrs.get("levels") if attrs.get("levels") is not None else attrs.get("floor")
    try:
        lvi = int(float(lv)) if lv not in (None, "") else 0
    except (TypeError, ValueError):
        lvi = 0
    if lvi > 0:
        tags["building:levels"] = str(lvi)

    aid = attrs.get("id")
    if aid not in (None, "", 0):
        tags["ref:amap"] = str(aid)

    return tags


# ---------------------------------------------------------------------------
# Writers
# ---------------------------------------------------------------------------
SHP_SCHEMA = {
    "geometry": "Polygon",
    "properties": {
        "id":     "str",
        "name":   "str",
        "height": "float",
        "levels": "int",
    },
}


def write_shp(path: str, polys: list[tuple[Polygon, dict]]):
    with fiona.open(path, "w", driver="ESRI Shapefile",
                    crs=from_epsg(4326), schema=SHP_SCHEMA) as dst:
        for i, (g, a) in enumerate(polys):
            dst.write({
                "geometry": mapping(g),
                "properties": {
                    "id":     str(a.get("id", i)),
                    "name":   str(a.get("name", "") or ""),
                    "height": float(a.get("height") or 0) or None,
                    "levels": int(a.get("levels") or a.get("floor") or 0) or None,
                },
            })


def write_osm(path: str, polys: list[tuple[Polygon, dict]], *,
              changeset: bool = False):
    """Emit either a .osm file (action='create' implicit) or a .osc changeset.
    Tags are produced by `clean_osm_tags` and conform to OSM conventions."""
    if changeset:
        root = ET.Element("osmChange", version="0.6", generator="amap2osm")
        parent = ET.SubElement(root, "create")
    else:
        root = ET.Element("osm", version="0.6", generator="amap2osm")
        parent = root

    node_id, way_id = -1, -1
    for poly, attrs in polys:
        coords = list(poly.exterior.coords)
        if coords[0] == coords[-1]:
            coords = coords[:-1]
        node_refs = []
        for lon, lat in coords:
            n = ET.SubElement(parent, "node", id=str(node_id), visible="true",
                              version="1", lat=f"{lat:.7f}", lon=f"{lon:.7f}")
            if changeset:
                n.set("changeset", "0")
            node_refs.append(node_id)
            node_id -= 1
        w = ET.SubElement(parent, "way", id=str(way_id), visible="true",
                          version="1")
        if changeset:
            w.set("changeset", "0")
        for ref in node_refs + [node_refs[0]]:
            ET.SubElement(w, "nd", ref=str(ref))
        for k, v in clean_osm_tags(attrs).items():
            ET.SubElement(w, "tag", k=k, v=v)
        way_id -= 1

    ET.indent(root, space="  ")
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------
def run(boundary, *, zoom: int, out: Path, tile_url: str,
        decrypt: str, workers: int, osm_format: str):
    """boundary is always interpreted as WGS-84.

    Inside China, we project it FORWARD to GCJ-02 to select the right Amap
    tiles, fetch them, then reverse-project the returned (GCJ-02) building
    geometries back to WGS-84 and clip against the original WGS-84 boundary.
    """
    out.mkdir(parents=True, exist_ok=True)

    if decrypt == "auto":
        do_decrypt = is_china_bbox(boundary)
    else:
        do_decrypt = decrypt == "yes"
    logging.info("GCJ-02 decrypt: %s", do_decrypt)

    query_geom = project_wgs_to_gcj(boundary) if do_decrypt else boundary
    tiles = list(tiles_for(query_geom, zoom))
    logging.info("crawling %d tiles at z=%d (URL=%s)",
                 len(tiles), zoom, tile_url)

    payloads = []
    with requests.Session() as s, cf.ThreadPoolExecutor(workers) as ex:
        for p in ex.map(lambda t: fetch_tile(s, tile_url, t), tiles):
            if p is not None:
                payloads.append(p)
    logging.info("got %d non-empty tile responses", len(payloads))

    raw = []
    for p in payloads:
        raw.extend(extract_buildings(p))
    logging.info("parsed %d raw building polygons", len(raw))
    if raw == [] and payloads:
        logging.warning("Tiles returned data but no buildings parsed -- "
                        "the endpoint shape may have changed; check --tile-url "
                        "and the response format.")

    if do_decrypt:
        raw = [(project_gcj_to_wgs(g), a) for g, a in raw]

    # Clip against the original WGS-84 boundary.
    flat: list[tuple[Polygon, dict]] = []
    for g, a in raw:
        if not g.is_valid:
            g = g.buffer(0)
        if g.is_empty or not g.intersects(boundary):
            continue
        clipped = g.intersection(boundary)
        if clipped.is_empty:
            continue
        if isinstance(clipped, MultiPolygon):
            for part in clipped.geoms:
                if part.geom_type == "Polygon" and part.area > 0:
                    flat.append((part, a))
        elif clipped.geom_type == "Polygon" and clipped.area > 0:
            flat.append((clipped, a))
    logging.info("retained %d buildings after clip", len(flat))

    shp_path = out / "buildings.shp"
    write_shp(str(shp_path), flat)
    logging.info("wrote %s", shp_path)

    if osm_format in ("osm", "both"):
        write_osm(str(out / "buildings.osm"), flat, changeset=False)
        logging.info("wrote %s", out / "buildings.osm")
    if osm_format in ("osc", "changeset", "both"):
        write_osm(str(out / "buildings.osc"), flat, changeset=True)
        logging.info("wrote %s", out / "buildings.osc")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--boundary", help="GeoJSON or SHP path")
    src.add_argument("--vertices", help='Polygon vertices: "lon,lat lon,lat ..."')
    ap.add_argument("--out", default="out", type=Path)
    ap.add_argument("--zoom", type=int, default=16,
                    help="Amap tile zoom level (16 is typical for buildings)")
    ap.add_argument("--tile-url", default=DEFAULT_TILE_URL,
                    help="Override Amap tile URL template ({z},{x},{y})")
    ap.add_argument("--decrypt", choices=("auto", "yes", "no"), default="auto",
                    help="Reverse GCJ-02 offset to true WGS-84")
    ap.add_argument("--osm-format", choices=("osm", "osc", "both", "none"),
                    default="both")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )

    boundary = load_boundary(args.boundary, args.vertices)
    run(boundary,
        zoom=args.zoom,
        out=args.out,
        tile_url=args.tile_url,
        decrypt=args.decrypt,
        workers=args.workers,
        osm_format=args.osm_format)


if __name__ == "__main__":
    main()
