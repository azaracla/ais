"""Train v7 ETA prediction — two-stage residual model + multi-model comparison.

Key v7 innovations (based on feedback analysis):
  1. Two-stage model: Stage A predicts coarse log(TTA), Stage B predicts residual
     → This recovers "oracle routing" implicitly without classification
  2. 6h trajectory features: stop fraction, COG variability, heading stability, SOG range
  3. Better physics baselines: eta_phys_corrected, eta_phys_closing
  4. Port/MMSI priors: waiting time, congestion, vessel-typical speed
  5. Draught: 100% coverage from ShipStaticData
  6. CatBoost + LightGBM + XGBoost comparison

Training strategy:
  - 5-fold cross-validation by MMSI group (no leakage)
  - Stage A outputs fold predictions → Stage B trains on residuals
  - Final metric: hold-out test set

Usage:
  uv run python ml/train_v7.py              # full training
  uv run python ml/train_v7.py --quick       # fast test with fewer trees
"""

import json
import sys
import warnings
from pathlib import Path

import lightgbm as lgb
import numpy as np
import polars as pl
import xgboost as xgb
from sklearn.metrics import mean_absolute_error

warnings.filterwarnings("ignore")

from utils import DATA_DIR

# Dataset: use v7 if available, fall back to v6
DATASET_V7 = DATA_DIR / "dataset_v7.parquet"
DATASET_V6 = DATA_DIR / "dataset.parquet"
MODEL_DIR = DATA_DIR / "models_v7"
MODEL_DIR.mkdir(exist_ok=True)

RANDOM_SEED = 42
QUICK_MODE = "--quick" in sys.argv

# ── Feature definitions ───────────────────────────────────────────────────────────

# These features are available in both v6 and v7 datasets
V6_NUMERIC = [
    "dist_to_dest_km", "sog", "cog", "bearing_offset_deg",
    "heading_offset_deg", "rate_of_turn", "rot_available",
    "vessel_length", "vessel_width", "length_width_ratio",
    "avg_sog_1h", "avg_sog_6h", "avg_sog_24h", "sog_trend_1h",
    "mmsi_avg_sog", "sog_vs_mmsi_avg",
    "heading_std_1h", "avg_heading_1h",
    "closing_speed_kmh", "approach_efficiency",
    "eta_naive_h",
]

# New v7 features (may not exist in v6 dataset)
V7_NEW_NUMERIC = [
    "stop_fraction_3h", "slow_fraction_3h",
    "stop_fraction_6h", "slow_fraction_6h",
    "cog_std_3h", "cog_std_6h",
    "heading_std_3h", "heading_std_6h",
    "sog_range_6h", "sog_delta_30min",
    "draught_filled",
    "port_avg_tta", "port_arrival_count", "port_avg_sog",
    "port_avg_vessel_area", "port_arrival_rate_per_hour",
    "mmsi_sog_std", "mmsi_sample_count", "mmsi_avg_tta",
    "mmsi_median_sog", "mmsi_avg_dist", "mmsi_sog_cv",
    "eta_phys_6h", "eta_phys_corrected", "eta_phys_closing",
    "eta_phys_bearing",
    "hour_sin", "hour_cos", "dow_sin", "dow_cos",
    "sog_vs_mmsi_typical", "stop_acceleration",
]

# Categorical features (for CatBoost, one-hot otherwise)
CATEGORICAL_COLS = ["ship_type", "nav_status", "port_lo_code_idx"]

TARGET = "time_to_arrival_hours"
HORIZON_BINS = [
    (0, 1, "0-1h"), (1, 6, "1-6h"), (6, 24, "6-24h"),
    (24, 72, "1-3d"), (72, 200, "3-8d"),
]

# Training budget
N_TREES = 500 if QUICK_MODE else 5000
EARLY_STOP = 30 if QUICK_MODE else 200


# ── Data loading ──────────────────────────────────────────────────────────────────

