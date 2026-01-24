import json
import math
import html as html_lib
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import gradio as gr
import ezdxf
from pyproj import CRS, Transformer
import simplekml

from ezdxf.addons.geo import proxy


# ----------------------------
# Settings
# ----------------------------
DEFAULT_FLATTENING_DISTANCE = 0.5  # internal default (no UI)
TARGET_VALUE = "EPSG:4326"
TARGET_CRS = CRS.from_epsg(4326)


# ----------------------------
# CRS dropdown options
# ----------------------------
NEPAL_TOWGS84 = "293.17,726.18,245.36,0,0,0,0"
MUTM_K0 = 0.9999
MUTM_FE = 500000
MUTM_FN = 0

def nepal_mutm_proj(lon0: int) -> str:
    return (
        f"+proj=tmerc +lat_0=0 +lon_0={lon0} +k_0={MUTM_K0} "
        f"+x_0={MUTM_FE} +y_0={MUTM_FN} "
        f"+ellps=evrst30 +towgs84={NEPAL_TOWGS84} +units=m +no_defs"
    )

def build_source_choices() -> List[Tuple[str, str]]:
    choices: List[Tuple[str, str]] = []
    choices.append(("Auto-detect from DXF (GEODATA / PRJ)", "__AUTO__"))
    choices.append(("WGS84 (Lat/Long) — EPSG:4326", "EPSG:4326"))
    choices.append(("Web Mercator — EPSG:3857", "EPSG:3857"))
    choices.append(("World Mercator — EPSG:3395", "EPSG:3395"))

    # Nepal presets (optional)
    choices.append(("Nepal 1981 (Everest 1830) — EPSG:6207", "EPSG:6207"))
    choices.append(("Nepal MUTM 81 (Everest 1830) — CM 81°E", nepal_mutm_proj(81)))
    choices.append(("Nepal MUTM 84 (Everest 1830) — CM 84°E", nepal_mutm_proj(84)))
    choices.append(("Nepal MUTM 87 (Everest 1830) — CM 87°E", nepal_mutm_proj(87)))

    # UTM WGS84 zones
    for z in range(1, 61):
        zz = f"{z:02d}"
        choices.append((f"UTM Zone {zz}N (WGS84) — EPSG:{32600+z}", f"EPSG:{32600+z}"))
    for z in range(1, 61):
        zz = f"{z:02d}"
        choices.append((f"UTM Zone {zz}S (WGS84) — EPSG:{32700+z}", f"EPSG:{32700+z}"))

    choices.append(("Custom (EPSG / PROJ / WKT)", "__CUSTOM__"))
    return choices

SOURCE_CHOICES = build_source_choices()
SOURCE_ALLOWED_VALUES = {v for _, v in SOURCE_CHOICES}
TARGET_CHOICES = [("WGS84 for KML — EPSG:4326", "EPSG:4326")]


# ----------------------------
# Force LIGHT mode (works great for your website + HF)
# ----------------------------
FORCE_LIGHT_JS = """
() => {
  try {
    const url = new URL(window.location.href);
    if (url.searchParams.get("__theme") !== "light") {
      url.searchParams.set("__theme", "light");
      window.location.replace(url.toString());
    }
  } catch (e) {}
}
"""


# ----------------------------
# Auto-detect Source CRS (best-effort)
# ----------------------------
def detect_source_crs(dxf_path: str) -> Tuple[Optional[str], str]:
    p = Path(dxf_path)

    # 1) Sidecar PRJ
    prj = p.with_suffix(".prj")
    if prj.exists():
        try:
            txt = prj.read_text(encoding="utf-8", errors="ignore").strip()
            if txt:
                crs = CRS.from_user_input(txt)
                auth = crs.to_authority()
                if auth and auth[0].upper() == "EPSG":
                    epsg_val = f"EPSG:{auth[1]}"
                    return epsg_val, f"Detected from sidecar PRJ: {epsg_val}"
                return txt, "Detected from sidecar PRJ (usable as Custom)."
        except Exception:
            pass

    # 2) DXF GEODATA
    try:
        doc = ezdxf.readfile(str(p))
        geodata = doc.modelspace().get_geodata()
        if geodata is None:
            return None, "Auto-detect: No GEODATA found in DXF (no embedded CRS)."

        # Try EPSG extraction
        try:
            res = geodata.get_crs()
            epsg = res[0] if isinstance(res, tuple) and len(res) else res
            if epsg:
                epsg_str = str(epsg)
                if not epsg_str.upper().startswith("EPSG:"):
                    epsg_str = f"EPSG:{epsg_str}"
                return epsg_str, f"Detected from DXF GEODATA: {epsg_str}"
        except Exception:
            pass

        cs_def = getattr(geodata, "coordinate_system_definition", None)
        if cs_def and str(cs_def).strip():
            return str(cs_def).strip(), "GEODATA found. Using CRS definition as Custom."
        return None, "Auto-detect: GEODATA present but CRS could not be read."

    except Exception as e:
        return None, f"Auto-detect failed: {e}"


