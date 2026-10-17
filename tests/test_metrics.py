"""Metrics test-suite: exact MRR arithmetic, retention invariants, no zombies."""

import duckdb
import pandas as pd
import pytest

from data_gen.generate import generate_dataframes
from metrics.metrics import (account_view, churn_breakdown, cohort_retention,
                             forecast_mrr, kpi_snapshot, load_frames,
                             monthly_bridge)
from pipeline.etl import build_db

MOVEMENT_TYPES = {"new", "expansion", "contraction", "churn"}


@pytest.fixture(scope="module")
def frames():
    """End-to-end fixture: generate -> load into in-memory DuckDB -> read back."""
    generated = generate_dataframes(seed=42)
    con = duckdb.connect(":memory:")
    # build_db manages its own connection; use a temp file-backed one instead
    # so load_frames can re-open it read-only like the app does.
    import tempfile, pathlib
    tmp = pathlib.Path(tempfile.mkdtemp()) / "test.duckdb"
    build_db(tmp, generated)
    con.close()
    con = duckdb.connect(str(tmp), read_only=True)
    loaded = load_frames(con)
    yield loaded
    con.close()


def test_mrr_bridge_identity_exact(frames):
    """beginning + (new + expansion + contraction + churn) == ending, exactly."""
    bridge = monthly_bridge(frames)
    for _, r in bridge.iterrows():
        assert r["beginning"] + r["new"] + r["expansion"] + r["contraction"] + r["churn"] == r["ending"], \
            f"bridge broken for {r['month_label']}"


def test_movement_sign_convention(frames):
    """new/expansion are non-negative; contraction/churn are non-positive."""
    mov = frames["movements"]
    assert set(mov["movement_type"].unique()) <= MOVEMENT_TYPES
    assert (mov.loc[mov["movement_type"] == "new", "amount_cents"] >= 0).all()
    assert (mov.loc[mov["movement_type"] == "expansion", "amount_cents"] > 0).all()
    assert (mov.loc[mov["movement_type"] == "contraction", "amount_cents"] < 0).all()
    assert (mov.loc[mov["movement_type"] == "churn", "amount_cents"] < 0).all()


def test_retention_in_unit_interval(frames):
    """Every observable cohort retention value lies in [0, 1].

    NaN marks ages a cohort has not lived long enough to observe
    (e.g. the final cohort has only age 0) - those are excluded.
    """
    import numpy as np
    ret = cohort_retention(frames)
    vals = ret.to_numpy(dtype=float)
    observed = vals[~np.isnan(vals)]
    assert len(observed) > 0
    assert ((observed >= 0) & (observed <= 1)).all()


def test_retention_monotonic_non_increasing(frames):
    """Retention never rises within a cohort (no zombie reactivations)."""
    ret = cohort_retention(frames)
    for cohort in ret.index:
        vals = ret.loc[cohort].dropna().to_numpy()
        assert (vals[:-1] >= vals[1:]).all(), f"retention rose in {cohort}"


def test_no_zombie_accounts(frames):
    """Churned accounts never appear as active again after their churn month."""
    acc = frames["accounts"]
    mov = frames["movements"]
    churned = acc[acc["churn_month"] != -1]
    assert len(churned) > 0, "expected some churned accounts in the fixture"
    for _, a in churned.iterrows():
        later = mov[(mov["account_id"] == a["account_id"]) & (mov["month"] > a["churn_month"])]
        assert later.empty, f"zombie activity for account {a['account_id']}"
    # and the account table agrees: churned status means zero current MRR
    view = account_view(frames)
    assert (view.loc[view["status"] == "churned", "mrr_usd"] == 0).all()


def test_reactivations_are_new_rows(frames):
    """Reactivations are new account_ids linked to a still-churned original."""
    acc = frames["accounts"]
    react = acc[acc["is_reactivation"]]
    assert len(react) > 0, "expected some reactivations in the fixture"
    assert react["account_id"].is_unique
    for _, r in react.iterrows():
        orig = acc[acc["account_id"] == r["original_account_id"]]
        assert len(orig) == 1
        assert int(orig.iloc[0]["churn_month"]) != -1, "original must stay churned"
        assert int(orig.iloc[0]["churn_month"]) < int(r["signup_month"])


def test_plan_lookup_consistency(frames):
    """Every new-logo movement amount matches the plan catalogue MRR."""
    mov = frames["movements"]
    plans = frames["plans"]
    plan_mrr = dict(zip(plans["plan_id"], plans["mrr_cents"]))
    new_mov = mov[mov["movement_type"] == "new"]
    assert (new_mov["amount_cents"] == new_mov["to_plan_id"].map(plan_mrr)).all()


def test_bridge_matches_fact_totals(frames):
    """The bridge aggregates equal the raw fact-table sums per month."""
    bridge = monthly_bridge(frames).set_index("month_index")
    mov = frames["movements"]
    for m, grp in mov.groupby("month"):
        by_type = grp.groupby("movement_type")["amount_cents"].sum()
        assert bridge.loc[m, "new"] == int(by_type.get("new", 0))
        assert bridge.loc[m, "expansion"] == int(by_type.get("expansion", 0))
        assert bridge.loc[m, "contraction"] == int(by_type.get("contraction", 0))
        assert bridge.loc[m, "churn"] == int(by_type.get("churn", 0))


def test_kpi_snapshot_and_forecast_shapes(frames):
    """KPI snapshot returns all cards; forecast returns 6 labelled steps."""
    kpis = kpi_snapshot(frames, month_index=23)
    for key in ("mrr", "arr", "nrr", "grr", "logo_churn", "ltv_cac"):
        assert key in kpis
    assert 0 < kpis["nrr"][0] < 2
    assert 0 <= kpis["logo_churn"][0] <= 1
    fc = forecast_mrr(frames, horizon=6)
    assert len(fc) == 6
    assert (fc["mrr_lower"] <= fc["mrr_central"]).all()
    assert (fc["mrr_central"] <= fc["mrr_upper"]).all()
    bd = churn_breakdown(frames)
    assert set(bd.keys()) == {"by_plan", "by_region", "reasons"}
    assert not bd["reasons"].empty
