"""Executive SaaS revenue dashboard.

Panels:
  1. KPI row - MRR, ARR, NRR, GRR, logo churn, LTV:CAC (vs prior month)
  2. MRR bridge waterfall
  3. Cohort retention heatmap
  4. Churn drilldown - by plan tier, by region, top exit-survey reasons
  5. 6-month MRR forecast band (trend extrapolation, labelled as such)
  6. Account table - searchable, sortable

Run with:  streamlit run app.py   (from the project root, after the
quickstart steps in the README have built data/saas_metrics.duckdb)
"""

from __future__ import annotations

import pathlib

import duckdb
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from metrics.metrics import (account_view, churn_breakdown, cohort_retention,
                             forecast_mrr, kpi_snapshot, load_frames,
                             monthly_bridge)

DB_PATH = pathlib.Path(__file__).resolve().parent / "data" / "saas_metrics.duckdb"

st.set_page_config(page_title="SaaS Metrics Dashboard", layout="wide",
                   page_icon="📊")
st.title("📊 SaaS Metrics Dashboard")
st.caption("Executive revenue analytics · seeded synthetic data · DuckDB star schema")


@st.cache_data(show_spinner="Loading metrics…")
def get_frames() -> dict[str, pd.DataFrame]:
    if not DB_PATH.exists():
        # First run (e.g. Streamlit Community Cloud): build the seeded
        # warehouse in place so the demo works with no committed binaries.
        from data_gen.generate import generate_dataframes
        from pipeline.etl import build_db

        with st.spinner("Building seeded demo dataset (one-time, a few seconds)…"):
            DB_PATH.parent.mkdir(parents=True, exist_ok=True)
            build_db(DB_PATH, generate_dataframes())
    con = duckdb.connect(str(DB_PATH), read_only=True)
    try:
        return load_frames(con)
    finally:
        con.close()


frames = get_frames()
dates = frames["dates"]
month_labels = dates["month_label"].tolist()

# ---------------------------------------------------------------- sidebar ---
st.sidebar.header("Controls")
month_label = st.sidebar.selectbox(
    "Analysis month", month_labels, index=len(month_labels) - 1)
month_index = int(dates.loc[dates["month_label"] == month_label, "month_index"].iloc[0])
region_filter = st.sidebar.multiselect("Region (drilldown + table)",
                                       ["NA", "EU"], default=["NA", "EU"])
st.sidebar.caption("KPI cards, bridge, cohorts and forecast always use the full dataset.")

# ------------------------------------------------------------ 1. KPI row ---
kpis = kpi_snapshot(frames, month_index)
st.subheader(f"Key metrics — {kpis['month_label']}")

cols = st.columns(6)
mrr, mrr_d = kpis["mrr"]
arr, arr_d = kpis["arr"]
nrr, nrr_d = kpis["nrr"]
grr, grr_d = kpis["grr"]
lc, lc_d = kpis["logo_churn"]
ltv_cac, _ = kpis["ltv_cac"]

cols[0].metric("MRR", f"${mrr:,.0f}", f"${mrr_d:+,.0f} MoM")
cols[1].metric("ARR", f"${arr:,.0f}", f"${arr_d:+,.0f} MoM")
cols[2].metric("NRR", f"{nrr:.1%}", f"{nrr_d:+.1%} MoM")
cols[3].metric("GRR", f"{grr:.1%}", f"{grr_d:+.1%} MoM")
cols[4].metric("Logo churn", f"{lc:.1%}", f"{lc_d:+.1%} MoM", delta_color="inverse")
cols[5].metric("LTV : CAC", f"{ltv_cac:.1f}x")

st.caption(f"ARPA ${kpis['arpa'][0]:,.0f} · CAC payback {kpis['cac_payback_months'][0]:.1f} months")

# ------------------------------------------------------- 2. MRR bridge ---
st.subheader("MRR bridge — waterfall")
bridge = monthly_bridge(frames)
row = bridge.loc[bridge["month_index"] == month_index].iloc[0]
to_usd = lambda c: c / 100  # noqa: E731
fig_bridge = go.Figure(go.Waterfall(
    orientation="v",
    measure=["absolute", "relative", "relative", "relative", "relative", "total"],
    x=["Starting MRR", "New", "Expansion", "Contraction", "Churn", "Ending MRR"],
    y=[to_usd(row["beginning"]), to_usd(row["new"]), to_usd(row["expansion"]),
       to_usd(row["contraction"]), to_usd(row["churn"]), to_usd(row["ending"])],
    textposition="outside",
    connector={"line": {"color": "rgb(63, 63, 63)"}},
))
fig_bridge.update_layout(title=f"{month_label}: ${to_usd(row['beginning']):,.0f} → "
                               f"${to_usd(row['ending']):,.0f}",
                         showlegend=False, height=420)
