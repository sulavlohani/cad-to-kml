import json
import math
import html as html_lib
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Tuple

import gradio as gr
import ezdxf
from ezdxf.addons import geo
from pyproj import CRS, Transformer
import simplekml


TARGET_CRS = CRS.from_epsg(4326)  # KML is WGS84 lon/lat


# --------------------------
# GeoJSON helpers
# --------------------------
def explode_to_geometries(mapping: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Takes a GeoJSON-like mapping that could be:
    - Geometry (Point/LineString/Polygon/...)
    - Feature
    - GeometryCollection
    - Multi*
    Returns a list of geometry dicts (no Features).
    """
    t = mapping.get("type")

    if t == "Feature":
        return explode_to_geometries(mapping["geometry"])

    if t == "FeatureCollection":
        out = []
        for f in mapping.get("features", []):
            out.extend(explode_to_geometries(f))
        return out

    if t == "GeometryCollection":
        out = []
        for g in mapping.get("geometries", []):
            out.extend(explode_to_geometries(g))
        return out

    if t in ("Point", "LineString", "Polygon"):
        return [mapping]

    if t == "MultiPoint":
        return [{"type": "Point", "coordinates": c} for c in mapping["coordinates"]]

    if t == "MultiLineString":
        return [{"type": "LineString", "coordinates": c} for c in mapping["coordinates"]]

    if t == "MultiPolygon":
        return [{"type": "Polygon", "coordinates": c} for c in mapping["coordinates"]]

    # Unsupported / unexpected
    return []


def transform_geometry(geom: Dict[str, Any], tf: Transformer) -> Dict[str, Any]:
    """
    Transform a geometry dict from source CRS to EPSG:4326.
    """
    t = geom["type"]
    c = geom["coordinates"]

    def tx_point(xy):
        x, y = xy
        lon, lat = tf.transform(x, y)
        return [round(float(lon), 6), round(float(lat), 6)]

    if t == "Point":
        return {"type": "Point", "coordinates": tx_point(c)}

    if t == "LineString":
        return {"type": "LineString", "coordinates": [tx_point(p) for p in c]}

    if t == "Polygon":
        # Polygon coords = [outerRing, hole1, hole2...]
        return {"type": "Polygon", "coordinates": [[tx_point(p) for p in ring] for ring in c]}

    raise ValueError(f"Unsupported geometry type for transform: {t}")


def iter_lonlat(geom_ll: Dict[str, Any]):
    """
    Iterate lon/lat pairs from EPSG:4326 geometry.
    """
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


def leaflet_iframe(feature_collection: Dict[str, Any]) -> str:
    geojson_str = json.dumps(feature_collection).replace("</", "<\\/")  # safe in script

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
    .leaflet-control-layers {{ border-radius: 12px; }}
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

  L.control.layers(baseLayers, {{"Converted DXF (EPSG:4326)": overlay}}, {{collapsed: true}}).addTo(map);

  if (overlay.getLayers().length > 0) {{
    map.fitBounds(overlay.getBounds().pad(0.12));
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


def write_kml(feature_collection: Dict[str, Any], name: str) -> str:
    kml = simplekml.Kml()
    kml.document.name = name

    folders = {}

    def folder_for(layer: str):
        if layer not in folders:
            folders[layer] = kml.newfolder(name=layer)
        return folders[layer]

    def ensure_closed(ring: List[List[float]]) -> List[List[float]]:
        if ring and ring[0] != ring[-1]:
            ring = ring + [ring[0]]
        return ring

    for feat in feature_collection["features"]:
        props = feat.get("properties", {})
        layer_name = props.get("layer", "Layer 0")
        ent_name = props.get("entity", "DXF Entity")
        geom = feat["geometry"]
        f = folder_for(layer_name)

        t = geom["type"]
        c = geom["coordinates"]

        if t == "Point":
            lon, lat = c
            f.newpoint(name=ent_name, coords=[(lon, lat)])

        elif t == "LineString":
            coords = [(p[0], p[1]) for p in c]
            f.newlinestring(name=ent_name, coords=coords)

        elif t == "Polygon":
            rings = c
            outer = ensure_closed(rings[0])
            holes = [ensure_closed(r) for r in rings[1:]] if len(rings) > 1 else []
            poly = f.newpolygon(
                name=ent_name,
                outerboundaryis=[(p[0], p[1]) for p in outer],
                innerboundaryis=[[(p[0], p[1]) for p in r] for r in holes] if holes else None,
            )
        else:
            # ignore anything else for now
            continue

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".kml", prefix=f"{name}_")
    tmp.close()
    kml.save(tmp.name)
    return tmp.name


def convert_dxf_to_kml(dxf_path: str, source_crs_text: str, flatten_dist: float):
    if not dxf_path:
        raise gr.Error("Please upload a DXF file.")

    p = Path(dxf_path)
    if p.suffix.lower() != ".dxf":
        raise gr.Error("This starter supports DXF. If you have DWG, export/convert it to DXF first.")

    try:
        src_crs = CRS.from_user_input(source_crs_text.strip())
    except Exception as e:
        raise gr.Error(f"Invalid Source CRS: {e}")

    tf = Transformer.from_crs(src_crs, TARGET_CRS, always_xy=True)

    try:
        doc = ezdxf.readfile(str(p))
        msp = doc.modelspace()
    except Exception as e:
        raise gr.Error(f"Failed to read DXF: {e}")

    features = []
    skipped = 0

    min_lon = math.inf
    min_lat = math.inf
    max_lon = -math.inf
    max_lat = -math.inf

    # geo.gfilter filters only entities that geo.proxy can handle. :contentReference[oaicite:8]{index=8}
    for ent in geo.gfilter(msp):
        try:
            gp = geo.proxy(ent, distance=float(flatten_dist))  # curve flattening :contentReference[oaicite:9]{index=9}
            mapping = gp.__geo_interface__  # GeoJSON-like mapping :contentReference[oaicite:10]{index=10}

            geoms = explode_to_geometries(mapping)
            if not geoms:
                skipped += 1
                continue

            for g in geoms:
                if g["type"] not in ("Point", "LineString", "Polygon"):
                    continue

                g_ll = transform_geometry(g, tf)

                for lon, lat in iter_lonlat(g_ll):
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
        raise gr.Error("No convertible geometries found. Try another DXF or check it contains 2D entities.")

    fc = {"type": "FeatureCollection", "features": features}

    kml_path = write_kml(fc, p.stem)
    map_html = leaflet_iframe(fc)

    warning = ""
    if (min_lon < -180 or max_lon > 180 or min_lat < -90 or max_lat > 90):
        warning = "\n⚠️ Output bounds look unusual for EPSG:4326. Double-check Source CRS."

    report = (
        f"### Conversion Report\n"
        f"- **Input:** `{p.name}`\n"
        f"- **Source CRS:** `{source_crs_text}`\n"
        f"- **Target CRS:** `EPSG:4326 (WGS84)`\n"
        f"- **Features exported:** `{len(features)}`\n"
        f"- **Entities skipped:** `{skipped}`\n"
        f"- **Bounds:** lon `{min_lon:.6f} .. {max_lon:.6f}`, lat `{min_lat:.6f} .. {max_lat:.6f}`\n"
        f"{warning}"
    )

    return map_html, gr.update(value=kml_path, visible=True), report


# --------------------------
# UI
# --------------------------
with gr.Blocks(title="DXF → KML (EPSG:4326)") as demo:
    gr.Markdown(
        "# DXF → KML Converter (with Reprojection)\n"
        "Upload a **DXF**, enter the **Source CRS**, then export **KML in EPSG:4326**."
    )

    with gr.Row():
        with gr.Column(scale=5):
            dxf_file = gr.File(label="Upload DXF", file_types=[".dxf"], type="filepath")

            source_crs = gr.Textbox(
                label="Source Coordinate Reference System (CRS)",
                value="EPSG:4326",
                placeholder="Example: EPSG:32645",
            )

            target_crs = gr.Textbox(
                label="Target CRS (KML Output)",
                value="EPSG:4326 (WGS84 lon/lat)",
                interactive=False,
            )

            flatten_dist = gr.Slider(
                0.01, 10.0, value=0.5, step=0.01,
                label="Curve flattening distance (drawing units)"
            )

            btn = gr.Button("Reproject & Convert", variant="primary")
            download = gr.DownloadButton("Download KML", visible=False)

            report = gr.Markdown()

        with gr.Column(scale=7):
            preview = gr.HTML(
                '<div style="padding:14px;border:1px dashed #cbd5e1;border-radius:14px;">'
                'Map preview will appear here after conversion.'
                "</div>"
            )

    btn.click(
        fn=convert_dxf_to_kml,
        inputs=[dxf_file, source_crs, flatten_dist],
        outputs=[preview, download, report],
    )

demo.launch()