# ----------------------------
# Geometry utilities
# ----------------------------
def _xy(pt: Any) -> Tuple[float, float]:
    if not isinstance(pt, (list, tuple)) or len(pt) < 2:
        raise ValueError("Invalid coordinate point.")
    return float(pt[0]), float(pt[1])

def explode_to_geometries(mapping: Dict[str, Any]) -> List[Dict[str, Any]]:
    if not isinstance(mapping, dict):
        return []
    t = mapping.get("type")

    if t == "Feature":
        geom = mapping.get("geometry")
        return explode_to_geometries(geom) if isinstance(geom, dict) else []

    if t == "FeatureCollection":
        out = []
        for f in (mapping.get("features") or []):
            out.extend(explode_to_geometries(f))
        return out

    if t == "GeometryCollection":
        out = []
        for g in (mapping.get("geometries") or []):
            out.extend(explode_to_geometries(g))
        return out

    if t in ("Point", "LineString", "Polygon"):
        return [mapping]

    coords = mapping.get("coordinates") or []
    if t == "MultiPoint":
        return [{"type": "Point", "coordinates": c} for c in coords]
    if t == "MultiLineString":
        return [{"type": "LineString", "coordinates": c} for c in coords]
    if t == "MultiPolygon":
        return [{"type": "Polygon", "coordinates": c} for c in coords]

    return []

def transform_geometry(geom: Dict[str, Any], tf: Transformer) -> Dict[str, Any]:
    t = geom.get("type")
    c = geom.get("coordinates")
    if c is None:
        raise ValueError("Geometry has no coordinates.")

    def tx_point(p):
        x, y = _xy(p)
        lon, lat = tf.transform(x, y)
        return [round(float(lon), 6), round(float(lat), 6)]

    if t == "Point":
        return {"type": "Point", "coordinates": tx_point(c)}
    if t == "LineString":
        return {"type": "LineString", "coordinates": [tx_point(p) for p in (c or [])]}
    if t == "Polygon":
        rings = c or []
        return {"type": "Polygon", "coordinates": [[tx_point(p) for p in (ring or [])] for ring in rings]}

    raise ValueError(f"Unsupported geometry type: {t}")

def iter_lonlat(geom_ll: Dict[str, Any]):
    t = geom_ll["type"]
    c = geom_ll["coordinates"]

    if t == "Point":
        yield c[0], c[1]
        return

    def walk(obj):
        if isinstance(obj, list):
            if len(obj) == 2 and all(isinstance(v, (int, float)) for v in obj):
                yield obj[0], obj[1]
            else:
                for item in obj:
                    yield from walk(item)

    yield from walk(c)


# ----------------------------
# Expand blocks (INSERT) so more DXFs work
# ----------------------------
def iter_entities_with_blocks(msp):
    for ent in msp:
        if ent.dxftype() == "INSERT":
            try:
                for v in ent.virtual_entities():
                    yield v
            except Exception:
                continue
        else:
            yield ent


# ----------------------------
# KML + Map Preview
# ----------------------------
def ensure_ring_closed(ring: List[List[float]]) -> List[List[float]]:
    if ring and ring[0] != ring[-1]:
        ring = ring + [ring[0]]
    return ring

def _valid_pair(p) -> bool:
    try:
        lon = float(p[0]); lat = float(p[1])
        return math.isfinite(lon) and math.isfinite(lat)
    except Exception:
        return False

