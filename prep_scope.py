#!/usr/bin/env python3
"""Build a merged test-scope polygon from (outer GeoJSON, inner OSM buildings).

outer: FeatureCollection of adjacent admin polygons -> unary_union dissolves
       shared/collinear edges into a single (Multi)Polygon.
inner: .osm file -> every closed `building` way becomes a hole subtracted
       from the outer area.

Output: GeoJSON Feature with a single (Multi)Polygon geometry, EPSG:4326.
"""
from __future__ import annotations
import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

from shapely.geometry import shape, mapping, Polygon, MultiPolygon
from shapely.ops import unary_union


def load_outer(path: Path):
    data = json.loads(path.read_text())
    geoms = [shape(f["geometry"]) for f in data["features"]]
    # buffer(0) repairs any self-touch from collinear adjacency before union.
    geoms = [g.buffer(0) for g in geoms]
    merged = unary_union(geoms)
    print(f"outer: {len(geoms)} features -> merged geom_type={merged.geom_type}, "
          f"area={merged.area:.6f} deg^2", file=sys.stderr)
    return merged


def load_inner_osm(path: Path):
    """Parse OSM XML; return union of all closed `building=*` way polygons."""
    root = ET.parse(path).getroot()
    nodes = {n.get("id"): (float(n.get("lon")), float(n.get("lat")))
             for n in root.iter("node")}
    polys = []
    for w in root.iter("way"):
        tags = {t.get("k"): t.get("v") for t in w.findall("tag")}
        if "building" not in tags:
            continue
        refs = [nd.get("ref") for nd in w.findall("nd")]
        if len(refs) < 4 or refs[0] != refs[-1]:
            continue
        try:
            coords = [nodes[r] for r in refs]
        except KeyError:
            continue
        try:
            p = Polygon(coords)
            if not p.is_valid:
                p = p.buffer(0)
            if not p.is_empty and p.area > 0:
                polys.append(p)
        except Exception:
            pass
    print(f"inner: {len(polys)} building polygons", file=sys.stderr)
    if not polys:
        return None
    return unary_union(polys)


def _drop_slivers(geom, min_area_deg2: float):
    """Difference operations leave numerical slivers along shared edges.
    Drop anything below a minimum area (1e-8 deg^2 ~ 100 m^2 at this lat)."""
    if isinstance(geom, MultiPolygon):
        kept = [p for p in geom.geoms if p.area >= min_area_deg2]
        if not kept:
            return geom
        return MultiPolygon(kept) if len(kept) > 1 else kept[0]
    return geom


def main():
    outer_path = Path(sys.argv[1])
    inner_path = Path(sys.argv[2])
    out_path = Path(sys.argv[3])

    outer = load_outer(outer_path)
    inner = load_inner_osm(inner_path)

    if inner is not None:
        scope = outer.difference(inner)
    else:
        scope = outer

    scope = _drop_slivers(scope, 1e-8)

    parts = len(scope.geoms) if isinstance(scope, MultiPolygon) else 1
    print(f"scope: geom_type={scope.geom_type}, parts={parts}, "
          f"area={scope.area:.6f} deg^2", file=sys.stderr)

    feat = {"type": "Feature", "properties": {"source": "outer\\inner"},
            "geometry": mapping(scope)}
    out_path.write_text(json.dumps(feat))
    print(f"wrote {out_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
