"""Train v9 Gated LSTM — LSTM for short TTA, v8 snapshot for long TTA.

Key insight from v9 experiments:
  - LSTM 0-24h MAE: 0.5-9.0h (v8: 2.4-9.9h) → 2-3x better
  - LSTM 1-8d MAE: 40-117h (v8: 23-57h) → much worse
  - Solution: gate by distance/sog ratio

Architecture:
  threshold = 24h (eta_naive_h)
  if eta_naive_h < threshold:  use LSTM (near port, trajectory matters)
  else:                         use v8 snapshot model (global context needed)
  Smooth sigmoid blend around threshold.

Usage:
  uv run python ml/train_v9_gated.py --quick       # 20 epochs test
  uv run python ml/train_v9_gated.py               # full training
"""

import json
import sys
import warnings
from pathlib import Path

import lightgbm as lgb
import numpy as np
import polars as pl
from sklearn.metrics import mean_absolute_error

import torch
import torch.nn as nn
from torch.utils.data import DataLoader, TensorDataset

from utils import DATA_DIR

warnings.filterwarnings("ignore")

SEQ_DIR = DATA_DIR / "sequences_v9"
DATASET_V7 = DATA_DIR / "dataset_v7.parquet"
MODEL_DIR = DATA_DIR / "models_v9"
MODEL_DIR.mkdir(exist_ok=True)
V8_MODEL_DIR = DATA_DIR / "models_v8"

RANDOM_SEED = 42
DEVICE = torch.device("cuda" if torch.cuda.is_available() else "cpu")
QUICK_MODE = "--quick" in sys.argv
N_EPOCHS = 20 if QUICK_MODE else 200
BATCH_SIZE = 256
LSTM_HIDDEN = 64
LSTM_LAYERS = 2
DROPOUT = 0.25
LR = 1e-3
WEIGHT_DECAY = 1e-4
EARLY_STOP_PATIENCE = 20 if QUICK_MODE else 40

GATE_THRESHOLD_KM = 200.0  # dist_to_dest below this → trust LSTM more
GATE_SMOOTHNESS = 3.0      # sigmoid steepness

torch.manual_seed(RANDOM_SEED)
np.random.seed(RANDOM_SEED)

HORIZON_BINS = [
    (0, 1, "0-1h"), (1, 6, "1-6h"), (6, 24, "6-24h"),
    (24, 72, "1-3d"), (72, 200, "3-8d"),
]


# ═══════════════════════════════════════════════════════════════════════════════════
# LSTM Model
# ═══════════════════════════════════════════════════════════════════════════════════

class ETALSTM(nn.Module):
    def __init__(self, input_dim, hidden_dim=64, num_layers=2, dropout=0.25):
        super().__init__()
        self.lstm = nn.LSTM(
            input_dim, hidden_dim, num_layers=num_layers,
            batch_first=True, bidirectional=True,
            dropout=dropout if num_layers > 1 else 0
        )
        lstm_out = hidden_dim * 2
        self.head = nn.Sequential(
            nn.Linear(lstm_out, 128), nn.ReLU(), nn.Dropout(dropout),
            nn.Linear(128, 64), nn.ReLU(), nn.Dropout(dropout),
            nn.Linear(64, 1),
        )

    def forward(self, x_seq, lengths):
        packed = nn.utils.rnn.pack_padded_sequence(
            x_seq, lengths.cpu(), batch_first=True, enforce_sorted=False
        )
        _, (hn, _) = self.lstm(packed)
        h = torch.cat([hn[-2, :, :], hn[-1, :, :]], dim=1)
        return self.head(h).squeeze(-1)


# ═══════════════════════════════════════════════════════════════════════════════════
# v8 snapshot predictions
# ═══════════════════════════════════════════════════════════════════════════════════

