"""Metrics layer: the SaaS KPI set, computed from the star schema.

All money is integer cents internally (exact arithmetic - no float drift);
values are converted to dollars only at presentation time.

Definitions:
  * MRR bridge:  ending = beginning + new + expansion + contraction + churn
    (contraction/churn are negative), verified exactly by the test-suite.
  * NRR (net revenue retention) = (begin + expansion + contraction + churn) / begin
  * GRR (gross revenue retention) = (begin + contraction + churn) / begin
  * Logo churn  = churned logos / beginning logos
  * Revenue churn = |churn| / beginning MRR
  * LTV = ARPA / monthly revenue-churn rate x gross margin (0.8)
  * CAC payback (months) = avg CAC / (ARPA x gross margin)
"""

from __future__ import annotations

import numpy as np
import pandas as pd

GROSS_MARGIN = 0.80
N_MONTHS = 24


def load_frames(con) -> dict[str, pd.DataFrame]:
    """Read the whole star schema into pandas (dataset is tiny)."""
    return {
        "accounts": con.execute("SELECT * FROM dim_account").df(),
        "movements": con.execute(
            "SELECT m.*, d.month_index AS month, d.month_label "
            "FROM fact_mrr_movements m JOIN dim_date d USING (date_id)").df(),
        "plans": con.execute("SELECT * FROM dim_plan").df(),
        "dates": con.execute("SELECT * FROM dim_date ORDER BY month_index").df(),
    }