def load_data():
    """Load the best available dataset and build feature matrices."""
    dataset_path = DATASET_V7 if DATASET_V7.exists() else DATASET_V6
    print(f"Loading dataset: {dataset_path}")
    df = pl.read_parquet(dataset_path).sort(["mmsi", "pos_ts"])

    # Determine which features exist
    available_numeric = [f for f in V6_NUMERIC if f in df.columns]
    new_features = [f for f in V7_NEW_NUMERIC if f in df.columns]
    all_numeric = available_numeric + new_features

    missing_v6 = [f for f in V6_NUMERIC if f not in df.columns]
    if missing_v6:
        print(f"  Warning: missing v6 features: {missing_v6}")

    print(f"  Rows: {len(df)}  Columns: {len(df.columns)}")
    print(f"  Features: {len(available_numeric)} v6 + {len(new_features)} v7 new = {len(all_numeric)} numeric")

    # Build numeric matrix
    X_num = df.select(all_numeric).to_numpy().astype(np.float32)
    y = df[TARGET].to_numpy().ravel().astype(np.float32)

    # One-hot encode ship_type and nav_status (for LightGBM/XGBoost)
    onehot_cols = {}
    for col, min_count in [("ship_type", 500), ("nav_status", 1000)]:
        if col in df.columns:
            vals = df[col].fill_null(-1)
            counts = vals.value_counts().sort("count", descending=True)
            top = {int(r[0]) for r in counts.iter_rows() if r[1] >= min_count and r[0] is not None}
            arr = vals.to_numpy()
            for v in sorted(top):
                onehot_cols[f"{col}_{v}"] = (arr == v).astype(np.float32)
            other = ~np.isin(arr, list(top)) if top else np.ones(len(arr), dtype=bool)
            if other.any():
                onehot_cols[f"{col}_other"] = other.astype(np.float32)

    feature_names = all_numeric + sorted(onehot_cols.keys())
    X = np.column_stack(
        [X_num] + [onehot_cols[c] for c in sorted(onehot_cols.keys())]
    ).astype(np.float32)

    # Categorical features for CatBoost
    cat_idx = []
    for col in CATEGORICAL_COLS:
        if col in df.columns:
            cat_idx.append(len(all_numeric) + len(cat_idx))

    # MMSI for grouped splitting
    mmsi_arr = df["mmsi"].to_numpy()

    print(f"  Final: {X.shape[1]} features ({len(cat_idx)} categorical)")
    return X, X_num, y, feature_names, all_numeric, cat_idx, mmsi_arr, df


# ── Evaluation ────────────────────────────────────────────────────────────────────

def adaptive_predict(stage_a_model, stage_b_model, X, threshold=3.0, soft=True):
    """Adaptive two-stage prediction.

    If Stage A predicts TTA < threshold: use Stage A directly (it's accurate).
    If Stage A predicts TTA >= threshold: apply Stage B correction.
    Soft blending: sigmoid transition instead of hard cutoff.

    Args:
        stage_a_model: LightGBM model predicting log(TTA)
        stage_b_model: LightGBM model predicting residual on log scale
        X: feature matrix
        threshold: TTA threshold in hours for applying Stage B
        soft: if True, use sigmoid blending instead of hard cutoff

    Returns:
        y_pred in original hours scale
    """
    yp_a_log = stage_a_model.predict(X)
    yp_a = np.expm1(yp_a_log)

    # Add Stage A prediction as feature for Stage B
    X_b = np.column_stack([X, yp_a_log])
    yp_b_log = stage_b_model.predict(X_b)

    if soft:
        # Sigmoid blend: smooth transition around threshold
        # At threshold: 50% Stage A, 50% Stage A+B
        # Below threshold: mostly Stage A
        # Above threshold: mostly Stage A+B
        steepness = 2.0  # higher = sharper transition
        blend_weight = 1.0 / (1.0 + np.exp(-steepness * (yp_a - threshold) / threshold))
        yp_final_log = yp_a_log + blend_weight * yp_b_log
    else:
        # Hard threshold
        needs_b = yp_a >= threshold
        yp_final_log = yp_a_log.copy()
        yp_final_log[needs_b] = yp_a_log[needs_b] + yp_b_log[needs_b]

    yp_final = np.expm1(yp_final_log)
    return np.clip(yp_final, 0.05, 200)