st.plotly_chart(fig_bridge, width="stretch")

# ------------------------------------------------- 3. cohort heatmap ---
st.subheader("Cohort retention heatmap")
ret = cohort_retention(frames)
fig_heat = px.imshow(
    ret.values * 100,
    x=[f"Month {c}" for c in ret.columns],
    y=ret.index,
    color_continuous_scale="Greens",
    aspect="auto",
    labels={"x": "Months since signup", "y": "Cohort", "color": "Retention %"},
    text_auto=".0f",
)
fig_heat.update_layout(height=560, title="Retention % by signup cohort")
st.plotly_chart(fig_heat, width="stretch")

# ------------------------------------------------- 4. churn drilldown ---
st.subheader("Churn drilldown")
bd = churn_breakdown(frames, months_back=3)
c1, c2 = st.columns(2)
with c1:
    by_plan = bd["by_plan"]
    fig_p = px.bar(by_plan, x="plan", y="churn_rate",
                   title="Trailing-3-month logo churn by plan tier",
                   labels={"plan": "Plan", "churn_rate": "Churn rate"})
    fig_p.update_yaxes(tickformat=".1%")
    st.plotly_chart(fig_p, width="stretch")
with c2:
    by_region = bd["by_region"]
    fig_r = px.bar(by_region, x="region", y="churn_rate",
                   title="Trailing-3-month logo churn by region",
                   labels={"region": "Region", "churn_rate": "Churn rate"})
    fig_r.update_yaxes(tickformat=".1%")
    st.plotly_chart(fig_r, width="stretch")

reasons = bd["reasons"]
if not reasons.empty:
    fig_re = px.bar(reasons, x="reason", y="churned_logos",
                    title="Top churn reasons (synthetic exit-survey codes, trailing 3 months)",
                    labels={"reason": "Exit-survey code", "churned_logos": "Churned logos"})
    st.plotly_chart(fig_re, width="stretch")

# ---------------------------------------------------------- 5. forecast ---
st.subheader("MRR forecast — next 6 months")
fc = forecast_mrr(frames, horizon=6)
hist = bridge[["month_label", "ending"]].copy()
hist["ending_usd"] = hist["ending"] / 100
fig_fc = go.Figure()
fig_fc.add_trace(go.Scatter(x=hist["month_label"], y=hist["ending_usd"],
                            mode="lines+markers", name="Actual MRR"))
fig_fc.add_trace(go.Scatter(x=fc["month_label"], y=fc["mrr_central"] / 100,
                            mode="lines+markers", name="Trend forecast",
                            line={"dash": "dash"}))
fig_fc.add_trace(go.Scatter(x=list(fc["month_label"]) + list(fc["month_label"][::-1]),
                            y=list(fc["mrr_upper"] / 100) + list(fc["mrr_lower"][::-1] / 100),
                            fill="toself", fillcolor="rgba(0,100,200,0.15)",
                            line={"color": "rgba(255,255,255,0)"},
                            name="95% band", showlegend=True))
fig_fc.update_layout(title="MRR: actual vs 6-month trend extrapolation",
                     yaxis_title="MRR (USD)", height=420)
st.plotly_chart(fig_fc, width="stretch")
st.caption("⚠️ " + fc.attrs.get("note", "Trend extrapolation from the trailing-6-month NRR series."))

# ------------------------------------------------------ 6. account table ---
st.subheader("Account drilldown")
view = account_view(frames)
if region_filter:
    view = view[view["region"].isin(region_filter)]
query = st.text_input("Search (account id, plan, region, status, channel)",
                      placeholder="e.g. 1042 or Enterprise or churned")
if query:
    q = query.strip().lower()
    mask = (view["account_id"].astype(str).str.contains(q)
            | view["plan"].str.lower().str.contains(q)
            | view["region"].str.lower().str.contains(q)
            | view["status"].str.lower().str.contains(q)
            | view["channel"].str.lower().str.contains(q))
    view = view[mask]
st.dataframe(view, width="stretch", hide_index=True)
st.caption(f"{len(view):,} accounts shown")
