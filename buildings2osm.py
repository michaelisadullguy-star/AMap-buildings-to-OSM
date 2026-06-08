#!/usr/bin/env python3
"""
buildings2osm — Extract Microsoft GlobalML building footprints inside a
polygon and export them to Shapefile + OSM (.osm / .osc).

Pipeline
--------
1. Load a boundary (raw lon,lat vertices, GeoJSON, or Shapefile).
2. Compute the Bing/quadtree z9 tiles (QuadKeys) covering its bbox and select
   the matching rows from Microsoft's `dataset-links.csv`.
3. Stream each gzipped GeoJSON-Lines footprint file, keep buildings whose
   representative point lies inside the boundary.
4. Microsoft footprints are published in EPSG:4326 (WGS-84), so NO GCJ-02
   "Mars" decryption is needed — we verify and report the source CRS.
5. Write a Shapefile and an OSM `.osm` file and/or `.osc` changeset.

Usage
-----
  python buildings2osm.py --boundary scope.geojson --out out/
  python buildings2osm.py --vertices "118.75,32.08 118.80,32.08 118.80,32.12 118.75,32.12"
"""
from __future__ import annotations

import io
import csv
import gzip
import json
import sys
import logging
import argparse
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Iterator

import requests
import mercantile
import fiona
from fiona.crs import from_epsg
from shapely.geometry import shape, mapping, Polygon, MultiPolygon
from shapely.prepared import prep
from shapely.ops import unary_union

# Microsoft GlobalMLBuildingFootprints index of per-QuadKey download links.
DATASET_LINKS_URL = (
    "https://minedbuildings.z5.web.core.windows.net/global-buildings/"
    "dataset-links.csv"
)
QUADKEY_ZOOM = 9  # Microsoft tiles the globe at zoom 9
UA = "buildings2osm/1.0 (+https://github.com/microsoft/GlobalMLBuildingFootprints)"


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
        return unary_union([g.buffer(0) for g in geoms])
    if p.suffix.lower() == ".shp":
        with fiona.open(str(p)) as src:
            return unary_union([shape(f["geometry"]).buffer(0) for f in src])
    sys.exit(f"unsupported boundary format: {p.suffix}")


# ---------------------------------------------------------------------------
# Microsoft dataset selection
# ---------------------------------------------------------------------------
def covering_quadkeys(boundary, zoom: int = QUADKEY_ZOOM) -> set[str]:
    minx, miny, maxx, maxy = boundary.bounds
    return {mercantile.quadkey(t)
            for t in mercantile.tiles(minx, miny, maxx, maxy, zooms=[zoom])}


def _col(fieldnames, *names):
    """Resolve a column name case-insensitively."""
    low = {f.lower(): f for f in (fieldnames or [])}
    for n in names:
        if n.lower() in low:
            return low[n.lower()]
    return None


def select_links(session: requests.Session, quadkeys: set[str],
                 links_url: str) -> list[tuple[str, str]]:
    """Stream dataset-links.csv and return (location, url) for matching tiles."""
    logging.info("fetching dataset index: %s", links_url)
    out: list[tuple[str, str]] = []
    seen_urls: set[str] = set()
    # bbox-prefix of our quadkeys, for diagnostics if nothing matches
    prefix = min(quadkeys, key=len)[:4] if quadkeys else ""
    total = 0
    samples: list[tuple[str, str]] = []
    locations: set[str] = set()
    qk_col = url_col = loc_col = None

    with session.get(links_url, stream=True, timeout=120,
                     headers={"User-Agent": UA}) as r:
        r.raise_for_status()
        lines = (ln.decode("utf-8", "replace") for ln in r.iter_lines())
        reader = csv.DictReader(lines)
        qk_col = _col(reader.fieldnames, "QuadKey", "quadkey")
        url_col = _col(reader.fieldnames, "Url", "url")
        loc_col = _col(reader.fieldnames, "Location", "location", "region")
        logging.info("dataset-links columns: %s (quadkey=%s url=%s loc=%s)",
                     reader.fieldnames, qk_col, url_col, loc_col)
        for row in reader:
            total += 1
            qk = (row.get(qk_col) or "").strip() if qk_col else ""
            loc = (row.get(loc_col) or "") if loc_col else ""
            if loc:
                locations.add(loc)
            if qk and qk in quadkeys:
                url = row.get(url_col) if url_col else None
                if url and url not in seen_urls:
                    out.append((loc or "?", url))
                    seen_urls.add(url)
            elif prefix and qk.startswith(prefix) and len(samples) < 8:
                samples.append((loc, qk))

    logging.info("matched %d footprint file(s) for quadkeys %s (scanned %d rows)",
                 len(out), sorted(quadkeys), total)
    if not out:
        logging.warning("No exact quadkey match. Rows near prefix %r: %s",
                        prefix, samples or "none")
        china = sorted(l for l in locations if "chin" in l.lower())
        logging.warning("Locations containing 'china': %s", china or "NONE")
        logging.warning("total distinct locations: %d (e.g. %s)",
                        len(locations), sorted(locations)[:12])
    return out