def evaluate(name, y_true, y_pred, X_num=None, feature_names=None):
    """Print comprehensive evaluation metrics for a model."""
    mae = mean_absolute_error(y_true, y_pred)
    rmse = np.sqrt(np.mean((y_true - y_pred) ** 2))
    r2 = 1 - np.sum((y_true - y_pred) ** 2) / np.sum((y_true - y_true.mean()) ** 2)

    header = f"  {name}"
    print(f"\n{'─'*60}")
    print(f"{header}")
    print(f"{'─'*60}")
    print(f"  MAE:  {mae:.2f}h")
    print(f"  RMSE: {rmse:.1f}h")
    print(f"  R²:   {r2:.4f}")

    # Within-tolerance accuracy
    for thresholds in [(0.5, 1, 2, 6), (12, 24)]:
        parts = []
        for t in thresholds:
            acc = (np.abs(y_true - y_pred) <= t).mean()
            parts.append(f"±{t:2d}h={acc:.0%}" if t >= 1 else f"±{t:.1f}h={acc:.0%}")
        print(f"  {'  '.join(parts)}")

    # Per-horizon breakdown
    print(f"  Per-horizon MAE:")
    for lo, hi, hname in HORIZON_BINS:
        mask = (y_true >= lo) & (y_true < hi)
        if mask.sum() > 10:
            err = np.abs(y_true[mask] - y_pred[mask])
            print(f"    {hname:6s}: MAE={err.mean():.1f}h  n={mask.sum()}")

    # Naive comparison
    if X_num is not None:
        # Find eta_naive column
        for i, fn in enumerate(feature_names or []):
            if fn == "eta_naive_h":
                naive_pred = X_num[:, i]
                naive_mae = mean_absolute_error(y_true, naive_pred)
                improv = (1 - mae / naive_mae) * 100
                print(f"  Naive (dist/sog) MAE: {naive_mae:.1f}h  (improvement: {improv:.1f}%)")
                break

    return mae, r2


# ── Two-stage training ────────────────────────────────────────────────────────────

