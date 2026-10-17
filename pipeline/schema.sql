-- ---------------------------------------------------------------------------
-- Star schema for the SaaS metrics dashboard.
--
-- Documented Snowflake / dbt model mapping (one mart per dbt model):
--   dim_date            <- models/marts/dim_date.sql            (seed: dim dates)
--   dim_plan            <- models/marts/dim_plan.sql            (seed: plan catalogue)
--   dim_account         <- models/marts/dim_account.sql         (ref: stg_accounts)
--   fact_mrr_movements  <- models/marts/fact_mrr_movements.sql  (ref: stg_movements)
--
-- Grain notes:
--   * dim_account is one row per account_id. A reactivation is a NEW
--     account row (is_reactivation = TRUE, original_account_id links back),
--     so a churned account never becomes active again - no zombie accounts.
--   * fact_mrr_movements is one row per MRR movement event. amount_cents is
--     SIGNED: positive for new/expansion, negative for contraction/churn, so
--     ending MRR = beginning MRR + SUM(amount_cents) holds exactly.
-- ---------------------------------------------------------------------------

DROP TABLE IF EXISTS fact_mrr_movements;
DROP TABLE IF EXISTS dim_account;
DROP TABLE IF EXISTS dim_plan;
DROP TABLE IF EXISTS dim_date;

CREATE TABLE dim_date (
    date_id      INTEGER PRIMARY KEY,  -- YYYYMM, e.g. 202401
    month_index  INTEGER NOT NULL,     -- 0-based offset from 2024-01
    full_date    DATE NOT NULL,        -- first day of the month
    year         INTEGER NOT NULL,
    month        INTEGER NOT NULL,     -- 1..12
    month_label  VARCHAR NOT NULL,     -- '2024-01'
    quarter      VARCHAR NOT NULL      -- '2024-Q1'
);

CREATE TABLE dim_plan (
    plan_id    INTEGER PRIMARY KEY,
    plan_name  VARCHAR NOT NULL,       -- Starter | Growth | Enterprise
    segment    VARCHAR NOT NULL,       -- SMB | Mid-Market | Enterprise
    mrr_cents  INTEGER NOT NULL       -- price per month, integer cents
);

CREATE TABLE dim_account (
    account_id          INTEGER PRIMARY KEY,
    signup_date_id      INTEGER NOT NULL REFERENCES dim_date(date_id),
    signup_month        INTEGER NOT NULL,                 -- 0-based month index
    plan_id             INTEGER NOT NULL REFERENCES dim_plan(plan_id),  -- final plan
    region              VARCHAR NOT NULL,                 -- NA | EU
    channel             VARCHAR NOT NULL,                 -- acquisition channel
    cac_cents           INTEGER NOT NULL,                 -- customer acquisition cost
    status              VARCHAR NOT NULL,                 -- 'active' | 'churned'
    churn_date_id       INTEGER REFERENCES dim_date(date_id),  -- NULL when active
    churn_month         INTEGER NOT NULL,                 -- -1 when active
    churn_reason        VARCHAR,                          -- exit-survey code
    is_reactivation     BOOLEAN NOT NULL,
    original_account_id INTEGER                           -- -1 unless reactivation
);

CREATE TABLE fact_mrr_movements (
    movement_id    INTEGER PRIMARY KEY,
    account_id     INTEGER NOT NULL REFERENCES dim_account(account_id),
    date_id        INTEGER NOT NULL REFERENCES dim_date(date_id),
    movement_type  VARCHAR NOT NULL,   -- new | expansion | contraction | churn
    amount_cents   INTEGER NOT NULL,   -- signed MRR delta, integer cents
    from_plan_id   INTEGER,            -- NULL for 'new'
    to_plan_id     INTEGER,            -- NULL for 'churn'
    churn_reason   VARCHAR             -- exit-survey code, only for 'churn'
);