def write_kml(feature_collection: Dict[str, Any], name: str) -> str:
    kml = simplekml.Kml()
    kml.document.name = name

    folders = {}
    def folder_for(layer: str):
        if layer not in folders:
            folders[layer] = kml.newfolder(name=layer)
        return folders[layer]

    for feat in feature_collection.get("features", []):
        props = feat.get("properties", {}) or {}
        layer_name = props.get("layer", "Layer 0")
        ent_name = props.get("entity", "DXF Entity")
        geom = feat.get("geometry") or {}
        folder = folder_for(layer_name)

        t = geom.get("type")
        c = geom.get("coordinates")

        if t == "Point":
            if isinstance(c, list) and len(c) >= 2 and _valid_pair(c):
                folder.newpoint(name=ent_name, coords=[(c[0], c[1])])
            continue

        if t == "LineString":
            if not isinstance(c, list):
                continue
            coords = [(p[0], p[1]) for p in c if isinstance(p, list) and len(p) >= 2 and _valid_pair(p)]
            if len(coords) >= 2:
                folder.newlinestring(name=ent_name, coords=coords)
            continue

        if t == "Polygon":
            if not isinstance(c, list) or not c:
                continue

            outer_raw = c[0] or []
            outer = ensure_ring_closed([p for p in outer_raw if isinstance(p, list) and len(p) >= 2 and _valid_pair(p)])
            outer_coords = [(p[0], p[1]) for p in outer]
            if len(outer_coords) < 4:
                continue

            holes_coords = []
            for hole_raw in c[1:]:
                hole_raw = hole_raw or []
                hole = ensure_ring_closed([p for p in hole_raw if isinstance(p, list) and len(p) >= 2 and _valid_pair(p)])
                hole_xy = [(p[0], p[1]) for p in hole]
                if len(hole_xy) >= 4:
                    holes_coords.append(hole_xy)

            if holes_coords:
                folder.newpolygon(name=ent_name, outerboundaryis=outer_coords, innerboundaryis=holes_coords)
            else:
                folder.newpolygon(name=ent_name, outerboundaryis=outer_coords)

        # Make filename safe for filesystem
        safe_name = "".join(c if c.isalnum() or c in ("-", "_", ".") else "_" for c in str(name))
        
        # Create a unique temp folder (prevents collisions across users)
        out_dir = Path(tempfile.mkdtemp(prefix="kml_"))
        
        # Required naming format:
        # input_file_name_Reprojected_ToolsForEngineers.com.kml
        out_path = out_dir / f"{safe_name}_Reprojected_ToolsForEngineers.com.kml"
        
        kml.save(str(out_path))
        return str(out_path)


def leaflet_iframe(feature_collection: Dict[str, Any]) -> str:
    geojson_str = json.dumps(feature_collection).replace("</", "<\\/")

    inner = f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/leaflet@1.9.4/dist/leaflet.css">
  <script src="https://cdn.jsdelivr.net/npm/leaflet@1.9.4/dist/leaflet.js"></script>
  <style>
    html, body {{ height: 100%; margin: 0; }}
    #map {{ height: 100%; width: 100%; background: #eef2f7; }}
    .leaflet-control-layers {{ border-radius: 12px; box-shadow: 0 10px 22px rgba(15,23,42,0.12); }}
  </style>
</head>
<body>
<div id="map"></div>
<script>
  const geojson = {geojson_str};

  const map = L.map("map");
  const baseLayers = {{
    "OpenStreetMap": L.tileLayer("https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png", {{
      maxZoom: 20, attribution: "© OpenStreetMap contributors"
    }}),
    "CARTO Positron": L.tileLayer("https://{{s}}.basemaps.cartocdn.com/light_all/{{z}}/{{x}}/{{y}}{{r}}.png", {{
      maxZoom: 20, attribution: "© OpenStreetMap contributors © CARTO"
    }}),
    "Esri World Imagery": L.tileLayer(
      "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{{z}}/{{y}}/{{x}}",
      {{ maxZoom: 20, attribution: "Tiles © Esri" }}
    )
  }};

  baseLayers["OpenStreetMap"].addTo(map);

  const overlay = L.geoJSON(geojson, {{
    style: () => ({{ weight: 2, fillOpacity: 0.15 }}),
    pointToLayer: (f, latlng) => L.circleMarker(latlng, {{ radius: 5, weight: 2, fillOpacity: 0.9 }})
  }}).addTo(map);

  L.control.layers(baseLayers, {{"Converted DXF → KML (EPSG:4326)": overlay}}, {{collapsed: true}}).addTo(map);

  if (overlay.getLayers().length > 0) {{
    map.fitBounds(overlay.getBounds().pad(0.25));  // zoom out a bit
  }} else {{
    map.setView([0,0], 2);
  }}
