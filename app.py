import streamlit as st
import streamlit.components.v1 as components

# ---- Page settings ----
st.set_page_config(
    page_title="DXF → KML Converter",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ---- Your embed URL (FORCE LIGHT THEME) ----
SPACE_URL = "https://sulavlohani-cad-to-kml.hf.space/?__theme=light"

# ---- Light website styling (your site look) ----
st.markdown(
    """
    <style>
      /* Hide Streamlit default UI */
      #MainMenu {visibility: hidden;}
      header {visibility: hidden;}
      footer {visibility: hidden;}

      /* Page background */
      .stApp {
        background: #f8fafc;
      }

      /* Wider content */
      .block-container {
        padding-top: 1.2rem;
        padding-bottom: 2rem;
        max-width: 1300px;
      }

      /* Card container */
      .tool-card {
        background: #ffffff;
        border: 1px solid #e5e7eb;
        border-radius: 18px;
        box-shadow: 0 12px 28px rgba(15,23,42,0.08);
        padding: 18px 18px 12px;
      }

      .tool-title {
        font-family: system-ui, -apple-system, Segoe UI, Roboto, Arial, sans-serif;
        font-weight: 700;
        font-size: 18px;
        margin: 0 0 6px 0;
        color: #0f172a;
      }

      .tool-subtitle {
        font-family: system-ui, -apple-system, Segoe UI, Roboto, Arial, sans-serif;
        font-size: 13px;
        margin: 0 0 14px 0;
        color: #64748b;
      }

      .tool-frame {
        width: 100%;
        height: 920px;   /* Adjust if you want more/less height */
        border: 0;
        border-radius: 14px;
        background: #fff;
      }
    </style>
    """,
    unsafe_allow_html=True,
)

# ---- Render as a nice light card with embedded tool ----
components.html(
    f"""
    <div class="tool-card">
      <div class="tool-title">DXF → KML Converter</div>
      <div class="tool-subtitle">Upload DXF, select CRS, export KML (EPSG:4326).</div>
      <iframe class="tool-frame" src="{SPACE_URL}" loading="lazy"></iframe>
    </div>
    """,
    height=980,  # must be slightly bigger than iframe height
)