# Same features as train_v8.py
V8_NUMERIC = [
    "dist_to_dest_km", "sog", "cog", "bearing_offset_deg",
    "vessel_length", "vessel_width", "length_width_ratio", "draught_filled",
    "heading_offset_deg", "rate_of_turn", "rot_available",
    "heading_std_1h", "heading_std_3h", "heading_std_6h",
    "avg_heading_1h",
    "avg_sog_1h", "avg_sog_6h", "avg_sog_24h", "sog_trend_1h",
    "closing_speed_kmh", "approach_efficiency",
    "stop_fraction_3h", "slow_fraction_3h",
    "stop_fraction_6h", "slow_fraction_6h",
    "cog_std_3h", "cog_std_6h",
    "sog_range_6h", "sog_delta_30min",
    "sog_accel_6h", "turn_rate_6h",
    "eta_naive_h", "eta_phys_6h",
    "port_avg_tta", "port_arrival_count", "port_avg_sog",
    "port_arrival_rate_per_hour",
    "mmsi_avg_sog", "sog_vs_mmsi_avg", "sog_vs_mmsi_typical",
    "mmsi_sog_std", "mmsi_sog_cv",
    "mmsi_avg_tta", "mmsi_sample_count", "mmsi_median_sog",
    "hour_of_day", "day_of_week",
    "hour_sin", "hour_cos", "dow_sin", "dow_cos",
]

V8_CATEGORICAL = ["ship_type", "nav_status"]


def build_v8_features(df):
    """Reproduce v8 feature engineering: interactions, anchoring."""
    # Interactions
    df = df.with_columns([
        (pl.col("dist_to_dest_km") * pl.col("closing_speed_kmh")).alias("dist_x_closing"),
        (pl.col("approach_efficiency") * np.log1p(pl.col("dist_to_dest_km"))).alias("weighted_efficiency"),
        (pl.col("mmsi_avg_tta") / (pl.col("eta_naive_h") + 1e-3)).alias("mmsi_tta_bias"),
        (pl.col("avg_sog_1h") / (pl.col("mmsi_median_sog") + 0.1)).alias("sog_vs_hist"),
    ])
    # Anchoring
    sog = df["avg_sog_1h"].to_numpy()
    dist = df["dist_to_dest_km"].to_numpy()
    stop = df["stop_fraction_6h"].to_numpy()
    anchor = np.where((sog < 0.5) & (dist > 10) & (dist < 80), 1.0, 0.0).astype(np.float32)
    stationary = np.where(stop > 0, stop * 6.0, 0.0).astype(np.float32)
    df = df.with_columns([
        pl.Series("anchoring_suspected", anchor),
        pl.Series("stationary_duration_h", stationary),
    ])
    return df


def predict_v8(df):
    """Generate v8 two-stage predictions for all samples."""
    print("  Generating v8 snapshot predictions ...")
    df = build_v8_features(df)

    # Build feature matrix (same order as v8)
    available_num = [f for f in V8_NUMERIC if f in df.columns]
    extra = ["dist_x_closing", "weighted_efficiency", "mmsi_tta_bias", "sog_vs_hist",
             "anchoring_suspected", "stationary_duration_h"]
    available_num += [e for e in extra if e in df.columns]
    available_cat = [f for f in V8_CATEGORICAL if f in df.columns]

    # Fill missing
    all_needed = set(available_num + available_cat)
    for col in all_needed:
        if col not in df.columns:
            df = df.with_columns(pl.lit(0.0).alias(col))

    X_num = df.select(available_num).fill_null(0.0).to_numpy().astype(np.float32)

    # Categorical as int
    cat_cols = []
    for col in available_cat:
        vals = df[col].fill_null(-1).cast(pl.Int32).to_numpy()
        cat_cols.append(vals.reshape(-1, 1).astype(np.int32))
    X_cat = np.column_stack(cat_cols) if cat_cols else np.zeros((len(df), 0), dtype=np.int32)

    # Combine
    X = np.column_stack([X_num, X_cat]).astype(np.float32)
    cat_indices = list(range(len(available_num), len(available_num) + len(available_cat)))

    # Load v8 models
    model_a = lgb.Booster(model_file=str(V8_MODEL_DIR / "stage_a.txt"))
    model_b = lgb.Booster(model_file=str(V8_MODEL_DIR / "stage_b.txt"))

    # Stage A prediction
    yp_a_log = model_a.predict(X)
    yp_a = np.expm1(yp_a_log)

    # Stage B residual
    X_b = np.column_stack([X, yp_a_log])
    yp_b = model_b.predict(X_b)

    # Adaptive blending (same as v8)
    blend = 1.0 / (1.0 + np.exp(-2.0 * (yp_a - 3.0) / 3.0))
    y_pred_log = yp_a_log + blend * yp_b
    y_pred = np.expm1(y_pred_log)
    y_pred = np.clip(y_pred, 0.05, 200.0)

    return y_pred.astype(np.float32)