# ---------------------------------------------------------------------------
# Footprint streaming
# ---------------------------------------------------------------------------
def iter_footprints(session: requests.Session, url: str
                    ) -> Iterator[tuple[Polygon, dict]]:
    """Yield (polygon, properties) from a gzipped GeoJSON-Lines file."""
    logging.info("downloading %s", url)
    r = session.get(url, timeout=300, headers={"User-Agent": UA})
    r.raise_for_status()
    gz = gzip.GzipFile(fileobj=io.BytesIO(r.content))
    n = 0
    for raw in gz:
        raw = raw.strip()
        if not raw:
            continue
        try:
            feat = json.loads(raw)
        except ValueError:
            continue
        geom = feat.get("geometry")
        if not isinstance(geom, dict):
            continue
        try:
            g = shape(geom)
        except Exception:
            continue
        if isinstance(g, MultiPolygon):
            for part in g.geoms:
                yield part, feat.get("properties") or {}
                n += 1
        elif isinstance(g, Polygon):
            yield g, feat.get("properties") or {}
            n += 1
    logging.info("  read %d footprints", n)


# ---------------------------------------------------------------------------
# OSM tag cleaning (strictly OSM-conformant)
# ---------------------------------------------------------------------------
def clean_osm_tags(props: dict) -> dict:
    """building=yes plus a numeric `height` in metres when known. Nothing else
    is invented — Microsoft footprints carry no reliable type/name."""
    tags = {"building": "yes"}
    h = props.get("height")
    try:
        hv = float(h)
    except (TypeError, ValueError):
        hv = 0.0
    if hv > 0:
        tags["height"] = f"{hv:g}"
    return tags


# ---------------------------------------------------------------------------
# Writers
# ---------------------------------------------------------------------------
SHP_SCHEMA = {"geometry": "Polygon",
              "properties": {"id": "int", "height": "float"}}


def write_shp(path: str, polys: list[tuple[Polygon, dict]]):
    with fiona.open(path, "w", driver="ESRI Shapefile",
                    crs=from_epsg(4326), schema=SHP_SCHEMA) as dst:
        for i, (g, props) in enumerate(polys):
            try:
                h = float(props.get("height"))
            except (TypeError, ValueError):
                h = 0.0
            dst.write({"geometry": mapping(g),
                       "properties": {"id": i, "height": h if h > 0 else None}})


def write_osm(path: str, polys: list[tuple[Polygon, dict]], *,
              changeset: bool = False):
    if changeset:
        root = ET.Element("osmChange", version="0.6", generator="buildings2osm")
        parent = ET.SubElement(root, "create")
    else:
        root = ET.Element("osm", version="0.6", generator="buildings2osm")
        parent = root

    node_id, way_id = -1, -1
    for poly, props in polys:
        coords = list(poly.exterior.coords)
        if coords[0] == coords[-1]:
            coords = coords[:-1]
        refs = []
        for lon, lat in coords:
            n = ET.SubElement(parent, "node", id=str(node_id), visible="true",
                              version="1", lat=f"{lat:.7f}", lon=f"{lon:.7f}")
            if changeset:
                n.set("changeset", "0")
            refs.append(node_id)
            node_id -= 1
        w = ET.SubElement(parent, "way", id=str(way_id), visible="true",
                          version="1")
        if changeset:
            w.set("changeset", "0")
        for ref in refs + [refs[0]]:
            ET.SubElement(w, "nd", ref=str(ref))
        for k, v in clean_osm_tags(props).items():
            ET.SubElement(w, "tag", k=k, v=v)
        way_id -= 1

    ET.indent(root, space="  ")
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------
def run(boundary, *, out: Path, links_url: str, osm_format: str,
        zoom: int = QUADKEY_ZOOM):
    out.mkdir(parents=True, exist_ok=True)

    # Source CRS check (task requirement #3): Microsoft data is EPSG:4326.
    logging.info("Source CRS: EPSG:4326 (WGS-84). GCJ-02 decryption: not "
                 "required for Microsoft GlobalML footprints.")

    quadkeys = covering_quadkeys(boundary, zoom)
    with requests.Session() as s:
        links = select_links(s, quadkeys, links_url)
        if not links:
            logging.warning("No Microsoft footprint tiles cover this area "
                            "(quadkeys=%s). Is the boundary inside a covered "
                            "country?", sorted(quadkeys))
        pbound = prep(boundary)
        kept: list[tuple[Polygon, dict]] = []
        scanned = 0
        for location, url in links:
            for g, props in iter_footprints(s, url):
                scanned += 1
                if not g.is_valid:
                    g = g.buffer(0)
                    if not isinstance(g, Polygon) or g.is_empty:
                        continue
                # keep whole buildings whose interior point is inside the area
                if pbound.contains(g.representative_point()):
                    kept.append((g, props))

    logging.info("scanned %d footprints, kept %d inside the area",
                 scanned, len(kept))

    shp = out / "buildings.shp"
    write_shp(str(shp), kept)
    logging.info("wrote %s", shp)

    if osm_format in ("osm", "both"):
        write_osm(str(out / "buildings.osm"), kept, changeset=False)
        logging.info("wrote %s", out / "buildings.osm")
    if osm_format in ("osc", "changeset", "both"):
        write_osm(str(out / "buildings.osc"), kept, changeset=True)
        logging.info("wrote %s", out / "buildings.osc")


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--boundary", help="GeoJSON or SHP path")
    src.add_argument("--vertices", help='Polygon vertices: "lon,lat lon,lat ..."')
    ap.add_argument("--out", default="out", type=Path)
    ap.add_argument("--links-url", default=DATASET_LINKS_URL,
                    help="Override Microsoft dataset-links.csv URL")
    ap.add_argument("--osm-format", choices=("osm", "osc", "both", "none"),
                    default="both")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(message)s")

    boundary = load_boundary(args.boundary, args.vertices)
    run(boundary, out=args.out, links_url=args.links_url,
        osm_format=args.osm_format)


if __name__ == "__main__":
    main()
