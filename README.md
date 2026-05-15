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

## Use

```bash
# boundary from a GeoJSON; produce buildings.shp, .osm and .osc
python amap2osm.py --boundary area.geojson --out out/ --zoom 16

# raw polygon vertices (lon,lat, space separated; ring auto-closed)
python amap2osm.py \
  --vertices "116.39,39.90 116.41,39.90 116.41,39.92 116.39,39.92" \
  --out out/ --osm-format osm

# force-disable GCJ-02 decryption (e.g. data already in WGS-84)
python amap2osm.py --boundary area.shp --decrypt no
```

### Notable flags

| Flag            | Default      | Meaning                                    |
|-----------------|--------------|--------------------------------------------|
| `--zoom`        | `16`         | Tile zoom; 16 is where Amap serves footprints. |
| `--tile-url`    | (built-in)   | URL template with `{z}/{x}/{y}` placeholders. |
| `--decrypt`     | `auto`       | `auto` toggles on if the boundary's centroid lies in China. |
| `--osm-format`  | `both`       | `osm`, `osc`, `both`, or `none`.            |
| `--workers`     | `8`          | Concurrent tile fetchers.                   |

## Notes

* Amap's building endpoint is not officially published; the default URL
  template targets the de-facto public endpoint. If Amap changes it,
  override with `--tile-url`.
* GCJ-02 → WGS-84 uses the well-known reverse offset; it is accurate to
  within a few metres, sufficient for OSM-grade footprints.
* OSM output uses negative placeholder IDs so the file can be opened in
  JOSM and uploaded as new objects.
