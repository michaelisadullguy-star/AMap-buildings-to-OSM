#!/usr/bin/env python3
"""Empirically discover & characterise Amap building / vector-tile endpoints.

We assume NOTHING about the format. Two phases:

  1. DISCOVERY (no key needed): fetch amap.com + its JS bundles and grep for
     live data-tile hosts/paths the WebGL map actually uses. This reveals the
     *current* building endpoint instead of relying on stale guesses.

  2. CAPTURE: hit a battery of candidate building/vector endpoints (with the
     JSAPI key + jscode/sig auth variants when provided), saving raw bodies to
     probe_out/ and printing a format sniff for each, so a decoder can be
     written against real bytes.

Env:
  AMAP_JSKEY / AMAP_KEY  - Amap key (JSAPI 'Web端' key preferred for tiles)
  AMAP_JSCODE            - security 安全密钥 (jscode), if the key requires it
  AMAP_SECRET            - private key for digital-signature (sig) auth
"""
from __future__ import annotations
import os
import re
import sys
import json
import hashlib
import pathlib

import requests
import mercantile

KEY    = os.environ.get("AMAP_JSKEY") or os.environ.get("AMAP_KEY") or ""
JSCODE = os.environ.get("AMAP_JSCODE", "")
SECRET = os.environ.get("AMAP_SECRET", "")
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
H = {"User-Agent": UA, "Referer": "https://www.amap.com/"}

OUT = pathlib.Path("probe_out")
OUT.mkdir(exist_ok=True)

# A point inside the Nanjing test area (幕府山 / 宝塔桥).
LON, LAT = 118.778, 32.110


# ---------------------------------------------------------------------------
def sniff(b: bytes) -> str:
    if not b:
        return "empty"
    if b[:2] == b"\x1f\x8b":
        return "gzip"
    if b[:4] == b"\x89PNG":
        return "png"
    if b[:3] == b"\xff\xd8\xff":
        return "jpeg"
    lead = b.lstrip()[:1]
    if lead in (b"{", b"["):
        return "json"
    if lead == b"<":
        return "html/xml"
    wt, fn = b[0] & 0x07, b[0] >> 3
    printable = sum(32 <= c < 127 for c in b[:64])
    if fn >= 1 and wt in (0, 1, 2, 5) and printable < 40:
        return "protobuf?/binary"
    ratio = sum(32 <= c < 127 or c in (9, 10, 13) for c in b[:200]) / len(b[:200])
    return "text" if ratio > 0.9 else "binary"


def hexdump(b: bytes, n: int = 48) -> str:
    chunk = b[:n]
    hx = " ".join(f"{c:02x}" for c in chunk)
    asc = "".join(chr(c) if 32 <= c < 127 else "." for c in chunk)
    return f"{hx}\n      |{asc}|"


# ---------------------------------------------------------------------------
HOST_PAT = re.compile(r"(?:https?:)?//[\w.\-]*(?:amap|autonavi|gaode)[\w.\-]*"
                      r"(?:/[\w./\-]*)?", re.I)
KEYWORD_PAT = re.compile(r"tile|vdata|vmap|building|楼块|mesh|gltf|frontend|"
                         r"datatile|webgl|sceneTile|abroad|basemap", re.I)


def _scan_js(session: requests.Session, urls: list[str], found: set[str],
             depth: int = 0) -> None:
    """Fetch JS bundles, harvest amap endpoint patterns, recurse one level
    into referenced .js modules."""
    for u in urls[:16]:
        try:
            j = session.get(u, timeout=25, headers=H)
        except requests.RequestException:
            continue
        if j.status_code != 200:
            continue
        text = j.text
        for m in HOST_PAT.findall(text):
            if KEYWORD_PAT.search(m):
                found.add(m)
        if depth == 0:
            # follow referenced sub-bundles once (JSAPI loader -> modules)
            refs = []
            for s in re.findall(r'["\'](https?:[^"\']+\.js[^"\']*)["\']', text):
                if "amap" in s or "autonavi" in s:
                    refs.append(s)
            if refs:
                _scan_js(session, list(dict.fromkeys(refs)), found, depth + 1)