def train_two_stage_lightgbm(X_train, y_train_orig, X_test, y_test_orig,
                              feature_names, mmsi_train, mmsi_test,
                              hard_only: bool = False, hard_threshold: float = 3.0):
    """Train a two-stage LightGBM model with MMSI-grouped CV for Stage A.

    Stage A: Predict log(TTA) with 5-fold CV → produce unbiased predictions
    Stage B: Predict residual on log scale.
      - If hard_only=True: train Stage B ONLY on samples where |Stage A error| > hard_threshold.
        This makes Stage B a long-horizon specialist that doesn't add noise to easy cases.

    Returns: y_pred (in original hours scale)
    """
    print(f"\n{'='*60}")
    print(f"Two-Stage LightGBM {'(hard samples only, >'+str(hard_threshold)+'h)' if hard_only else ''}")
    print(f"{'='*60}")

    y_train_log = np.log1p(y_train_orig)
    y_test_log = np.log1p(y_test_orig)

    # Sample weights (balance short vs long horizons)
    w_train = 1.0 / (y_train_orig + 1.0)
    w_train = w_train / w_train.mean()

    # ── Stage A: 5-fold CV by MMSI ──
    print("\n── Stage A: Coarse log(TTA) prediction (5-fold CV) ──")
    unique_mmsis = np.unique(mmsi_train)
    np.random.seed(RANDOM_SEED)
    np.random.shuffle(unique_mmsis)

    n_folds = min(5, len(unique_mmsis))
    fold_size = len(unique_mmsis) // n_folds

    y_train_pred_a = np.zeros(len(y_train_log), dtype=np.float32)

    params_a = {
        "objective": "regression", "metric": "rmse", "boosting_type": "gbdt",
        "num_leaves": 127, "learning_rate": 0.02,
        "feature_fraction": 0.7, "bagging_fraction": 0.75, "bagging_freq": 5,
        "min_data_in_leaf": 50, "lambda_l1": 0.5, "lambda_l2": 2.0,
        "verbose": -1, "num_threads": 10, "seed": RANDOM_SEED,
    }

    for fold in range(n_folds):
        val_start = fold * fold_size
        val_end = (fold + 1) * fold_size if fold < n_folds - 1 else len(unique_mmsis)
        val_mmsis = set(unique_mmsis[val_start:val_end])
        train_idx = np.array([i for i, m in enumerate(mmsi_train) if m not in val_mmsis])
        val_idx = np.array([i for i, m in enumerate(mmsi_train) if m in val_mmsis])

        dtrain = lgb.Dataset(X_train[train_idx], label=y_train_log[train_idx],
                             weight=w_train[train_idx])
        dval = lgb.Dataset(X_train[val_idx], label=y_train_log[val_idx])

        model = lgb.train(
            params_a, dtrain, num_boost_round=N_TREES,
            valid_sets=[dval],
            callbacks=[lgb.early_stopping(EARLY_STOP), lgb.log_evaluation(0)],
        )
        y_train_pred_a[val_idx] = model.predict(X_train[val_idx])
        print(f"  Fold {fold+1}/{n_folds}: {len(val_idx)} val samples")

    # Train final Stage A on all training data for test predictions
    print("  Training final Stage A on all train data ...")
    dtrain_full = lgb.Dataset(X_train, label=y_train_log, weight=w_train)
    model_a = lgb.train(
        params_a, dtrain_full, num_boost_round=N_TREES,
        valid_sets=[dtrain_full],
        callbacks=[lgb.log_evaluation(0)],
    )
    y_test_pred_a = model_a.predict(X_test)

    # Stage A evaluation
    y_pred_a_orig = np.expm1(y_train_pred_a)
    mae_a = mean_absolute_error(y_train_orig, y_pred_a_orig)
    r2_a = 1 - np.sum((y_train_orig - y_pred_a_orig)**2) / np.sum((y_train_orig - y_train_orig.mean())**2)
    print(f"  Stage A CV MAE: {mae_a:.1f}h  R²: {r2_a:.4f}")

    # ── Stage B: Residual prediction (optionally hard samples only) ──
    print(f"\n── Stage B: Residual correction{' (HARD SAMPLES ONLY)' if hard_only else ''} ──")
    residual_train = y_train_log - y_train_pred_a

    if hard_only:
        # Train Stage B only on samples where Stage A was inaccurate (> hard_threshold hours)
        y_a_orig = np.expm1(y_train_pred_a)
        abs_err = np.abs(y_train_orig - y_a_orig)
        hard_mask = abs_err > hard_threshold
        n_hard = hard_mask.sum()
        print(f"  Hard samples: {n_hard}/{len(y_train_orig)} ({n_hard/len(y_train_orig)*100:.1f}%)")
        if n_hard < 100:
            print("  Too few hard samples, falling back to all samples")
            hard_mask = np.ones(len(y_train_orig), dtype=bool)
        residual_train_b = residual_train[hard_mask]
        X_train_b = np.column_stack([X_train[hard_mask], y_train_pred_a[hard_mask]])
        w_residual = np.abs(residual_train_b) + 0.1
    else:
        X_train_b = np.column_stack([X_train, y_train_pred_a])
        residual_train_b = residual_train
        w_residual = np.abs(residual_train) + 0.1

    w_residual = w_residual / w_residual.mean()

    X_test_b = np.column_stack([X_test, y_test_pred_a])

    dtrain_b = lgb.Dataset(X_train_b, label=residual_train_b, weight=w_residual)
    model_b = lgb.train(
        {**params_a, "num_leaves": 63, "learning_rate": 0.03},
        dtrain_b, num_boost_round=N_TREES,
        callbacks=[lgb.log_evaluation(0)],
    )

    y_test_pred_b = model_b.predict(X_test_b)

    # ── Adaptive blending: apply Stage B only when Stage A predicts > threshold ──
    y_pred_a_test = np.expm1(y_test_pred_a)
    blend_weight = 1.0 / (1.0 + np.exp(-2.0 * (y_pred_a_test - hard_threshold) / hard_threshold))
    y_pred_log = y_test_pred_a + blend_weight * y_test_pred_b
    y_pred_final = np.expm1(y_pred_log)
    y_pred_final = np.clip(y_pred_final, 0.05, 200)

    label = f"Two-Stage {'Hard' if hard_only else 'Full'}"
    evaluate(label, y_test_orig, y_pred_final, X_test, feature_names)

    # Feature importance for Stage B
    importance = model_b.feature_importance(importance_type="gain")
    top_idx = np.argsort(importance)[::-1][:15]
    print(f"\n  Stage B top features:")
    feature_names_b = feature_names + ["stage_a_prediction"]
    for i in top_idx:
        print(f"    {feature_names_b[i]:30s}: {importance[i] / importance.sum() * 100:.1f}%")

    return y_pred_final, model_a, model_b