def monthly_bridge(frames: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Per-month MRR bridge with the exact identity ending = begin + deltas."""
    acc = frames["accounts"].copy()
    mov = frames["movements"]
    dates = frames["dates"]
    plan_mrr = dict(zip(frames["plans"]["plan_id"], frames["plans"]["mrr_cents"]))
    acc["plan_mrr"] = acc["plan_id"].map(plan_mrr)

    rows = []
    for _, d in dates.iterrows():
        m = int(d["month_index"])
        begin_mask = (acc["signup_month"] <= m) & ((acc["churn_month"] == -1) | (acc["churn_month"] > m))
        end_mask = (acc["signup_month"] <= m) & ((acc["churn_month"] == -1) | (acc["churn_month"] >= m + 1))
        mv = mov[mov["month"] == m]
        by_type = mv.groupby("movement_type")["amount_cents"].sum()
        new = int(by_type.get("new", 0))
        expansion = int(by_type.get("expansion", 0))
        contraction = int(by_type.get("contraction", 0))   # negative
        churn = int(by_type.get("churn", 0))               # negative
        beginning = int(acc.loc[begin_mask, "plan_mrr"].sum())
        ending = beginning + new + expansion + contraction + churn
        churned_logos = int((mv["movement_type"] == "churn").sum())
        rows.append({
            "month_index": m, "date_id": int(d["date_id"]), "month_label": d["month_label"],
            "beginning": beginning, "new": new, "expansion": expansion,
            "contraction": contraction, "churn": churn, "ending": ending,
            "begin_logos": int(begin_mask.sum()), "end_logos": int(end_mask.sum()),
            "churned_logos": churned_logos,
        })
    bridge = pd.DataFrame(rows)
    bridge["nrr"] = np.where(bridge["beginning"] > 0,
                             (bridge["beginning"] + bridge["expansion"]
                              + bridge["contraction"] + bridge["churn"]) / bridge["beginning"], np.nan)
    bridge["grr"] = np.where(bridge["beginning"] > 0,
                             (bridge["beginning"] + bridge["contraction"]
                              + bridge["churn"]) / bridge["beginning"], np.nan)
    bridge["logo_churn"] = np.where(bridge["begin_logos"] > 0,
                                    bridge["churned_logos"] / bridge["begin_logos"], np.nan)
    bridge["revenue_churn"] = np.where(bridge["beginning"] > 0,
                                       -bridge["churn"] / bridge["beginning"], np.nan)
    return bridge


def kpi_snapshot(frames: dict[str, pd.DataFrame], month_index: int) -> dict:
    """KPIs for one month plus deltas vs the prior month."""
    bridge = monthly_bridge(frames)
    cur = bridge.loc[bridge["month_index"] == month_index].iloc[0]
    prev = bridge.loc[bridge["month_index"] == month_index - 1]
    prev = prev.iloc[0] if len(prev) else None

    acc = frames["accounts"]
    end_mask = (acc["signup_month"] <= month_index) & (
        (acc["churn_month"] == -1) | (acc["churn_month"] > month_index))
    plan_mrr = dict(zip(frames["plans"]["plan_id"], frames["plans"]["mrr_cents"]))
    active = acc.loc[end_mask]
    arpa_cents = float(active["plan_id"].map(plan_mrr).mean()) if len(active) else 0.0
    avg_cac = float(active["cac_cents"].mean()) if len(active) else 0.0
    rev_churn = float(cur["revenue_churn"]) if cur["beginning"] else 0.0
    lifetime_months = (1.0 / rev_churn) if rev_churn > 0 else float("nan")
    ltv_cents = arpa_cents * lifetime_months * GROSS_MARGIN if rev_churn > 0 else float("nan")
    payback = (avg_cac / (arpa_cents * GROSS_MARGIN)) if arpa_cents > 0 else float("nan")
    ltv_cac = (ltv_cents / avg_cac) if avg_cac > 0 and rev_churn > 0 else float("nan")

    def delta(key):
        if prev is None:
            return None
        return float(cur[key]) - float(prev[key])

    return {
        "month_label": cur["month_label"],
        "mrr": (cur["ending"] / 100, (delta("ending") or 0) / 100),
        "arr": (cur["ending"] * 12 / 100, ((delta("ending") or 0) * 12) / 100),
        "nrr": (float(cur["nrr"]), delta("nrr")),
        "grr": (float(cur["grr"]), delta("grr")),
        "logo_churn": (float(cur["logo_churn"]), delta("logo_churn")),
        "ltv_cac": (float(ltv_cac), None),
        "cac_payback_months": (float(payback), None),
        "arpa": (arpa_cents / 100, None),
    }


def cohort_retention(frames: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Retention matrix: rows = signup cohort, cols = months since signup.

    Values in [0, 1] and non-increasing within each cohort (a churned
    account never reactivates - reactivations are new rows in new cohorts).
    """
    acc = frames["accounts"]
    cohorts = sorted(acc["signup_month"].unique())
    data: dict[str, dict[int, float]] = {}
    for c in cohorts:
        cohort = acc[acc["signup_month"] == c]
        n = len(cohort)
        row = {}
        for age in range(0, N_MONTHS - c):
            retained = ((cohort["churn_month"] == -1) | (cohort["churn_month"] >= c + age)).mean()
            row[age] = float(retained)
        data[f"cohort_{c:02d}"] = row
    return pd.DataFrame(data).T


def churn_breakdown(frames: dict[str, pd.DataFrame],
                    months_back: int = 3) -> dict[str, pd.DataFrame]:
    """Trailing-N-month churn rates by plan and region, plus top exit reasons."""
    acc = frames["accounts"]
    plan_name = dict(zip(frames["plans"]["plan_id"], frames["plans"]["plan_name"]))
    # Trailing window over the full simulated horizon (24 months).
    window_start = N_MONTHS - months_back

    exposed, churned = [], []
    for _, a in acc.iterrows():
        for m in range(window_start, N_MONTHS):
            # Active at the START of month m: churned later (or never).
            if a["signup_month"] <= m and (a["churn_month"] == -1 or a["churn_month"] >= m):
                exposed.append((m, a["plan_id"], a["region"]))
                if a["churn_month"] == m:
                    churned.append((m, a["plan_id"], a["region"], a["churn_reason"]))
    exp_df = pd.DataFrame(exposed, columns=["month", "plan_id", "region"])
    chu_df = pd.DataFrame(churned, columns=["month", "plan_id", "region", "reason"])

    def rate(frame, group_col):
        if frame.empty:
            return pd.DataFrame(columns=[group_col, "churn_rate"])
        e = exp_df.groupby(group_col).size()
        c = chu_df.groupby(group_col).size().reindex(e.index, fill_value=0)
        return pd.DataFrame({group_col: e.index, "churn_rate": (c / e).values})

    by_plan = rate(chu_df, "plan_id")
    by_plan["plan"] = by_plan["plan_id"].map(plan_name)
    by_region = rate(chu_df, "region")
    reasons = (chu_df["reason"].value_counts().rename_axis("reason")
               .reset_index(name="churned_logos")) if not chu_df.empty else pd.DataFrame(
        columns=["reason", "churned_logos"])
    return {"by_plan": by_plan, "by_region": by_region, "reasons": reasons}


def forecast_mrr(frames: dict[str, pd.DataFrame], horizon: int = 6) -> pd.DataFrame:
    """6-month MRR forecast: linear trend on the trailing-6-month NRR series.

    This is a simple trend extrapolation (clearly labelled as such in the
    dashboard), not a predictive model.
    """
    bridge = monthly_bridge(frames).dropna(subset=["nrr"])
    hist = bridge.tail(6)
    t = np.arange(len(hist))
    nrr = hist["nrr"].to_numpy(dtype=float)
    slope, intercept = np.polyfit(t, nrr, 1)
    resid_std = float(np.std(nrr - (slope * t + intercept), ddof=1)) if len(nrr) > 2 else 0.0

    last_label = bridge.iloc[-1]["month_label"]
    last_mrr = float(bridge.iloc[-1]["ending"])
    rows = []
    mrr = last_mrr
    for h in range(1, horizon + 1):
        nrr_hat = float(np.clip(intercept + slope * (len(hist) - 1 + h), 0.80, 1.30))
        mrr = mrr * nrr_hat
        band = 1.96 * resid_std * h * mrr  # widening band with horizon
        rows.append({"step": h, "nrr_trend": nrr_hat,
                     "mrr_central": mrr,
                     "mrr_lower": max(mrr - band, 0.0),
                     "mrr_upper": mrr + band})
    fc = pd.DataFrame(rows)
    fc["month_label"] = [f"+{h}mo" for h in fc["step"]]
    fc.attrs["note"] = ("Trend extrapolation from the trailing-6-month NRR series; "
                        "not a predictive model.")
    return fc


def account_view(frames: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Account-level table for the dashboard drilldown."""
    acc = frames["accounts"].copy()
    plan_name = dict(zip(frames["plans"]["plan_id"], frames["plans"]["plan_name"]))
    plan_mrr = dict(zip(frames["plans"]["plan_id"], frames["plans"]["mrr_cents"]))
    label = dict(zip(frames["dates"]["month_index"], frames["dates"]["month_label"]))
    last_m = N_MONTHS - 1

    view = pd.DataFrame({
        "account_id": acc["account_id"],
        "signup_month": acc["signup_month"].map(label),
        "plan": acc["plan_id"].map(plan_name),
        "region": acc["region"],
        "channel": acc["channel"],
        "mrr_usd": np.where(acc["churn_month"] == -1,
                            acc["plan_id"].map(plan_mrr) / 100, 0.0).round(2),
        "tenure_months": np.where(acc["churn_month"] == -1,
                                  last_m - acc["signup_month"] + 1,
                                  acc["churn_month"] - acc["signup_month"] + 1),
        "status": np.where(acc["churn_month"] == -1, "active", "churned"),
        "churn_reason": acc["churn_reason"].fillna(""),
        "is_reactivation": acc["is_reactivation"],
    })
    return view.sort_values("account_id").reset_index(drop=True)
