"""
Train, evaluate, backtest, and score the long-stay model.

Question: at the moment a dog or cat arrives, how likely is it to still be in
care 30 days later? A shelter that knows this on day one can start foster
outreach or rescue conversations while the animal is still healthy and
adoptable, rather than once a long stay is already under way.

What this script writes, all into the `ml` schema of shelter.duckdb:

  ml.model_evaluation      holdout metrics for the baseline and the model
  ml.calibration           predicted vs observed long-stay rate, by decile
  ml.feature_importance    permutation importance on the holdout year
  ml.monthly_performance   rolling backtest: each month scored by a model
                           trained only on what was known before it
  ml.feature_drift         population stability of key inputs, by month
  ml.stay_scores           current scores for animals in care under 30 days

Usage:
    python -m ml.train
    python -m ml.train --expect-no-signal   # sample data: fail if AUC is high
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.impute import SimpleImputer
from sklearn.inspection import permutation_importance
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score, brier_score_loss, roc_auc_score
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, OrdinalEncoder, StandardScaler
from scipy.optimize import brentq

from ml import features as F

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DB_PATH = PROJECT_ROOT / "shelter.duckdb"

HOLDOUT_DAYS = 365
TRAIN_YEARS = 4          # older years describe a different shelter
CAL_MONTHS = 6           # most recent mature months, held back to recalibrate
BACKTEST_MONTHS = 12
MAX_CATEGORIES = 40      # rarer breeds, colours, and places are grouped
RANDOM_STATE = 20261005

# Thresholds for flagging a backtest month for review. They are deliberately
# simple: a person reads the flag, not an automated retraining job. PSI above
# PSI_ALERT is recorded per feature but does not by itself flag a month.
AUC_FLOOR = 0.65
CALIBRATION_TOLERANCE = 0.05
PSI_ALERT = 0.25
# Below this many intakes, a month's AUC is too noisy to act on. It is still
# reported, but marked rather than flagged.
MIN_MONTH_ROWS = 200

# On the synthetic sample, length of stay is random with respect to every
# feature, so an honest model scores near 0.5. Anything well above that means
# the label is leaking into the inputs.
NO_SIGNAL_MAX_AUC = 0.60

DRIFT_FEATURES = ["intake_type", "intake_condition", "animal_type", "age_band", "census_band"]


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #

def baseline_model() -> Pipeline:
    """Regularised logistic regression. The bar the main model has to clear."""
    pre = ColumnTransformer(
        [
            (
                "cat",
                OneHotEncoder(handle_unknown="infrequent_if_exist", max_categories=MAX_CATEGORIES),
                F.CATEGORICAL_FEATURES,
            ),
            (
                "num",
                Pipeline([
                    ("impute", SimpleImputer(strategy="median", add_indicator=True)),
                    ("scale", StandardScaler()),
                ]),
                F.NUMERIC_FEATURES,
            ),
        ]
    )
    return Pipeline([
        ("pre", pre),
        ("clf", LogisticRegression(max_iter=2000, C=0.5)),
    ])


def boosted_model() -> Pipeline:
    """Gradient boosting with native categorical handling and missing values."""
    pre = ColumnTransformer(
        [
            (
                "cat",
                OrdinalEncoder(
                    handle_unknown="use_encoded_value",
                    unknown_value=-1,
                    max_categories=MAX_CATEGORIES,
                ),
                F.CATEGORICAL_FEATURES,
            ),
            ("num", "passthrough", F.NUMERIC_FEATURES),
        ],
        verbose_feature_names_out=False,
    )
    is_categorical = [True] * len(F.CATEGORICAL_FEATURES) + [False] * len(F.NUMERIC_FEATURES)
    clf = HistGradientBoostingClassifier(
        categorical_features=is_categorical,
        learning_rate=0.05,
        max_iter=400,
        max_leaf_nodes=31,
        min_samples_leaf=40,
        l2_regularization=1.0,
        early_stopping=True,
        validation_fraction=0.1,
        n_iter_no_change=25,
        random_state=RANDOM_STATE,
    )
    return Pipeline([("pre", pre), ("clf", clf)])


def _logit(p: np.ndarray) -> np.ndarray:
    p = np.clip(p, 1e-6, 1 - 1e-6)
    return np.log(p / (1 - p))


def _sigmoid(z: np.ndarray) -> np.ndarray:
    return 1 / (1 + np.exp(-z))


class RecalibratedModel:
    """
    A boosted model plus a correction for the shelter changing under it.

    The long-stay rate is not stable: in Austin's 2024 to 2025 holdout it ran
    about ten points above the long-run average, so a model fitted on four
    years of history ranked animals well but predicted the old rate. The fix is to hold back
    the most recent months of mature labels, fit the model on the years before
    them, and then fit a slope and intercept on the log-odds (Platt scaling)
    against those recent months. Ranking is unchanged as long as the slope is
    positive; the probabilities move toward what the shelter looks like now.
    """

    def __init__(self, base: Pipeline, slope: float, intercept: float):
        self.base = base
        self.slope = slope
        self.intercept = intercept

    def predict_proba(self, X: pd.DataFrame) -> np.ndarray:
        z = _logit(self.base.predict_proba(X)[:, 1])
        p = _sigmoid(self.slope * z + self.intercept)
        return np.column_stack([1 - p, p])


def fit_recalibrated(
    data: pd.DataFrame,
    X_all: pd.DataFrame,
    y_all: np.ndarray,
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> tuple[RecalibratedModel, pd.Timestamp]:
    """Fit on [start, cal_start), recalibrate on [cal_start, end). Nothing after end."""
    cal_start = end - pd.DateOffset(months=CAL_MONTHS)
    fit_idx = _window(data, start, cal_start).index
    cal_idx = _window(data, cal_start, end).index
    base = boosted_model().fit(X_all.loc[fit_idx], y_all[fit_idx])

    z = _logit(base.predict_proba(X_all.loc[cal_idx])[:, 1])
    y = y_all[cal_idx]
    slope, intercept = 1.0, 0.0
    if len(np.unique(y)) == 2:
        platt = LogisticRegression(C=100.0).fit(z.reshape(-1, 1), y)
        slope, intercept = float(platt.coef_[0, 0]), float(platt.intercept_[0])
        if slope <= 0:
            # A non-positive slope would flip or flatten the ranking, which
            # only happens when the base model has no signal. Fall back to
            # shifting the level alone, so the mean matches recent months.
            slope = 1.0
            intercept = brentq(lambda b: _sigmoid(z + b).mean() - y.mean(), -20, 20)
    return RecalibratedModel(base, slope, intercept), cal_start


# --------------------------------------------------------------------------- #
# Metrics
# --------------------------------------------------------------------------- #

def _safe_auc(y: np.ndarray, p: np.ndarray) -> float | None:
    return float(roc_auc_score(y, p)) if len(np.unique(y)) == 2 else None


def evaluate(y: np.ndarray, p: np.ndarray) -> dict:
    n = len(y)
    top_n = max(1, int(round(n * 0.10)))
    top_idx = np.argsort(-p)[:top_n]
    base_rate = float(y.mean())
    top_precision = float(y[top_idx].mean())
    return {
        "n": n,
        "base_rate": base_rate,
        "mean_predicted": float(p.mean()),
        "roc_auc": _safe_auc(y, p),
        "average_precision": float(average_precision_score(y, p)) if y.sum() else None,
        "brier": float(brier_score_loss(y, p)),
        "top_decile_precision": top_precision,
        "top_decile_lift": top_precision / base_rate if base_rate else None,
    }


def calibration_table(y: np.ndarray, p: np.ndarray) -> pd.DataFrame:
    frame = pd.DataFrame({"y": y, "p": p})
    # Rank first so ties do not collapse deciles on coarse scores.
    frame["decile"] = pd.qcut(frame["p"].rank(method="first"), 10, labels=False) + 1
    out = (
        frame.groupby("decile")
        .agg(n=("y", "size"), mean_predicted=("p", "mean"), observed_rate=("y", "mean"))
        .reset_index()
    )
    out["decile"] = out["decile"].astype(int)
    return out


def psi(expected: pd.Series, actual: pd.Series) -> float:
    """Population stability index across shared categories, floored at 0.5%."""
    e = expected.value_counts(normalize=True)
    a = actual.value_counts(normalize=True)
    cats = e.index.union(a.index)
    e = e.reindex(cats, fill_value=0).clip(lower=0.005)
    a = a.reindex(cats, fill_value=0).clip(lower=0.005)
    return float(((a - e) * np.log(a / e)).sum())


def _drift_view(x: pd.DataFrame) -> pd.DataFrame:
    view = x[["intake_type", "intake_condition", "animal_type"]].copy()
    view["age_band"] = pd.cut(
        x["age_at_intake_days"],
        bins=[-1, 180, 365, 1095, 2555, np.inf],
        labels=["<6m", "6-12m", "1-3y", "3-7y", "7y+"],
    ).astype(str)
    # The occupancy band is added by the caller, because its edges come from
    # the training period's own quintiles.
    return view


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def _model_version() -> str:
    sha = os.environ.get("GITHUB_SHA", "local")[:7]
    spec = repr((F.FEATURES, F.LONG_STAY_DAYS, TRAIN_YEARS, MAX_CATEGORIES, boosted_model().get_params()))
    return f"{sha}-{hashlib.sha1(spec.encode()).hexdigest()[:8]}"


def _window(df: pd.DataFrame, start: pd.Timestamp | None, end: pd.Timestamp) -> pd.DataFrame:
    mask = df["intake_at"] < end
    if start is not None:
        mask &= df["intake_at"] >= start
    return df[mask]


def run(expect_no_signal: bool) -> int:
    con = duckdb.connect(str(DB_PATH))
    frames = F.load(con)
    data = frames.labelled
    X_all = F.derive(data)
    y_all = data["is_long_stay"].to_numpy()

    version = _model_version()
    print(f"as of {frames.as_of:%Y-%m-%d}, labels mature before {frames.label_cutoff:%Y-%m-%d}")
    print(f"{len(data):,} labelled stays, long-stay rate {y_all.mean():.1%}")
    print(f"{frames.excluded_implausible_open:,} open stays older than "
          f"{F.MAX_PLAUSIBLE_OPEN_DAYS} days left out as likely missing outcomes")

    # ---- Holdout: the last year of mature labels -------------------------- #
    # Training stops 30 days before the holdout starts, so every training label
    # was already known on the first day of the holdout. Without that gap the
    # model would learn from stays whose outcome was still in the future.
    test_start = frames.label_cutoff - pd.Timedelta(days=HOLDOUT_DAYS)
    train_end = test_start - pd.Timedelta(days=F.LONG_STAY_DAYS)
    train_start = train_end - pd.DateOffset(years=TRAIN_YEARS)

    train_idx = _window(data, train_start, train_end).index
    test_idx = _window(data, test_start, frames.label_cutoff).index
    X_tr, y_tr = X_all.loc[train_idx], y_all[train_idx]
    X_te, y_te = X_all.loc[test_idx], y_all[test_idx]
    print(f"train {len(X_tr):,} stays ({train_start:%Y-%m-%d} to {train_end:%Y-%m-%d}), "
          f"holdout {len(X_te):,} ({test_start:%Y-%m-%d} to {frames.label_cutoff:%Y-%m-%d})")

    evaluation_rows = []
    for name, factory in (("logistic_baseline", baseline_model), ("gradient_boosting", boosted_model)):
        model = factory().fit(X_tr, y_tr)
        p = model.predict_proba(X_te)[:, 1]
        row = {"model": name, **evaluate(y_te, p)}
        evaluation_rows.append(row)
        auc = row["roc_auc"]
        print(f"  {name:<30} AUC {auc:.3f}  AP {row['average_precision']:.3f}  "
              f"Brier {row['brier']:.3f}  gap {row['mean_predicted'] - row['base_rate']:+.3f}  "
              f"top-10% lift {row['top_decile_lift']:.2f}x")

    # The model that is actually used: same window, recalibrated on its most
    # recent six months. Reported alongside the raw model so the cost to
    # ranking (if any) and the gain in calibration are both visible.
    main_model, cal_start = fit_recalibrated(data, X_all, y_all, train_start, train_end)
    p_te = main_model.predict_proba(X_te)[:, 1]
    row = {"model": "gradient_boosting_recalibrated", **evaluate(y_te, p_te),
           "calibration_start": cal_start, "calibration_slope": main_model.slope,
           "calibration_intercept": main_model.intercept}

    # Sensitivity check, not a scoring rule. Community cats in the
    # shelter-neuter-return program leave within days by design, so they are
    # easy short stays that flatter the AUC. The outcome subtype is not known
    # at intake, so they cannot be removed from the population the model
    # scores, but the holdout can be re-measured without them to show how much
    # of the headline number they account for.
    not_snr = (data.loc[test_idx, "outcome_subtype"].fillna("") != "Snr").to_numpy()
    if not_snr.sum() and len(np.unique(y_te[not_snr])) == 2:
        sub = evaluate(y_te[not_snr], p_te[not_snr])
        row["n_excluding_snr"] = sub["n"]
        row["base_rate_excluding_snr"] = sub["base_rate"]
        row["roc_auc_excluding_snr"] = sub["roc_auc"]
        row["top_decile_precision_excluding_snr"] = sub["top_decile_precision"]
    evaluation_rows.append(row)
    print(f"  {'gradient_boosting_recalibrated':<30} AUC {row['roc_auc']:.3f}  "
          f"AP {row['average_precision']:.3f}  Brier {row['brier']:.3f}  "
          f"gap {row['mean_predicted'] - row['base_rate']:+.3f}  "
          f"top-10% lift {row['top_decile_lift']:.2f}x")
    if "roc_auc_excluding_snr" in row:
        print(f"    excluding SNR transfers: AUC {row['roc_auc_excluding_snr']:.3f}, "
              f"long-stay rate {row['base_rate_excluding_snr']:.1%}, "
              f"top-10% precision {row['top_decile_precision_excluding_snr']:.1%} "
              f"({row['n_excluding_snr']:,} stays)")

    evaluation = pd.DataFrame(evaluation_rows)
    for col, value in (
        ("model_version", version),
        ("as_of", frames.as_of),
        ("train_start", train_start),
        ("train_end", train_end),
        ("holdout_start", test_start),
        ("holdout_end", frames.label_cutoff),
        ("long_stay_days", F.LONG_STAY_DAYS),
        ("excluded_implausible_open", frames.excluded_implausible_open),
    ):
        evaluation[col] = value

    calibration = calibration_table(y_te, p_te)
    calibration["model_version"] = version

    # Permutation importance on raw input columns, so the result reads in the
    # same terms a person would use. Sampled to keep the run short.
    sample = X_te.sample(n=min(len(X_te), 15000), random_state=RANDOM_STATE)
    imp = permutation_importance(
        main_model.base, sample, y_te[X_te.index.get_indexer(sample.index)],
        scoring="roc_auc", n_repeats=5, random_state=RANDOM_STATE,
    )
    importance = (
        pd.DataFrame({
            "feature": F.FEATURES,
            "auc_drop_mean": imp.importances_mean,
            "auc_drop_std": imp.importances_std,
        })
        .sort_values("auc_drop_mean", ascending=False)
        .reset_index(drop=True)
    )
    importance["rank"] = importance.index + 1
    importance["model_version"] = version

    # ---- Rolling backtest: what monitoring would have shown ---------------- #
    # Each month is scored by a model trained only on intakes whose labels were
    # known by the first of that month. That is the model staff would actually
    # have had, so the month's AUC and calibration are what they would have
    # seen. Storing train_end alongside lets a dbt test prove the gap held.
    def census_edges(ref: pd.DataFrame) -> dict:
        # Occupancy scales differ between dogs and cats, so quintiles are taken
        # within each animal type rather than across the pooled numbers.
        return {
            t: np.unique(np.quantile(g["census_same_type"].dropna(), [0.2, 0.4, 0.6, 0.8]))
            for t, g in ref.groupby("animal_type")
            if g["census_same_type"].notna().any()
        }

    def drift_frame(x: pd.DataFrame, edges: dict) -> pd.DataFrame:
        view = _drift_view(x)
        band = pd.Series("unknown", index=x.index)
        for t, e in edges.items():
            rows = x["animal_type"] == t
            band[rows] = np.digitize(x.loc[rows, "census_same_type"].fillna(-1), e).astype(str)
        view["census_band"] = band
        return view

    last_month = (frames.label_cutoff - pd.Timedelta(days=1)).to_period("M")
    # Only whole months whose every intake has a mature label.
    if (last_month + 1).to_timestamp() > frames.label_cutoff:
        last_month -= 1
    months = pd.period_range(end=last_month, periods=BACKTEST_MONTHS, freq="M")

    perf_rows, drift_rows = [], []
    for month in months:
        m_start = month.to_timestamp()
        m_end = (month + 1).to_timestamp()
        bt_train_end = m_start - pd.Timedelta(days=F.LONG_STAY_DAYS)
        bt_train_start = bt_train_end - pd.DateOffset(years=TRAIN_YEARS)
        tr = _window(data, bt_train_start, bt_train_end).index
        te = _window(data, m_start, m_end).index
        if len(te) == 0 or len(np.unique(y_all[tr])) < 2:
            continue
        model, bt_cal_start = fit_recalibrated(data, X_all, y_all, bt_train_start, bt_train_end)
        p = model.predict_proba(X_all.loc[te])[:, 1]
        metrics = evaluate(y_all[te], p)

        # Drift is measured against the same calendar month in the training
        # window. Comparing May with a whole year would flag kitten season as
        # drift every spring.
        ref_rows = X_all.loc[tr]
        ref_rows = ref_rows[data.loc[tr, "intake_at"].dt.month == month.month]
        edges = census_edges(ref_rows)
        reference = drift_frame(ref_rows, edges)
        month_view = drift_frame(X_all.loc[te], edges)
        month_psi = {feat: psi(reference[feat], month_view[feat]) for feat in DRIFT_FEATURES}
        for feat, value in month_psi.items():
            drift_rows.append({"intake_month": m_start, "feature": feat, "psi": value})

        reasons = []
        if metrics["roc_auc"] is not None and metrics["roc_auc"] < AUC_FLOOR:
            reasons.append("auc_below_floor")
        if abs(metrics["mean_predicted"] - metrics["base_rate"]) > CALIBRATION_TOLERANCE:
            reasons.append("calibration_gap")

        perf_rows.append({
            "intake_month": m_start,
            "train_start": bt_train_start,
            "train_end": bt_train_end,
            "calibration_start": bt_cal_start,
            "train_rows": len(tr),
            **metrics,
            "calibration_gap": metrics["mean_predicted"] - metrics["base_rate"],
            "max_feature_psi": max(month_psi.values()),
            # Drift is reported, not alerted on. A shift in who arrives only
            # matters if the model stops ranking them well, which the AUC and
            # calibration checks above already catch; drift is how you explain
            # the drop when it happens. Alerting on it alone trains people to
            # ignore the flag.
            "drifted_features": ", ".join(
                f for f, v in month_psi.items() if v > PSI_ALERT
            ) or None,
            "status": (
                "insufficient_data" if len(te) < MIN_MONTH_ROWS
                else "review" if reasons
                else "ok"
            ),
            "review_reasons": ", ".join(reasons) or None,
            "model_version": version,
        })

    performance = pd.DataFrame(perf_rows)
    drift = pd.DataFrame(drift_rows)
    drift["model_version"] = version
    flagged = int((performance["status"] == "review").sum()) if len(performance) else 0
    print(f"backtest: {len(performance)} months, {flagged} flagged for review")

    # ---- Score animals in care now ---------------------------------------- #
    # Refit on every mature label in the training window, then score open
    # stays younger than 30 days: the ones where a long stay can still be
    # headed off.
    final_start = frames.label_cutoff - pd.DateOffset(years=TRAIN_YEARS)
    final_model, _ = fit_recalibrated(data, X_all, y_all, final_start, frames.label_cutoff)

    open_now = frames.unlabelled_open
    if len(open_now):
        p_open = final_model.predict_proba(F.derive(open_now))[:, 1]
        # Bands use the holdout's score distribution, so "high" means the top
        # tenth of what the model typically sees, not an arbitrary cut.
        high, elevated = np.quantile(p_te, [0.9, 0.7])
        scores = pd.DataFrame({
            "stay_key": open_now["stay_key"],
            "animal_id": open_now["animal_id"],
            "animal_type": open_now["animal_type"],
            "intake_at": open_now["intake_at"],
            "days_in_care": (frames.as_of - open_now["intake_at"]).dt.days,
            "long_stay_probability": p_open,
        })
        scores["risk_band"] = np.select(
            [scores["long_stay_probability"] >= high, scores["long_stay_probability"] >= elevated],
            ["High", "Elevated"],
            default="Typical",
        )
    else:
        scores = pd.DataFrame(columns=[
            "stay_key", "animal_id", "animal_type", "intake_at", "days_in_care",
            "long_stay_probability", "risk_band",
        ])
    scores["model_version"] = version
    scores["scored_as_of"] = frames.as_of
    print(f"scored {len(scores):,} animals currently in care under {F.LONG_STAY_DAYS} days")

    # ---- Write ------------------------------------------------------------ #
    con.execute("create schema if not exists ml")
    for table, frame in (
        ("model_evaluation", evaluation),
        ("calibration", calibration),
        ("feature_importance", importance),
        ("monthly_performance", performance),
        ("feature_drift", drift),
        ("stay_scores", scores),
    ):
        con.register("frame", frame)
        con.execute(f"create or replace table ml.{table} as select * from frame")
        con.unregister("frame")
    con.close()

    if expect_no_signal:
        auc = evaluation.loc[evaluation["model"] == "gradient_boosting_recalibrated", "roc_auc"].iloc[0]
        if auc is None or auc > NO_SIGNAL_MAX_AUC:
            print(f"LEAKAGE CHECK FAILED: holdout AUC {auc} on data with no real signal "
                  f"(limit {NO_SIGNAL_MAX_AUC}). Something in the inputs encodes the label.")
            return 1
        print(f"leakage check passed: AUC {auc:.3f} on data with no real signal")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--expect-no-signal",
        action="store_true",
        help="Fail if the model finds signal. For synthetic sample data only.",
    )
    args = parser.parse_args(argv)
    return run(args.expect_no_signal)


if __name__ == "__main__":
    sys.exit(main())