def train_lightgbm_single(X_train, y_train, X_test, y_test, feature_names, w_train):
    """Train a single-stage LightGBM baseline."""
    print(f"\n── Single-Stage LightGBM (baseline) ──")

    y_train_log = np.log1p(y_train)
    dtrain = lgb.Dataset(X_train, label=y_train_log, weight=w_train)
    dval = lgb.Dataset(X_test, label=np.log1p(y_test))

    params = {
        "objective": "regression", "metric": "rmse", "boosting_type": "gbdt",
        "num_leaves": 127, "learning_rate": 0.05,
        "feature_fraction": 0.75, "bagging_fraction": 0.8, "bagging_freq": 5,
        "min_data_in_leaf": 30, "lambda_l1": 0.1, "lambda_l2": 1.0,
        "verbose": -1, "num_threads": 10, "seed": RANDOM_SEED,
    }

    model = lgb.train(
        params, dtrain, num_boost_round=N_TREES,
        valid_sets=[dval],
        callbacks=[lgb.early_stopping(EARLY_STOP), lgb.log_evaluation(100)],
    )

    y_pred = np.expm1(model.predict(X_test))
    evaluate("Single-Stage LGBM", y_test, y_pred, X_test, feature_names)

    # Feature importance
    importance = model.feature_importance(importance_type="gain")
    top_idx = np.argsort(importance)[::-1][:15]
    print(f"\n  Top 15 features:")
    for i in top_idx:
        print(f"    {feature_names[i]:30s}: {importance[i] / importance.sum() * 100:.1f}%")

    return y_pred, model