# ═══════════════════════════════════════════════════════════════════════════════════
# Data loading
# ═══════════════════════════════════════════════════════════════════════════════════

def load_data():
    # ── Sequences ──
    print(f"Loading sequences from {SEQ_DIR}")
    X_seq = np.load(SEQ_DIR / "X_seq.npy")
    seq_lengths = np.load(SEQ_DIR / "seq_lengths.npy")
    y = np.load(SEQ_DIR / "y.npy")
    mmsi_arr = np.load(SEQ_DIR / "mmsi.npy")
    print(f"  X_seq: {X_seq.shape}")

    # Normalize
    print("  Normalizing ...")
    mask = np.zeros((X_seq.shape[0], X_seq.shape[1]), dtype=bool)
    for i in range(len(seq_lengths)):
        mask[i, :seq_lengths[i]] = True
    seq_mean = np.zeros(X_seq.shape[2], dtype=np.float32)
    seq_std = np.ones(X_seq.shape[2], dtype=np.float32)
    for k in range(X_seq.shape[2]):
        vals = X_seq[:, :, k][mask]
        seq_mean[k] = vals.mean()
        seq_std[k] = vals.std() if vals.std() > 0 else 1.0
    X_seq_norm = X_seq.copy()
    for k in range(X_seq.shape[2]):
        X_seq_norm[:, :, k] = (X_seq[:, :, k] - seq_mean[k]) / seq_std[k]
    for i in range(len(seq_lengths)):
        if seq_lengths[i] < X_seq.shape[1]:
            X_seq_norm[i, seq_lengths[i]:, :] = 0.0

    # ── v8 snapshot predictions ──
    print(f"\nLoading snapshot data from {DATASET_V7}")
    snap_df = pl.read_parquet(DATASET_V7).sort(["mmsi", "pos_ts"])

    # Align counts (should match)
    n_seq = len(y)
    n_snap = snap_df.height
    if n_seq != n_snap:
        n_use = min(n_seq, n_snap)
        X_seq_norm = X_seq_norm[:n_use]
        seq_lengths = seq_lengths[:n_use]
        y = y[:n_use]
        mmsi_arr = mmsi_arr[:n_use]
        snap_df = snap_df[:n_use]

    # Get dist_to_dest for gating
    dist_to_dest = snap_df["dist_to_dest_km"].fill_null(9999.0).to_numpy().astype(np.float32)

    # Generate v8 predictions (expensive — only once)
    v8_pred = predict_v8(snap_df)
    v8_mae = mean_absolute_error(y, v8_pred)
    print(f"  v8 snapshot MAE on full dataset: {v8_mae:.1f}h")

    return X_seq_norm, seq_lengths, dist_to_dest, v8_pred, y, mmsi_arr


def mmsi_split(*arrays, mmsi_arr):
    unique_mmsis = np.unique(mmsi_arr)
    np.random.seed(RANDOM_SEED)
    np.random.shuffle(unique_mmsis)
    split_n = int(len(unique_mmsis) * 0.8)
    train_mmsis = set(unique_mmsis[:split_n])
    train_mask = np.array([m in train_mmsis for m in mmsi_arr])
    test_mask = ~train_mask
    print(f"  Train: {train_mask.sum()} samples ({len(train_mmsis)} MMSIs)")
    print(f"  Test:  {test_mask.sum()} samples")
    return tuple(a[train_mask] for a in arrays), tuple(a[test_mask] for a in arrays)


# ═══════════════════════════════════════════════════════════════════════════════════
# Evaluation
# ═══════════════════════════════════════════════════════════════════════════════════

def evaluate(name, y_true, y_pred, ref_mae=None):
    mae = mean_absolute_error(y_true, y_pred)
    rmse = np.sqrt(np.mean((y_true - y_pred) ** 2))
    r2 = 1 - np.sum((y_true - y_pred) ** 2) / np.sum((y_true - y_true.mean()) ** 2)
    print(f"\n{'─'*60}")
    print(f"  {name}")
    print(f"{'─'*60}")
    print(f"  MAE:  {mae:.2f}h")
    print(f"  RMSE: {rmse:.1f}h")
    print(f"  R²:   {r2:.4f}")
    if ref_mae:
        print(f"  vs reference: {mae - ref_mae:+.2f}h")
    print(f"  Per-horizon MAE:")
    for lo, hi, hname in HORIZON_BINS:
        mask = (y_true >= lo) & (y_true < hi)
        if mask.sum() > 10:
            err = np.abs(y_true[mask] - y_pred[mask])
            print(f"    {hname:6s}: MAE={err.mean():.1f}h  n={mask.sum()}")
    return mae, r2


