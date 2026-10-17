"""ETL: normalize generated events into a DuckDB star schema.

Reads the parquet files written by ``data_gen.generate`` (data/*.parquet),
applies ``pipeline/schema.sql``, and loads dim_date, dim_plan, dim_account
and fact_mrr_movements into ``data/saas_metrics.duckdb``.

The build function is importable so tests can run the whole load against an
in-memory database without touching the filesystem.
"""

from __future__ import annotations

import argparse
import pathlib

import duckdb
import pandas as pd

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
SCHEMA_PATH = pathlib.Path(__file__).resolve().parent / "schema.sql"
DB_PATH = DATA_DIR / "saas_metrics.duckdb"


def build_db(db_path: str | pathlib.Path, frames: dict[str, pd.DataFrame]) -> pathlib.Path:
    """Create the star schema at ``db_path`` and load all frames into it."""
    db_path = pathlib.Path(db_path)
    schema_sql = SCHEMA_PATH.read_text()

    accounts = frames["accounts"].copy()
    movements = frames["movements"].copy()
    plans = frames["plans"].copy()
    dates = frames["dates"].copy()

    month_to_date = dict(zip(dates["month_index"], dates["date_id"]))

    dim_date = dates[["date_id", "month_index", "full_date", "year",
                      "month", "month_label", "quarter"]].copy()
    dim_date["full_date"] = pd.to_datetime(dim_date["full_date"]).dt.date

    dim_plan = plans[["plan_id", "plan_name", "segment", "mrr_cents"]].copy()

    dim_account = pd.DataFrame({
        "account_id": accounts["account_id"],
        "signup_date_id": accounts["signup_month"].map(month_to_date),
        "signup_month": accounts["signup_month"],
        "plan_id": accounts["plan_id"],
        "region": accounts["region"],
        "channel": accounts["channel"],
        "cac_cents": accounts["cac_cents"],
        "status": accounts["churn_month"].map(lambda c: "churned" if c != -1 else "active"),
        "churn_date_id": accounts["churn_month"].map(
            lambda c: month_to_date[c] if c != -1 else None).astype("Int64"),
        "churn_month": accounts["churn_month"],
        "churn_reason": accounts["churn_reason"],
        "is_reactivation": accounts["is_reactivation"].astype(bool),
        "original_account_id": accounts["original_account_id"],
    })

    fact = pd.DataFrame({
        "movement_id": range(1, len(movements) + 1),
        "account_id": movements["account_id"],
        "date_id": movements["month"].map(month_to_date),
        "movement_type": movements["movement_type"],
        "amount_cents": movements["amount_cents"],
        "from_plan_id": movements["from_plan_id"].astype("Int64"),
        "to_plan_id": movements["to_plan_id"].astype("Int64"),
        "churn_reason": movements["churn_reason"],
    })

    con = duckdb.connect(str(db_path))
    try:
        con.execute(schema_sql)
        con.register("stg_dim_date", dim_date)
        con.register("stg_dim_plan", dim_plan)
        con.register("stg_dim_account", dim_account)
        con.register("stg_fact", fact)
        con.execute("INSERT INTO dim_date SELECT * FROM stg_dim_date")
        con.execute("INSERT INTO dim_plan SELECT * FROM stg_dim_plan")
        con.execute("INSERT INTO dim_account SELECT * FROM stg_dim_account")
        con.execute("INSERT INTO fact_mrr_movements SELECT * FROM stg_fact")

        counts = {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                  for t in ("dim_date", "dim_plan", "dim_account", "fact_mrr_movements")}
        print(f"loaded {db_path}: " + ", ".join(f"{t}={c:,}" for t, c in counts.items()))
    finally:
        con.close()
    return db_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Load generated events into DuckDB star schema.")
    parser.add_argument("--db", type=pathlib.Path, default=DB_PATH)
    args = parser.parse_args()

    frames = {}
    for name in ("accounts", "movements", "plans", "dates"):
        path = DATA_DIR / f"{name}.parquet"
        if not path.exists():
            raise SystemExit(f"missing {path} - run `python -m data_gen.generate` first")
        frames[name] = pd.read_parquet(path)

    args.db.parent.mkdir(parents=True, exist_ok=True)
    build_db(args.db, frames)


if __name__ == "__main__":
    main()
