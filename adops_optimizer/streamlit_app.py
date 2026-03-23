"""
Campaign Optimization Tool — Streamlit Cloud UI.
Supports both Performance and Scale optimization modes.
"""

import streamlit as st
from optimizer import run_optimization, run_scale_optimization, xlsx_to_csv, col_letter_to_idx

st.set_page_config(page_title="Campaign Optimization Tool", page_icon="📊", layout="centered")

# --- Header ---
st.markdown(
    "<h1 style='color:#1F3864;margin-bottom:0;'>Campaign Optimization Tool</h1>"
    "<p style='color:#666;margin-top:0;'>Digital Turbine Preload Campaign Optimizer</p>",
    unsafe_allow_html=True,
)
st.divider()

# --- Mode selector ---
mode = st.radio(
    "Optimization Mode",
    ["Performance Optimization", "Scale Optimization"],
    horizontal=True,
)
is_scale = mode == "Scale Optimization"

if is_scale:
    st.info(
        "**Scale Optimization** — Only the internal file is required. "
        "Bids are increased based on FillRate bands. "
        "Sites with spend < $100, maxPreloads < 100, or an existing dailyCap are excluded.",
        icon="ℹ️",
    )

# --- File uploads ---
st.subheader("Input Files")
internal_file = st.file_uploader(
    "Internal Campaign Data (.xlsx)",
    type=["xlsx"],
    key="internal",
)

advertiser_label = "Advertiser Performance Report (.csv) — optional" if is_scale else "Advertiser Performance Report (.csv)"
advertiser_file = st.file_uploader(
    advertiser_label,
    type=["csv"],
    key="advertiser",
)

# --- KPI settings (Performance only) ---
if not is_scale:
    st.subheader("KPI Settings")
    col1, col2 = st.columns(2)
    with col1:
        d7_col = st.text_input("ROI D7 Column Letter", value="I", max_chars=3)
        kpi_d7 = st.number_input("D7 KPI Target (%)", min_value=0.0, max_value=100.0, value=3.36, step=0.01, format="%.2f")
    with col2:
        d2nd_col = st.text_input("ROI D2nd Column Letter (D14 or D30)", value="K", max_chars=3)
        kpi_d2nd = st.number_input("D2nd KPI Target (%)", min_value=0.0, max_value=100.0, value=13.36, step=0.01, format="%.2f")

# --- Run button ---
st.divider()
run_clicked = st.button("Run Optimization", type="primary", use_container_width=True)

if run_clicked:
    # Validation
    if not internal_file:
        st.error("Please upload the Internal Campaign Data (.xlsx) file.")
        st.stop()

    if not is_scale:
        if not advertiser_file:
            st.error("Please upload the Advertiser Performance Report (.csv) file.")
            st.stop()
        d7_val = d7_col.strip().upper()
        d2nd_val = d2nd_col.strip().upper()
        if not d7_val or not d7_val.isalpha():
            st.error("ROI D7 Column Letter must be letters A–Z.")
            st.stop()
        if not d2nd_val or not d2nd_val.isalpha():
            st.error("ROI D2nd Column Letter must be letters A–Z.")
            st.stop()
        if kpi_d7 <= 0 or kpi_d2nd <= 0:
            st.error("KPI targets must be greater than 0.")
            st.stop()

    # Save uploads to temp files
    import tempfile, os
    internal_path = None
    advertiser_path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx") as f:
            f.write(internal_file.getvalue())
            internal_path = f.name

        if advertiser_file:
            with tempfile.NamedTemporaryFile(delete=False, suffix=".csv") as f:
                f.write(advertiser_file.getvalue())
                advertiser_path = f.name

        with st.spinner("Running optimization..."):
            if is_scale:
                buf, summary = run_scale_optimization(
                    internal_file=internal_path,
                    advertiser_file=advertiser_path,
                )
            else:
                kpi_col_d7_idx = col_letter_to_idx(d7_val)
                kpi_col_d2nd_idx = col_letter_to_idx(d2nd_val)
                buf, summary = run_optimization(
                    internal_file=internal_path,
                    advertiser_file=advertiser_path,
                    kpi_col_d7_idx=kpi_col_d7_idx,
                    kpi_col_d2nd_idx=kpi_col_d2nd_idx,
                    kpi_d7_pct=kpi_d7,
                    kpi_d2nd_pct=kpi_d2nd,
                )

        # Results
        st.success(f"{'Scale' if is_scale else 'Performance'} Optimization complete!")

        # Metric cards
        cols = st.columns(4 if is_scale else 5)
        cols[0].metric("Total Sites", summary.get("total_rows", 0))
        cols[1].metric("Sites Actioned", summary.get("rows_actioned", 0))
        cols[2].metric("Sites Disregarded", summary.get("rows_disregarded", 0))
        if not is_scale:
            cols[3].metric("Daily Cap Suggestions", summary.get("rows_with_cap", 0))
            cols[4].metric("KPI Column Used", summary.get("kpi_d2nd_col", "–"))
        else:
            cols[3].metric("FillRate Sorted", "High → Low")

        # Action breakdown
        action_breakdown = summary.get("action_breakdown") or {}
        if action_breakdown:
            st.subheader("Action Breakdown")
            import pandas as pd
            action_df = pd.DataFrame(
                sorted(action_breakdown.items(), key=lambda x: -x[1]),
                columns=["Action", "Count"],
            )
            st.dataframe(action_df, use_container_width=True, hide_index=True)

        # Segment breakdown (Performance only)
        segment_breakdown = summary.get("segment_breakdown") or {}
        if segment_breakdown:
            st.subheader("Segment Breakdown")
            seg_df = pd.DataFrame(
                sorted(segment_breakdown.items(), key=lambda x: -x[1]),
                columns=["Segment", "Count"],
            )
            st.dataframe(seg_df, use_container_width=True, hide_index=True)

        # Download buttons
        st.divider()
        dl_col1, dl_col2 = st.columns(2)
        with dl_col1:
            csv_buf = xlsx_to_csv(buf)
            st.download_button(
                label="Download CSV",
                data=csv_buf.getvalue(),
                file_name="optimization_output.csv",
                mime="text/csv",
                type="primary",
                use_container_width=True,
            )
        with dl_col2:
            buf.seek(0)
            st.download_button(
                label="Download Excel",
                data=buf.getvalue(),
                file_name="optimization_output.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True,
            )

    except Exception as e:
        st.error(f"Optimization failed: {e}")
    finally:
        if internal_path and os.path.isfile(internal_path):
            os.unlink(internal_path)
        if advertiser_path and os.path.isfile(advertiser_path):
            os.unlink(advertiser_path)