def discover(session: requests.Session) -> None:
    print("=" * 70)
    print("PHASE 1 — DISCOVERY (scrape live tile endpoints)")
    print("=" * 70)
    found: set[str] = set()

    # 1) The keyed JSAPI loader bundles carry the real runtime data endpoints.
    if KEY:
        loaders = [
            f"https://webapi.amap.com/maps?v=2.0&key={KEY}",
            f"https://webapi.amap.com/maps?v=1.4.15&key={KEY}",
            f"https://webapi.amap.com/loca?v=2.0.0&key={KEY}",
        ]
        print(f"scanning {len(loaders)} keyed JSAPI loaders + their modules...")
        _scan_js(session, loaders, found)
    else:
        print("(no key -> skipping keyed JSAPI discovery; this is where the "
              "real endpoint normally surfaces)")

    # 2) Best-effort static scrape of the SPA homepage.
    try:
        r = session.get("https://www.amap.com/", timeout=25, headers=H)
        print(f"homepage: {r.status_code}, {len(r.content)} bytes")
        srcs = []
        for s in re.findall(r'src=["\']([^"\']+\.js[^"\']*)["\']', r.text):
            if s.startswith("//"):
                s = "https:" + s
            elif s.startswith("/"):
                s = "https://www.amap.com" + s
            elif not s.startswith("http"):
                continue
            srcs.append(s)
        _scan_js(session, list(dict.fromkeys(srcs)), found)
    except requests.RequestException as e:
        print("homepage fetch failed:", e)

    for m in sorted(found):
        print("  endpoint-hit:", m[:160])
    if not found:
        print("  (no tile/building endpoint patterns found)")


# ---------------------------------------------------------------------------
def auth_variants(base_url: str, params: dict) -> list[tuple[str, dict]]:
    """Return labelled (url, params) auth variants to try."""
    out = [("key", {**params, **({"key": KEY} if KEY else {})})]
    if KEY and JSCODE:
        out.append(("key+jscode", {**params, "key": KEY, "jscode": JSCODE}))
    if KEY and SECRET:
        p = {**params, "key": KEY}
        sig_src = "&".join(f"{k}={p[k]}" for k in sorted(p)) + SECRET
        sig = hashlib.md5(sig_src.encode()).hexdigest()
        out.append(("key+sig", {**p, "sig": sig}))
    return out


def capture(session: requests.Session) -> None:
    print("\n" + "=" * 70)
    print("PHASE 2 — CAPTURE (probe candidate building/vector endpoints)")
    print(f"key set: {bool(KEY)}   jscode: {bool(JSCODE)}   secret: {bool(SECRET)}")
    print("=" * 70)

    manifest = []
    for z in (15, 16, 17, 18):
        t = mercantile.tile(LON, LAT, z)
        x, y = t.x, t.y
        candidates: list[tuple[str, dict]] = []
        # vdata data-tile, layer selector t = 0..9 (building layer is one of these)
        for tt in range(0, 10):
            candidates.append((f"vdata-tile-t{tt}-z{z}",
                               ("https://vdata.amap.com/tile",
                                {"x": x, "y": y, "z": z, "t": tt})))
        candidates += [
            (f"vdata-datatile-z{z}",
             ("https://vdata.amap.com/datatile", {"x": x, "y": y, "z": z})),
            (f"vdata-v3-z{z}",
             ("https://vdata.amap.com/v3/tile", {"x": x, "y": y, "z": z})),
        ]
        for label, (url, params) in candidates:
            for auth_label, p in auth_variants(url, params):
                try:
                    r = session.get(url, params=p, timeout=30, headers=H)
                    body = r.content
                    kind = sniff(body)
                    print(f"\n[{label} | {auth_label}] {r.status_code} "
                          f"ct={r.headers.get('content-type','?')} "
                          f"size={len(body)} sniff={kind}")
                    print("      " + hexdump(body))
                    fn = OUT / f"{label}_{auth_label}.bin"
                    fn.write_bytes(body[:2_000_000])
                    manifest.append({
                        "label": label, "auth": auth_label, "url": r.url,
                        "status": r.status_code,
                        "content_type": r.headers.get("content-type"),
                        "size": len(body), "sniff": kind,
                        "sha256": hashlib.sha256(body).hexdigest()[:16],
                        "file": fn.name,
                    })
                except requests.RequestException as e:
                    print(f"[{label} | {auth_label}] ERROR {e}")
        # only sweep the full t-range at one zoom to keep the probe quick
        if z == 16:
            continue
    (OUT / "manifest.json").write_text(json.dumps(manifest, indent=2))
    print(f"\nwrote {len(manifest)} captures + manifest to {OUT}/")
    # Highlight anything that looks like real data (not error JSON / html).
    print("\n--- promising captures (binary/protobuf/gzip, non-trivial size) ---")
    for m in manifest:
        if m["sniff"] in ("protobuf?/binary", "binary", "gzip") and m["size"] > 64:
            print("  ", m["label"], m["auth"], m["sniff"], m["size"], m["url"][:120])


# ---------------------------------------------------------------------------
def main() -> None:
    with requests.Session() as s:
        discover(s)
        capture(s)


if __name__ == "__main__":
    main()