</script>
</body>
</html>
"""
    srcdoc = html_lib.escape(inner, quote=True)
    return f"""
<iframe
  style="width:100%; height:620px; border:1px solid rgba(148,163,184,0.35); border-radius:14px; background:#fff;"
  srcdoc="{srcdoc}">
</iframe>
"""

INTRO_MAP = """
<div style="padding:14px;border:1px dashed rgba(148,163,184,0.65);border-radius:14px;background:#fff;">
  <div style="font:600 14px/1.35 system-ui,-apple-system,Segoe UI,Roboto,Arial;color:#0f172a;margin-bottom:6px;">
    Map Preview
  </div>
  <div style="font:13px/1.5 system-ui,-apple-system,Segoe UI,Roboto,Arial;color:#475569;">
    Upload a DXF, confirm the <b>Source CRS</b>, then click <b>Reproject &amp; Convert</b>.
  </div>
</div>
"""


# ----------------------------
# UI handlers
# ----------------------------
def on_upload(dxf_path: Optional[str]):
    if not dxf_path:
        return (
            gr.update(value="__AUTO__"),
            gr.update(value="**Detected Source CRS:** —"),
            gr.update(visible=False, value=""),
        )

    p = Path(dxf_path)
    if p.suffix.lower() != ".dxf":
        return (
            gr.update(value="__AUTO__"),
            gr.update(value="**Detected Source CRS:** Please upload a DXF."),
            gr.update(visible=False, value=""),
        )

    detected, msg = detect_source_crs(dxf_path)

    if detected and detected in SOURCE_ALLOWED_VALUES:
        return (
            gr.update(value=detected),
            gr.update(value=f"**Detected Source CRS:** {msg}"),
            gr.update(visible=False, value=""),
        )

    if detected and detected not in SOURCE_ALLOWED_VALUES:
        return (
            gr.update(value="__AUTO__"),
            gr.update(value=f"**Detected Source CRS:** {msg}"),
            gr.update(visible=False, value=detected),
        )

    return (
        gr.update(value="__AUTO__"),
        gr.update(value=f"**Detected Source CRS:** {msg}"),
        gr.update(visible=False, value=""),
    )

def toggle_custom_visibility(source_value: str, custom_value: str):
    return gr.update(visible=(source_value == "__CUSTOM__"), value=custom_value)

def convert_dxf_to_kml(dxf_path: str, source_value: str, custom_crs: str, detected_status: str):
    try:
        if not dxf_path:
            raise ValueError("Please upload a DXF file.")

        p = Path(dxf_path)
        if p.suffix.lower() != ".dxf":
            raise ValueError("Only DXF is supported.")

        # Resolve source CRS input
        used_auto_msg = ""
        if source_value == "__AUTO__":
            detected, msg = detect_source_crs(dxf_path)
            used_auto_msg = msg
            if not detected:
                raise ValueError("Auto-detect could not determine Source CRS. Please select it manually.")
            source_input = detected
        elif source_value == "__CUSTOM__":
            if not (custom_crs or "").strip():
                raise ValueError("Custom CRS selected but empty.")
            source_input = custom_crs.strip()
        else:
            source_input = source_value

        src_crs = CRS.from_user_input(source_input)
        tf = Transformer.from_crs(src_crs, TARGET_CRS, always_xy=True)

        doc = ezdxf.readfile(str(p))
        msp = doc.modelspace()

        features = []
        skipped = 0

        min_lon = math.inf
        min_lat = math.inf
        max_lon = -math.inf
        max_lat = -math.inf

        for ent in iter_entities_with_blocks(msp):
            try:
                # proxy signature varies slightly by version; support both
                try:
                    gp = proxy(ent, distance=float(DEFAULT_FLATTENING_DISTANCE))
                except TypeError:
                    gp = proxy(ent, float(DEFAULT_FLATTENING_DISTANCE))

                mapping = getattr(gp, "__geo_interface__", None)
                if not isinstance(mapping, dict):
                    skipped += 1
                    continue

                geoms = explode_to_geometries(mapping)
                if not geoms:
                    skipped += 1
                    continue

                for g in geoms:
                    if g.get("type") not in ("Point", "LineString", "Polygon"):
                        continue

                    g_ll = transform_geometry(g, tf)

                    for lon, lat in iter_lonlat(g_ll):
                        if math.isfinite(lon) and math.isfinite(lat):
                            min_lon = min(min_lon, lon)
                            min_lat = min(min_lat, lat)
                            max_lon = max(max_lon, lon)
                            max_lat = max(max_lat, lat)

                    features.append({
                        "type": "Feature",
                        "properties": {
                            "layer": getattr(ent.dxf, "layer", "Layer 0"),
                            "entity": ent.dxftype(),
                        },
                        "geometry": g_ll,
                    })

            except Exception:
                skipped += 1

        if not features:
            raise ValueError("No convertible geometries found. Try exporting DXF with lines/polylines.")

        fc = {"type": "FeatureCollection", "features": features}
        kml_path = write_kml(fc, p.stem)
        map_html = leaflet_iframe(fc)

        warning = ""
        if (
            math.isfinite(min_lon) and math.isfinite(max_lon)
            and (min_lon < -180 or max_lon > 180 or min_lat < -90 or max_lat > 90)
        ):
            warning = "\n\n⚠️ Output bounds look unusual for EPSG:4326. Check Source CRS."

        report = (
            f"### Conversion Report\n"
            f"- **Input:** `{p.name}`\n"
            f"- **Source CRS used:** `{source_input}`\n"
            f"- **Target CRS (KML):** `EPSG:4326 (WGS84 lon/lat)`\n"
            f"- **Features exported:** `{len(features)}`\n"
            f"- **Entities skipped:** `{skipped}`\n"
            f"- **Bounds:** lon `{min_lon:.6f} .. {max_lon:.6f}`, lat `{min_lat:.6f} .. {max_lat:.6f}`"
            f"{warning}"
        )

        status_out = detected_status
        if source_value == "__AUTO__":
            status_out = f"**Detected Source CRS:** {used_auto_msg}"

        return map_html, gr.update(value=kml_path, visible=True), report, status_out

    except Exception as e:
        return INTRO_MAP, gr.update(visible=False, value=None), f"### Error\n{e}", detected_status


# ----------------------------
# UI (Light theme + hide footer)
# ----------------------------
CSS = """
body, .gradio-container { background: #f8fafc !important; }
footer { display: none !important; }
"""

theme = gr.themes.Base(
    primary_hue="orange",
    secondary_hue="slate",
    neutral_hue="slate",
)

with gr.Blocks(css=CSS, theme=theme, js=FORCE_LIGHT_JS, title="DXF → KML Converter") as demo:
    gr.Markdown(
        "# DXF → KML Converter (with Reprojection)\n"
        "Upload a **DXF**, confirm the **Source CRS**, and export **KML in EPSG:4326 (WGS84 lon/lat)**.\n\n"
        "**Tip:** For Geographic CRS, coordinates should be **X=Longitude**, **Y=Latitude**."
    )

    with gr.Row():
        with gr.Column(scale=5):
            dxf_file = gr.File(label="Upload DXF", file_types=[".dxf"], type="filepath")
            detected_md = gr.Markdown("**Detected Source CRS:** —")

            source_crs = gr.Dropdown(
                label="Source Coordinate Reference System (CRS)",
                choices=SOURCE_CHOICES,
                value="__AUTO__",
            )

            custom_crs = gr.Textbox(
                label="Custom Source CRS (EPSG / PROJ / WKT)",
                placeholder="Example: EPSG:32645",
                visible=False,
            )

            target_crs = gr.Dropdown(
                label="Target CRS (KML Output)",
                choices=TARGET_CHOICES,
                value="EPSG:4326",
                interactive=False,
            )

            btn = gr.Button("Reproject & Convert", variant="primary")
            download = gr.DownloadButton("Download KML", visible=False)
            report = gr.Markdown()

        with gr.Column(scale=7):
            preview = gr.HTML(value=INTRO_MAP)

    dxf_file.change(fn=on_upload, inputs=[dxf_file], outputs=[source_crs, detected_md, custom_crs])
    source_crs.change(fn=toggle_custom_visibility, inputs=[source_crs, custom_crs], outputs=[custom_crs])

    btn.click(
        fn=convert_dxf_to_kml,
        inputs=[dxf_file, source_crs, custom_crs, detected_md],
        outputs=[preview, download, report, detected_md],
    )

demo.queue().launch()