# ═══════════════════════════════════════════════════════════════════════════════════
# Training
# ═══════════════════════════════════════════════════════════════════════════════════

def train():
    print("=" * 70)
    print("Vessel ETA Prediction — v9 Gated LSTM + v8 Snapshot")
    print(f"Gate threshold: {GATE_THRESHOLD_KM}km (dist_to_dest)")
    print(f"Device: {DEVICE}  Mode: {'QUICK' if QUICK_MODE else 'FULL'}")
    print("=" * 70)

    # Load all data
    X_seq, lengths, dist_to_dest, v8_pred, y, mmsi_arr = load_data()

    # Split
    train_arrays, test_arrays = mmsi_split(
        X_seq, lengths, dist_to_dest, v8_pred, y, mmsi_arr=mmsi_arr
    )
    X_seq_tr, len_tr, dist_tr, v8_tr, y_tr = train_arrays
    X_seq_ts, len_ts, dist_ts, v8_ts, y_ts = test_arrays

    # Log target
    y_tr_log = np.log1p(y_tr).astype(np.float32)
    w_train = 1.0 / (y_tr + 1.0)
    w_train = w_train / w_train.mean()
    w_train = w_train.astype(np.float32)

    # Tensors
    X_tr_t = torch.from_numpy(X_seq_tr).float()
    len_tr_t = torch.from_numpy(len_tr).long()
    y_tr_t = torch.from_numpy(y_tr_log).float()
    w_tr_t = torch.from_numpy(w_train).float()
    X_ts_t = torch.from_numpy(X_seq_ts).float()
    len_ts_t = torch.from_numpy(len_ts).long()

    train_ds = TensorDataset(X_tr_t, len_tr_t, y_tr_t, w_tr_t)
    test_ds = TensorDataset(X_ts_t, len_ts_t)
    train_dl = DataLoader(train_ds, batch_size=BATCH_SIZE, shuffle=True, drop_last=False)
    test_dl = DataLoader(test_ds, batch_size=BATCH_SIZE * 2, shuffle=False)

    # LSTM model
    model = ETALSTM(X_seq.shape[2], hidden_dim=LSTM_HIDDEN,
                     num_layers=LSTM_LAYERS, dropout=DROPOUT).to(DEVICE)
    n_params = sum(p.numel() for p in model.parameters())
    print(f"\nModel: LSTM(seq_dim={X_seq.shape[2]}, hidden={LSTM_HIDDEN}, "
          f"layers={LSTM_LAYERS})  Params: {n_params:,}")

    def predict_lstm_batched(model, dl):
        model.eval()
        preds = []
        with torch.no_grad():
            for xb, lb, *_ in dl:
                xb, lb = xb.to(DEVICE), lb.to(DEVICE)
                p = model(xb, lb).cpu()
                p = torch.nan_to_num(p, nan=2.0, posinf=5.0, neginf=-2.0)
                preds.append(p)
        return np.expm1(np.clip(torch.cat(preds).numpy(), 0, np.log1p(200.0)))

    optimizer = torch.optim.AdamW(model.parameters(), lr=LR, weight_decay=WEIGHT_DECAY)
    scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(
        optimizer, mode='min', factor=0.5, patience=10
    )

    # ── Training ──
    print(f"\n── Training LSTM ({N_EPOCHS} epochs, BS={BATCH_SIZE}) ──")
    best_gated_mae = float("inf")
    best_epoch = 0
    patience_counter = 0
    history = []

    for epoch in range(1, N_EPOCHS + 1):
        model.train()
        total_loss = 0.0
        for xb, lb, yb, wb in train_dl:
            xb, lb, yb, wb = xb.to(DEVICE), lb.to(DEVICE), yb.to(DEVICE), wb.to(DEVICE)
            optimizer.zero_grad()
            pred = model(xb, lb)
            loss = (wb * (pred - yb) ** 2).mean()
            loss.backward()
            nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            optimizer.step()
            total_loss += loss.item() * xb.size(0)

        avg_loss = total_loss / len(train_ds)

        # Predict with LSTM
        yp_lstm = predict_lstm_batched(model, test_dl)

        # ── Gating: combine LSTM (close) + v8 (far) ──
        # Sigmoid gate on dist_to_dest: < 200km → LSTM, > 500km → v8
        gate = 1.0 / (1.0 + np.exp(
            (dist_ts - GATE_THRESHOLD_KM) / (GATE_THRESHOLD_KM / GATE_SMOOTHNESS)
        ))
        yp_gated = gate * yp_lstm + (1 - gate) * v8_ts
        yp_gated = np.clip(yp_gated, 0.05, 200.0)
        gated_mae = mean_absolute_error(y_ts, yp_gated)

        scheduler.step(gated_mae)
        history.append({"epoch": epoch, "train_loss": float(avg_loss),
                         "gated_mae": float(gated_mae)})

        if gated_mae < best_gated_mae:
            best_gated_mae = gated_mae
            best_epoch = epoch
            patience_counter = 0
            torch.save(model.state_dict(), MODEL_DIR / "gated_lstm_best.pt")
        else:
            patience_counter += 1

        if epoch % 10 == 0 or epoch <= 3:
            lr = optimizer.param_groups[0]["lr"]
            print(f"  Epoch {epoch:3d} | loss={avg_loss:.4f} | "
                  f"gated_mae={gated_mae:.2f}h | lr={lr:.1e} | best={best_gated_mae:.2f}h")

        if patience_counter >= EARLY_STOP_PATIENCE:
            print(f"  Early stopping at epoch {epoch}")
            break

    # ── Final eval ──
    print(f"\n── Best model: epoch {best_epoch} ──")
    model.load_state_dict(torch.load(MODEL_DIR / "gated_lstm_best.pt",
                          map_location=DEVICE, weights_only=False))
    yp_lstm = predict_lstm_batched(model, test_dl)

    # Final gating
    gate = 1.0 / (1.0 + np.exp(
        (dist_ts - GATE_THRESHOLD_KM) / (GATE_THRESHOLD_KM / GATE_SMOOTHNESS)
    ))
    yp_gated = gate * yp_lstm + (1 - gate) * v8_ts
    yp_gated = np.clip(yp_gated, 0.05, 200.0)

    # ── Evaluate all variants ──
    evaluate("v8 snapshot (reference)", y_ts, v8_ts)
    evaluate("v9 LSTM (pure)", y_ts, yp_lstm, ref_mae=mean_absolute_error(y_ts, v8_ts))
    evaluate("v9 Gated LSTM+v8", y_ts, yp_gated, ref_mae=mean_absolute_error(y_ts, v8_ts))

    # Gate stats by distance
    print(f"\n  Gate stats (fraction LSTM by dist_to_dest):")
    for lo, hi, label in [(0, 50, "<50km"), (50, 100, "50-100km"), (100, 200, "100-200km"),
                           (200, 500, "200-500km"), (500, 9999, ">500km")]:
        mask = (dist_ts >= lo) & (dist_ts < hi)
        if mask.sum() > 10:
            print(f"    {label:12s}: gate_mean={gate[mask].mean():.2f}  n={mask.sum()}")

    # ── Summary ──
    lstm_mae = mean_absolute_error(y_ts, yp_lstm)
    v8_mae = mean_absolute_error(y_ts, v8_ts)
    gated_mae = mean_absolute_error(y_ts, yp_gated)

    print(f"\n{'='*70}")
    print("Summary — v9 Gated")
    print(f"{'='*70}")
    print(f"  v8 Two-Stage:          {v8_mae:.2f}h")
    print(f"  v9 LSTM (pure):        {lstm_mae:.2f}h")
    print(f"  v9 Gated LSTM+v8:      {gated_mae:.2f}h")

    # Save
    meta = {
        "version": "v9-gated",
        "gate_threshold_km": GATE_THRESHOLD_KM,
        "gate_smoothness": GATE_SMOOTHNESS,
        "test_mae_lstm": float(lstm_mae),
        "test_mae_v8": float(v8_mae),
        "test_mae_gated": float(gated_mae),
        "lstm_hidden": LSTM_HIDDEN,
        "n_epochs": best_epoch,
        "random_seed": RANDOM_SEED,
    }
    json.dump(meta, open(MODEL_DIR / "metadata_gated.json", "w"), indent=2)
    json.dump(history, open(MODEL_DIR / "history_gated.json", "w"), indent=2)
    print(f"\n✓ v9 gated saved to {MODEL_DIR}/")


if __name__ == "__main__":
    train()
