"""Seeded synthetic SaaS subscription event generator.

Produces ~24 months of deterministic subscription events (signups, upgrades,
downgrades, churns, reactivations) for ~2,000 accounts across 3 plans and
2 regions. No API keys, no downloads - everything comes from a fixed
numpy Generator seed, so every run produces identical output.

Modelling rules enforced here (the test-suite verifies them):
  * beginning MRR + (new + expansion - contraction - churn) == ending MRR
    exactly, in integer cents.
  * A churned account NEVER becomes active again. A reactivation is emitted
    as a brand-new account row (new account_id, is_reactivation=True,
    original_account_id links back), so "no zombie accounts" holds.
  * Cohort retention is therefore non-increasing within each cohort.
"""

from __future__ import annotations

import argparse
import pathlib

import numpy as np
import pandas as pd

SEED = 42
N_MONTHS = 24
START_YEAR = 2024
START_MONTH = 1  # January

PLANS = [
    {"plan_id": 1, "plan_name": "Starter", "segment": "SMB",
     "mrr_cents": 4_900, "churn_p": 0.055, "upgrade_p": 0.028, "downgrade_p": 0.000},
    {"plan_id": 2, "plan_name": "Growth", "segment": "Mid-Market",
     "mrr_cents": 19_900, "churn_p": 0.028, "upgrade_p": 0.014, "downgrade_p": 0.012},
    {"plan_id": 3, "plan_name": "Enterprise", "segment": "Enterprise",
     "mrr_cents": 99_900, "churn_p": 0.012, "upgrade_p": 0.000, "downgrade_p": 0.008},
]
PLAN_MRR = {p["plan_id"]: p["mrr_cents"] for p in PLANS}

REGIONS = ["NA", "EU"]
REGION_P = [0.65, 0.35]

CHANNELS = ["organic", "paid_search", "referral", "partner"]
CHANNEL_P = [0.35, 0.30, 0.20, 0.15]
# CAC expressed as a multiple of first-month MRR, by acquisition channel.
CAC_MULT = {"organic": 1.5, "paid_search": 4.0, "referral": 1.0,
            "partner": 2.5, "winback": 0.8}

CHURN_REASONS = ["budget_cut", "missing_features", "switched_competitor",
                 "poor_support", "company_closed", "other"]
CHURN_REASON_P = [0.30, 0.22, 0.18, 0.12, 0.08, 0.10]

MOVEMENT_TYPES = ("new", "expansion", "contraction", "churn")


def _date_id(month_index: int) -> int:
    """YYYYMM integer for a 0-based month offset from 2024-01."""
    year = START_YEAR + (START_MONTH - 1 + month_index) // 12
    month = (START_MONTH - 1 + month_index) % 12 + 1
    return year * 100 + month


def _month_label(month_index: int) -> str:
    year = START_YEAR + (START_MONTH - 1 + month_index) // 12
    month = (START_MONTH - 1 + month_index) % 12 + 1
    return f"{year}-{month:02d}"


