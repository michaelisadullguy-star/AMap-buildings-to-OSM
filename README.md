# AMap-buildings-to-OSM

`amap2osm.py` is a single-file pipeline that, given a polygon (raw
`lon,lat` vertices, a GeoJSON, or a Shapefile), will:

1. Crawl Amap (Gaode) building vector tiles inside the polygon.
2. Write the footprints to an ESRI Shapefile.
3. Detect whether the source coordinates are GCJ-02 encrypted (the
   "Mars" offset Amap applies inside mainland China) and, if so,
   reverse it to real WGS-84.
4. Emit OSM data as `.osm`, an `.osc` changeset, or both.

## Install

```bash
pip install -r requirements.txt
```

## API key

All Amap data endpoints now require a developer key. Get one from
<https://console.amap.com/dev/key/app> (free tier) and pass it via
`--key` or the `AMAP_KEY` environment variable. In GitHub Actions, add
it as a repository secret named `AMAP_KEY`.

## Use

```bash
# AOI mode: boundary from a GeoJSON, official Amap REST API
export AMAP_KEY=<your key>
python amap2osm.py --boundary area.geojson --out out/ --zoom 16

# raw polygon vertices (lon,lat, space separated; ring auto-closed)
python amap2osm.py \
  --vertices "116.39,39.90 116.41,39.90 116.41,39.92 116.39,39.92" \
  --out out/ --osm-format osm

# force-disable GCJ-02 decryption (e.g. data already in WGS-84)
python amap2osm.py --boundary area.shp --decrypt no

# legacy tile mode (no key, only if you have a working tile URL)
python amap2osm.py --boundary area.geojson \
  --tile-url 'https://example.tld/path?z={z}&x={x}&y={y}'
```

### Notable flags

| Flag            | Default      | Meaning                                    |
|-----------------|--------------|--------------------------------------------|
| `--zoom`        | `16`         | Tile zoom; 16 is where Amap serves footprints. |
| `--tile-url`    | (built-in)   | URL template with `{z}/{x}/{y}` placeholders. |
| `--decrypt`     | `auto`       | `auto` toggles on if the boundary's centroid lies in China. |
| `--osm-format`  | `both`       | `osm`, `osc`, `both`, or `none`.            |
| `--workers`     | `8`          | Concurrent tile fetchers.                   |

## Coordinate handling

The pipeline always treats your boundary as **WGS-84**. Inside China:

1. The boundary is forward-projected to **GCJ-02** to select the right
   Amap tiles in Mars-coord space.
2. Building geometries returned by Amap are iteratively reverse-projected
   back to WGS-84 (sub-millimetre round-trip with `gcj02_to_wgs84`).
3. Buildings are clipped against the original WGS-84 boundary.

Force the behaviour with `--decrypt yes|no`; `auto` (default) keys off
whether the boundary's centroid lies in mainland China.

## OSM tags

`write_osm` emits OSM-conformant tags only:

| Tag                | Source                             |
|--------------------|-------------------------------------|
| `building=yes`     | default                             |
| `building=<type>`  | mapped from Amap `type` when it has a clean OSM equivalent (`residential`, `apartments`, `office`, `school`, …) |
| `name`             | non-empty, whitespace-stripped      |
| `height`           | bare numeric metres (e.g. `24.5`)   |
| `building:levels`  | positive integer                    |
| `source=AMap`      | always                              |
| `ref:amap=<id>`    | when Amap returns a stable id       |

Empty, zero, or malformed values are dropped.

## Test fixtures + GitHub Actions

`test_input/` contains a Nanjing test case: three adjacent 街道
boundaries (`outer.geojson`) and the existing OSM buildings inside
them (`inner.osm`). `prep_scope.py` unions the three districts
(`unary_union` collapses their shared collinear edges) and subtracts
the existing buildings to produce `scope.geojson`.

Because Amap is not reachable from every environment, the actual crawl
runs in GitHub Actions:

```
.github/workflows/amap-test.yml
```

Trigger via the *Actions* tab → *Amap building crawl test* → *Run
workflow*. Tunables (`zoom`, `tile_url`, `workers`) are exposed as
workflow inputs. The job uploads `buildings.osm`, `buildings.osc`,
`buildings.shp`, and the merged `scope.geojson` as artifacts.

## Notes

* Amap's building endpoint is not officially published; the default URL
  template targets the de-facto public endpoint. If Amap changes it,
  override with `--tile-url`.
* OSM output uses negative placeholder IDs so the file can be opened in
  JOSM and uploaded as new objects.
