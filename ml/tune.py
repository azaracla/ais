"""Hyperparameter tuning with Optuna — LightGBM on log-target.

Tunes: num_leaves, learning_rate, min_data_in_leaf, feature_fraction,
       bagging_fraction, lambda_l1, lambda_l2.
"""

import polars as pl
import numpy as np
from sklearn.metrics import mean_absolute_error
from sklearn.model_selection import TimeSeriesSplit
import lightgbm as lgb
import optuna
from utils import DATA_DIR

DATASET = DATA_DIR / "dataset.parquet"

NUMERIC_FEATURES = [
    "dist_to_dest_km", "sog", "cog", "bearing_offset_deg",
    "vessel_length", "vessel_width", "length_width_ratio",
    "hour_of_day", "day_of_week",
    "avg_sog_1h", "avg_sog_6h", "avg_sog_24h", "sog_trend_1h",
    "eta_naive_h",
]

TARGET = "time_to_arrival_hours"
N_TRIALS = 50


def load_data():
    df = pl.read_parquet(DATASET).sort(["mmsi", "pos_ts"])
    all_numeric = NUMERIC_FEATURES + [TARGET]
    df = df.select(all_numeric + ["ship_type"]).drop_nulls(subset=all_numeric)

    # One-hot ship_type
    ship_types = df["ship_type"].fill_null(-1).cast(pl.Int64)
    type_counts = ship_types.value_counts().sort("count", descending=True)
    top_types = {int(row[0]) for row in type_counts.iter_rows() if row[1] >= 500}

    st_np = ship_types.to_numpy()
    onehot_cols = {}
    for st in sorted(top_types):
        onehot_cols[f"st_{int(st)}"] = (st_np == st).astype(np.float32)
    other_mask = ~np.isin(st_np, list(top_types))
    if other_mask.any():
        onehot_cols["st_other"] = other_mask.astype(np.float32)

    X_num = df.select(NUMERIC_FEATURES).to_numpy().astype(np.float32)
    X = np.column_stack([X_num] + [onehot_cols[c] for c in sorted(onehot_cols.keys())])
    y = df.select(TARGET).to_numpy().ravel().astype(np.float32)
    y_log = np.log1p(y)

    # Split 80/20
    split = int(len(X) * 0.8)
    return X[:split], X[split:], y_log[:split], y_log[split:], y[:split], y[split:]


def objective(trial, X_train, y_train, X_val, y_val, y_val_orig):
    params = {
        "objective": "regression",
        "metric": "rmse",
        "boosting_type": "gbdt",
        "num_leaves": trial.suggest_int("num_leaves", 31, 255),
        "learning_rate": trial.suggest_float("learning_rate", 0.01, 0.1, log=True),
        "feature_fraction": trial.suggest_float("feature_fraction", 0.5, 0.9),
        "bagging_fraction": trial.suggest_float("bagging_fraction", 0.5, 0.9),
        "bagging_freq": trial.suggest_int("bagging_freq", 1, 10),
        "min_data_in_leaf": trial.suggest_int("min_data_in_leaf", 5, 100),
        "lambda_l1": trial.suggest_float("lambda_l1", 1e-4, 10.0, log=True),
        "lambda_l2": trial.suggest_float("lambda_l2", 1e-4, 10.0, log=True),
        "verbose": -1,
        "num_threads": 4,
        "seed": 42,
    }

    # Sample weight: inverse TTA
    weight = 1.0 / (y_val_orig[:len(y_train)] + 1.0) if len(y_train) == len(y_val_orig) else None

    train_data = lgb.Dataset(X_train, label=y_train)
    val_data = lgb.Dataset(X_val, label=y_val, reference=train_data)

    model = lgb.train(
        params,
        train_data,
        num_boost_round=500,
        valid_sets=[val_data],
        callbacks=[lgb.early_stopping(30), lgb.log_evaluation(0)],
    )

    y_pred_log = model.predict(X_val)
    y_pred = np.expm1(y_pred_log)
    y_true = np.expm1(y_val)
    mae = mean_absolute_error(y_true, y_pred)

    # Composite metric: weighted MAE (short horizons matter more)
    weight = 1.0 / (y_true + 1.0)
    weighted_mae = np.average(np.abs(y_true - y_pred), weights=weight)

    trial.set_user_attr("mae", float(mae))
    trial.set_user_attr("weighted_mae", float(weighted_mae))

    return weighted_mae


def main():
    print("=" * 60)
    print("Hyperparameter Tuning — Optuna + LightGBM")
    print(f"  {N_TRIALS} trials")
    print("=" * 60)

    print("\nLoading data ...")
    X_train, X_val, y_train, y_val, y_train_orig, y_val_orig = load_data()
    print(f"  Train: {len(X_train)}  Val: {len(X_val)}")

    # Use subset for faster tuning
    n_tune = min(len(X_train), 50000)
    n_val_tune = min(len(X_val), 20000)
    print(f"  Tuning subset: {n_tune} train, {n_val_tune} val")

    study = optuna.create_study(
        direction="minimize",
        study_name="eta_lgbm_v4",
        storage="sqlite:///ml/data/optuna.db",
        load_if_exists=True,
    )

    study.optimize(
        lambda trial: objective(
            trial,
            X_train[:n_tune], y_train[:n_tune],
            X_val[:n_val_tune], y_val[:n_val_tune],
            y_val_orig[:n_val_tune],
        ),
        n_trials=N_TRIALS,
        show_progress_bar=True,
    )

    print("\n" + "=" * 60)
    print("Best trial:")
    print(f"  Weighted MAE: {study.best_value:.2f}")
    print(f"  MAE:          {study.best_trial.user_attrs['mae']:.1f}h")
    print(f"  Params: {study.best_params}")
    print(f"  Trial #: {study.best_trial.number}")

    # Save best params
    import json
    best_path = DATA_DIR / "best_params.json"
    best_path.write_text(json.dumps(study.best_params, indent=2))
    print(f"\nSaved to {best_path}")


if __name__ == "__main__":
    main()