def train_catboost(X_train, y_train, X_test, y_test, feature_names, raw_df_train, raw_df_test, w_train):
    """Train CatBoost with native categorical support on raw columns."""
    print(f"\n── CatBoost ──")

    try:
        from catboost import CatBoostRegressor, Pool as CatPool
    except ImportError:
        print("  CatBoost not installed, skipping.")
        return None, None

    y_train_log = np.log1p(y_train)

    # Build feature matrix with raw categoricals (not one-hot)
    # Use Polars dataframes for easy categorical handling
    cat_cols_raw = []
    for col in ["ship_type", "nav_status"]:
        if col in raw_df_train.columns:
            cat_cols_raw.append(col)
    if "port_lo_code_idx" in raw_df_train.columns:
        cat_cols_raw.append("port_lo_code_idx")

    # Drop one-hot versions of these cols from X_train and add raw versions
    # For simplicity: use the full numeric features + raw categoricals
    # Rebuild X with raw categoricals instead of one-hot
    num_cols = [c for c in raw_df_train.columns
                if c in V6_NUMERIC + V7_NEW_NUMERIC]
    X_tr_num = raw_df_train[num_cols].to_numpy().astype(np.float32)
    X_te_num = raw_df_test[num_cols].to_numpy().astype(np.float32)

    # Build categorical features as strings (CatBoost handles string categoricals)
    cat_data_tr = []
    cat_data_te = []
    cat_feature_indices = []
    for col in cat_cols_raw:
        idx = X_tr_num.shape[1] + len(cat_data_tr)
        cat_feature_indices.append(idx)
        vals_tr = raw_df_train[col].fill_null(-1).cast(pl.Utf8).to_numpy().astype(str)
        vals_te = raw_df_test[col].fill_null(-1).cast(pl.Utf8).to_numpy().astype(str)
        cat_data_tr.append(vals_tr.reshape(-1, 1))
        cat_data_te.append(vals_te.reshape(-1, 1))

    X_tr_cb = np.column_stack([X_tr_num] + cat_data_tr) if cat_data_tr else X_tr_num
    X_te_cb = np.column_stack([X_te_num] + cat_data_te) if cat_data_te else X_te_num

    train_pool = CatPool(X_tr_cb, y_train_log, cat_features=cat_feature_indices, weight=w_train)
    test_pool = CatPool(X_te_cb, np.log1p(y_test), cat_features=cat_feature_indices)

    model = CatBoostRegressor(
        iterations=min(N_TREES, 1000),
        learning_rate=0.05,
        depth=8,
        l2_leaf_reg=3.0,
        bagging_temperature=0.5,
        od_type="Iter",
        od_wait=min(EARLY_STOP, 50),
        random_seed=RANDOM_SEED,
        thread_count=10,
        verbose=100 if not QUICK_MODE else 200,
        allow_writing_files=False,
    )
    model.fit(train_pool, eval_set=test_pool)

    y_pred = np.expm1(model.predict(X_te_cb))

    # Build feature names for CatBoost
    cb_feature_names = num_cols + [f"{c}_raw" for c in cat_cols_raw]
    evaluate("CatBoost", y_test, y_pred, X_te_cb, cb_feature_names)

    # Feature importance
    importance = model.get_feature_importance()
    top_idx = np.argsort(importance)[::-1][:15]
    print(f"\n  Top 15 features:")
    for i in top_idx:
        print(f"    {cb_feature_names[i]:30s}: {importance[i] / importance.sum() * 100:.1f}%")

    return y_pred, model


def train_xgboost(X_train, y_train, X_test, y_test, feature_names, w_train):
    """Train XGBoost baseline."""
    print(f"\n── XGBoost ──")

    y_train_log = np.log1p(y_train)
    dtrain = xgb.DMatrix(X_train, label=y_train_log, weight=w_train)
    dtest = xgb.DMatrix(X_test, label=np.log1p(y_test))

    params = {
        "objective": "reg:squarederror",
        "eval_metric": "rmse",
        "max_depth": 8,
        "learning_rate": 0.05,
        "subsample": 0.8,
        "colsample_bytree": 0.75,
        "min_child_weight": 5,
        "lambda": 1.0,
        "alpha": 0.1,
        "seed": RANDOM_SEED,
        "nthread": 10,
        "verbosity": 0,
    }

    model = xgb.train(
        params, dtrain, num_boost_round=N_TREES,
        evals=[(dtest, "test")],
        early_stopping_rounds=EARLY_STOP,
        verbose_eval=100,
    )

    y_pred = np.expm1(model.predict(dtest))
    evaluate("XGBoost", y_test, y_pred, X_test, feature_names)

    # Feature importance
    importance = model.get_score(importance_type="gain")
    total = sum(importance.values())
    top = sorted(importance.items(), key=lambda x: x[1], reverse=True)[:15]
    print(f"\n  Top 15 features:")
    for fname, score in top:
        idx = int(fname.replace("f", ""))
        print(f"    {feature_names[idx]:30s}: {score / total * 100:.1f}%")

    return y_pred, model


# ── Main ──────────────────────────────────────────────────────────────────────────

