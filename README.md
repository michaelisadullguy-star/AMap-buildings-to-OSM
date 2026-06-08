# Buildings → OSM

`buildings2osm.py` takes a polygon (raw `lon,lat` vertices, a GeoJSON, or a
Shapefile) and:

1. Computes the Microsoft GlobalML quadtree (z9) tiles covering it and
   selects the matching rows from Microsoft's `dataset-links.csv`.
2. Streams each gzipped GeoJSON-Lines footprint file and keeps the buildings
   whose representative point falls inside the polygon.
3. Confirms the source CRS is **EPSG:4326 (WGS-84)** — Microsoft footprints
   carry no GCJ-02 "Mars" offset, so no decryption is needed.
4. Writes an ESRI Shapefile and an OSM `.osm` file and/or `.osc` changeset.

Data source: [microsoft/GlobalMLBuildingFootprints](https://github.com/microsoft/GlobalMLBuildingFootprints)
(ODbL-compatible, already in WGS-84).

## Install

```bash
pip install -r requirements.txt
```

## Use

```bash
# boundary from a GeoJSON; produce buildings.shp, .osm and .osc
python buildings2osm.py --boundary scope.geojson --out out/

# raw polygon vertices (lon,lat, space separated; ring auto-closed)
python buildings2osm.py \
  --vertices "118.75,32.08 118.80,32.08 118.80,32.12 118.75,32.12" \
  --out out/ --osm-format osm
```

| Flag           | Default                    | Meaning                              |
|----------------|----------------------------|--------------------------------------|
| `--boundary`   | —                          | GeoJSON or SHP boundary file.        |
| `--vertices`   | —                          | Inline `lon,lat` polygon vertices.   |
| `--out`        | `out`                      | Output directory.                    |
| `--osm-format` | `both`                     | `osm`, `osc`, `both`, or `none`.     |
| `--links-url`  | Microsoft dataset index    | Override the `dataset-links.csv` URL.|

## OSM tags

Footprints are tagged strictly per OSM conventions — nothing is invented:

| Tag             | Source                                            |
|-----------------|---------------------------------------------------|
| `building=yes`  | every footprint                                   |
| `height`        | `properties.height` (metres) when Microsoft has it (> 0) |

OSM output uses negative placeholder node/way IDs so the file opens in JOSM
and uploads as new objects.

## Test fixtures + CI

`test_input/` holds a Nanjing test case: three adjacent 街道 boundaries
(`outer.geojson`) and the existing OSM buildings inside them (`inner.osm`).
`prep_scope.py` unions the three districts (collinear shared edges collapse
via `unary_union`) and subtracts the existing buildings, producing
`scope.geojson` — i.e. only the currently-unmapped area.

`.github/workflows/buildings-test.yml` runs the full pipeline against that
scope and uploads `buildings.osm`, `buildings.osc`, `buildings.shp`, and the
merged `scope.geojson` as artifacts. No API key or secret is required — the
Microsoft dataset is public.