def generate_dataframes(seed: int = SEED) -> dict[str, pd.DataFrame]:
    """Generate the full synthetic dataset as pandas DataFrames."""
    rng = np.random.default_rng(seed)

    accounts: list[dict] = []
    movements: list[dict] = []

    def add_account(signup_month: int, plan_id: int, region: str, channel: str,
                    is_reactivation: bool = False,
                    original_account_id: int = -1) -> int:
        account_id = len(accounts) + 1
        cac = int(round(PLAN_MRR[plan_id] * CAC_MULT[channel] * rng.uniform(0.8, 1.2)))
        accounts.append({
            "account_id": account_id,
            "signup_month": signup_month,
            "plan_id": plan_id,
            "region": region,
            "channel": channel,
            "cac_cents": cac,
            "churn_month": -1,          # -1 == still active
            "churn_reason": None,
            "is_reactivation": is_reactivation,
            "original_account_id": original_account_id,
            "reactivated": False,      # bookkeeping only, dropped later
        })
        movements.append({
            "account_id": account_id,
            "month": signup_month,
            "movement_type": "new",
            "amount_cents": PLAN_MRR[plan_id],
            "from_plan_id": None,
            "to_plan_id": plan_id,
            "churn_reason": None,
        })
        return account_id

    for m in range(N_MONTHS):
        new_ids: set[int] = set()

        # ---- 1. new logos -------------------------------------------------
        n_new = 700 if m == 0 else int(rng.poisson(58 * (1 + 0.015 * m)))
        plan_ids = rng.choice([1, 2, 3], size=n_new, p=[0.55, 0.33, 0.12])
        regions = rng.choice(REGIONS, size=n_new, p=REGION_P)
        channels = rng.choice(CHANNELS, size=n_new, p=CHANNEL_P)
        for i in range(n_new):
            new_ids.add(add_account(m, int(plan_ids[i]), str(regions[i]), str(channels[i])))

        # ---- 2. reactivations (always NEW account rows) -------------------
        if m >= 2:
            eligible = [a for a in accounts
                        if a["churn_month"] != -1 and not a["reactivated"]
                        and a["churn_month"] <= m - 2]
            n_re = min(len(eligible), int(rng.poisson(6)))
            if n_re:
                picks = rng.choice(len(eligible), size=n_re, replace=False)
                for p in picks:
                    old = eligible[int(p)]
                    old["reactivated"] = True
                    new_ids.add(add_account(
                        m, old["plan_id"], old["region"], "winback",
                        is_reactivation=True,
                        original_account_id=old["account_id"]))

        # ---- 3. existing-base movements -----------------------------------
        plan_lookup = {p["plan_id"]: p for p in PLANS}
        for a in accounts:
            if a["account_id"] in new_ids or a["churn_month"] != -1:
                continue  # new this month, or already churned: no events
            pid = a["plan_id"]
            spec = plan_lookup[pid]
            r = rng.random()
            if r < spec["churn_p"]:
                a["churn_month"] = m
                reason = str(rng.choice(CHURN_REASONS, p=CHURN_REASON_P))
                a["churn_reason"] = reason
                movements.append({
                    "account_id": a["account_id"],
                    "month": m,
                    "movement_type": "churn",
                    "amount_cents": -PLAN_MRR[pid],
                    "from_plan_id": pid,
                    "to_plan_id": None,
                    "churn_reason": reason,
                })
            elif r < spec["churn_p"] + spec["upgrade_p"] and pid < 3:
                new_pid = pid + 1
                movements.append({
                    "account_id": a["account_id"],
                    "month": m,
                    "movement_type": "expansion",
                    "amount_cents": PLAN_MRR[new_pid] - PLAN_MRR[pid],
                    "from_plan_id": pid,
                    "to_plan_id": new_pid,
                    "churn_reason": None,
                })
                a["plan_id"] = new_pid
            elif r < spec["churn_p"] + spec["upgrade_p"] + spec["downgrade_p"] and pid > 1:
                new_pid = pid - 1
                movements.append({
                    "account_id": a["account_id"],
                    "month": m,
                    "movement_type": "contraction",
                    "amount_cents": PLAN_MRR[new_pid] - PLAN_MRR[pid],  # negative
                    "from_plan_id": pid,
                    "to_plan_id": new_pid,
                    "churn_reason": None,
                })
                a["plan_id"] = new_pid

    accounts_df = pd.DataFrame(accounts).drop(columns=["reactivated"])
    movements_df = pd.DataFrame(movements)
    plans_df = pd.DataFrame(PLANS)[["plan_id", "plan_name", "segment", "mrr_cents"]]
    dates_df = pd.DataFrame([{
        "date_id": _date_id(m),
        "month_index": m,
        "full_date": f"{_month_label(m)}-01",
        "year": START_YEAR + (START_MONTH - 1 + m) // 12,
        "month": (START_MONTH - 1 + m) % 12 + 1,
        "month_label": _month_label(m),
        "quarter": f"{START_YEAR + (START_MONTH - 1 + m) // 12}-Q{((START_MONTH - 1 + m) % 12) // 3 + 1}",
    } for m in range(N_MONTHS)])

    return {"accounts": accounts_df, "movements": movements_df,
            "plans": plans_df, "dates": dates_df}


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate seeded SaaS subscription events.")
    parser.add_argument("--seed", type=int, default=SEED)
    parser.add_argument("--out", type=pathlib.Path,
                        default=pathlib.Path(__file__).resolve().parent.parent / "data")
    args = parser.parse_args()

    frames = generate_dataframes(seed=args.seed)
    args.out.mkdir(parents=True, exist_ok=True)
    for name, df in frames.items():
        path = args.out / f"{name}.parquet"
        df.to_parquet(path, index=False)
        print(f"wrote {path} ({len(df):,} rows)")

    n_accounts = len(frames["accounts"])
    n_churned = int((frames["accounts"]["churn_month"] != -1).sum())
    n_react = int(frames["accounts"]["is_reactivation"].sum())
    print(f"accounts={n_accounts:,} churned={n_churned:,} reactivations(as new rows)={n_react:,}")


if __name__ == "__main__":
    main()