def main():
    print("=" * 70)
    print("Vessel ETA Prediction — v7")
    print(f"Mode: {'QUICK' if QUICK_MODE else 'FULL'} ({N_TREES} trees, patience={EARLY_STOP})")
    print("=" * 70)

    # ── Load data ──
    X, X_num, y, feature_names, numeric_features, cat_idx, mmsi_arr, df_raw = load_data()

    # ── Train/test split (80/20 by MMSI) ──
    print("\n── Train/Test Split ──")
    unique_mmsis = np.unique(mmsi_arr)
    np.random.seed(RANDOM_SEED)
    np.random.shuffle(unique_mmsis)
    split_n = int(len(unique_mmsis) * 0.8)
    train_mmsis = set(unique_mmsis[:split_n])

    train_mask = np.array([m in train_mmsis for m in mmsi_arr])
    test_mask = ~train_mask

    X_train, X_test = X[train_mask], X[test_mask]
    X_num_train, X_num_test = X_num[train_mask], X_num[test_mask]
    y_train, y_test = y[train_mask], y[test_mask]
    mmsi_train = mmsi_arr[train_mask]
    df_raw_train = df_raw.filter(pl.Series(train_mask))
    df_raw_test = df_raw.filter(pl.Series(test_mask))

    print(f"  Train: {len(X_train)} samples ({len(np.unique(mmsi_train))} MMSIs)")
    print(f"  Test:  {len(X_test)} samples ({len(np.unique(mmsi_arr[test_mask]))} MMSIs)")

    # Sample weights
    w_train = 1.0 / (y_train + 1.0)
    w_train = w_train / w_train.mean()

    # ── Model comparison ──
    print(f"\n{'='*70}")
    print("Model Comparison")
    print(f"{'='*70}")

    results = {}

    # 1. Single-stage LightGBM (baseline)
    yp_lgb, model_lgb = train_lightgbm_single(
        X_train, y_train, X_test, y_test, feature_names, w_train
    )
    if yp_lgb is not None:
        results["LightGBM"] = mean_absolute_error(y_test, yp_lgb)

    # 2. Two-stage LightGBM (full — key innovation)
    yp_2stage, model_a, model_b = train_two_stage_lightgbm(
        X_train, y_train, X_test, y_test, feature_names, mmsi_train, mmsi_arr[test_mask],
        hard_only=False
    )
    results["Two-Stage Full"] = mean_absolute_error(y_test, yp_2stage)

    # 2b. Two-stage LightGBM (hard samples only — makes Stage B a specialist)
    yp_2stage_hard, model_a_hard, model_b_hard = train_two_stage_lightgbm(
        X_train, y_train, X_test, y_test, feature_names, mmsi_train, mmsi_arr[test_mask],
        hard_only=True, hard_threshold=3.0
    )
    results["Two-Stage Hard"] = mean_absolute_error(y_test, yp_2stage_hard)

    # 3. CatBoost
    yp_cb, model_cb = train_catboost(
        X_train, y_train, X_test, y_test, feature_names,
        df_raw_train, df_raw_test, w_train
    )
    if yp_cb is not None:
        results["CatBoost"] = mean_absolute_error(y_test, yp_cb)

    # 4. XGBoost
    yp_xgb, model_xgb = train_xgboost(
        X_train, y_train, X_test, y_test, feature_names, w_train
    )
    if yp_xgb is not None:
        results["XGBoost"] = mean_absolute_error(y_test, yp_xgb)

    # 5. Ensemble: average of best models
    all_preds_list = [p for p in [yp_lgb, yp_2stage, yp_2stage_hard, yp_cb, yp_xgb] if p is not None]
    if len(all_preds_list) >= 2:
        yp_ens = np.mean(all_preds_list, axis=0)
        evaluate("Ensemble (avg all)", y_test, yp_ens, X_num_test, feature_names)
        results["Ensemble"] = mean_absolute_error(y_test, yp_ens)

    # ── Summary ──
    print(f"\n{'='*70}")
    print("Summary — v7")
    print(f"{'='*70}")

    # Naive baseline
    eta_idx = numeric_features.index("eta_naive_h")
    naive_mae = mean_absolute_error(y_test, X_num_test[:, eta_idx])
    print(f"  Naive (dist/sog):        {naive_mae:.1f}h MAE")
    print(f"  v6 global (reference):   10.4h MAE")

    for name, mae in sorted(results.items(), key=lambda x: x[1]):
        vs_naive = (1 - mae / naive_mae) * 100
        vs_v6 = (1 - mae / 10.4) * 100
        flag = " ← BEST" if name == min(results, key=results.get) else ""
        print(f"  {name:25s}: {mae:.1f}h MAE  (vs naive: {vs_naive:.0f}%  vs v6: {vs_v6:.0f}%){flag}")

    # ── Save best model ──
    best_name = min(results, key=results.get)
    best_mae = results[best_name]
    print(f"\n── Saving best model: {best_name} ({best_mae:.1f}h) ──")

    if best_name in ("Two-Stage Full", "Two-Stage Hard"):
        save_a = model_a_hard if best_name == "Two-Stage Hard" else model_a
        save_b = model_b_hard if best_name == "Two-Stage Hard" else model_b
        save_a.save_model(str(MODEL_DIR / "stage_a.txt"))
        save_b.save_model(str(MODEL_DIR / "stage_b.txt"))
        meta = {
            "version": "v7",
            "architecture": "two_stage_hard" if best_name == "Two-Stage Hard" else "two_stage",
            "hard_samples_only": best_name == "Two-Stage Hard",
            "hard_threshold": 3.0,
            "test_mae": float(best_mae),
            "feature_names": feature_names,
            "numeric_features": numeric_features,
            "horizon_bins": [(lo, hi, name) for lo, hi, name in HORIZON_BINS],
            "random_seed": RANDOM_SEED,
        }
    elif best_name == "LightGBM":
        model_lgb.save_model(str(MODEL_DIR / "model.txt"))
        meta = {
            "version": "v7",
            "architecture": "single_stage_lgbm",
            "test_mae": float(best_mae),
            "feature_names": feature_names,
            "numeric_features": numeric_features,
            "horizon_bins": [(lo, hi, name) for lo, hi, name in HORIZON_BINS],
            "random_seed": RANDOM_SEED,
        }
    elif best_name == "CatBoost":
        model_cb.save_model(str(MODEL_DIR / "model.cbm"))
        meta = {
            "version": "v7",
            "architecture": "catboost",
            "test_mae": float(best_mae),
            "feature_names": feature_names,
            "numeric_features": numeric_features,
            "horizon_bins": [(lo, hi, name) for lo, hi, name in HORIZON_BINS],
            "random_seed": RANDOM_SEED,
        }
    elif best_name == "XGBoost":
        model_xgb.save_model(str(MODEL_DIR / "model.json"))
        meta = {
            "version": "v7",
            "architecture": "xgboost",
            "test_mae": float(best_mae),
            "feature_names": feature_names,
            "numeric_features": numeric_features,
            "horizon_bins": [(lo, hi, name) for lo, hi, name in HORIZON_BINS],
            "random_seed": RANDOM_SEED,
        }

    json.dump(meta, open(MODEL_DIR / "metadata.json", "w"), indent=2)

    # Save test predictions for analysis
    best_preds_map = {
        "LightGBM": yp_lgb, "Two-Stage Full": yp_2stage, "Two-Stage Hard": yp_2stage_hard,
        "CatBoost": yp_cb, "XGBoost": yp_xgb,
    }
    if yp_ens is not None:
        best_preds_map["Ensemble"] = yp_ens
    best_preds = best_preds_map.get(best_name, yp_2stage)

    preds_df = pl.DataFrame({
        "true_h": y_test,
        "pred_h": np.clip(best_preds, 0, 200),
        "error_h": y_test - best_preds,
        "abs_error_h": np.abs(y_test - best_preds),
        "dist_to_dest_km": X_num_test[:, numeric_features.index("dist_to_dest_km")],
        "sog": X_num_test[:, numeric_features.index("sog")],
        "eta_naive_h": X_num_test[:, eta_idx],
    })
    preds_df.write_parquet(MODEL_DIR / "test_predictions.parquet")

    print(f"  Saved to {MODEL_DIR}/")
    print(f"\n✓ v7 training complete. Best: {best_name} at {best_mae:.1f}h MAE")


if __name__ == "__main__":
    main()
