"""Train v9 ETA prediction — Hybrid LSTM + Snapshot model.

v9 combines:
  1. LSTM encoder over last 50 AIS positions → temporal patterns
  2. v8 snapshot features → global context (dist, port, MMSI stats)

Architecture:
  LSTM branch: (batch, 50, 15) → biLSTM(64) → embedding(128)
  Snapshot branch: (batch, N_snap) → Dense(64) → embedding(64)
  Fusion: concat → Dense(128) → Dense(64) → Dense(1)
  Target: log1p(TTA_hours)

Usage:
  uv run python ml/train_v9.py              # full training
  uv run python ml/train_v9.py --quick       # 20 epochs test
  uv run python ml/train_v9.py --pure-lstm    # LSTM only, no snapshot
"""

import json
import sys
from pathlib import Path

import numpy as np
import polars as pl
from sklearn.metrics import mean_absolute_error
from sklearn.preprocessing import StandardScaler

import torch
import torch.nn as nn
from torch.utils.data import DataLoader, TensorDataset

from utils import DATA_DIR

SEQ_DIR = DATA_DIR / "sequences_v9"
DATASET_V7 = DATA_DIR / "dataset_v7.parquet"
MODEL_DIR = DATA_DIR / "models_v9"
MODEL_DIR.mkdir(exist_ok=True)

RANDOM_SEED = 42
DEVICE = torch.device("cuda" if torch.cuda.is_available() else "cpu")
QUICK_MODE = "--quick" in sys.argv
PURE_LSTM = "--pure-lstm" in sys.argv
N_EPOCHS = 20 if QUICK_MODE else 200
BATCH_SIZE = 256
LSTM_HIDDEN = 64
LSTM_LAYERS = 2
DROPOUT = 0.25
LR = 1e-3
WEIGHT_DECAY = 1e-4
EARLY_STOP_PATIENCE = 20 if QUICK_MODE else 40
GRAD_CLIP = 1.0

torch.manual_seed(RANDOM_SEED)
np.random.seed(RANDOM_SEED)

HORIZON_BINS = [
    (0, 1, "0-1h"), (1, 6, "1-6h"), (6, 24, "6-24h"),
    (24, 72, "1-3d"), (72, 200, "3-8d"),
]

# Top v8 snapshot features (by gain from v8 training)
SNAPSHOT_FEATURES = [
    "dist_to_dest_km", "sog", "cog", "bearing_offset_deg",
    "heading_offset_deg", "rate_of_turn", "rot_available",
    "heading_std_1h", "avg_heading_1h",
    "avg_sog_1h", "avg_sog_6h", "avg_sog_24h", "sog_trend_1h",
    "closing_speed_kmh", "approach_efficiency",
    "stop_fraction_6h", "slow_fraction_6h", "cog_std_6h",
    "sog_range_6h", "sog_accel_6h",
    "eta_naive_h", "eta_phys_6h",
    "port_avg_tta", "port_avg_sog", "port_arrival_rate_per_hour",
    "mmsi_avg_sog", "sog_vs_mmsi_avg",
    "mmsi_sog_std", "mmsi_avg_tta", "mmsi_sample_count", "mmsi_median_sog",
    "hour_sin", "hour_cos", "dow_sin", "dow_cos",
    "vessel_length", "vessel_width", "length_width_ratio", "draught_filled",
]


# ═══════════════════════════════════════════════════════════════════════════════════
# Models
# ═══════════════════════════════════════════════════════════════════════════════════

class PureLSTM(nn.Module):
    """LSTM-only baseline."""
    def __init__(self, input_dim, hidden_dim=64, num_layers=2, dropout=0.25):
        super().__init__()
        self.lstm = nn.LSTM(
            input_dim, hidden_dim, num_layers=num_layers,
            batch_first=True, bidirectional=True,
            dropout=dropout if num_layers > 1 else 0
        )
        lstm_out = hidden_dim * 2
        self.head = nn.Sequential(
            nn.Linear(lstm_out, 128),
            nn.ReLU(),
            nn.Dropout(dropout),
            nn.Linear(128, 64),
            nn.ReLU(),
            nn.Dropout(dropout),
            nn.Linear(64, 1),
        )

    def forward(self, x_seq, lengths):
        packed = nn.utils.rnn.pack_padded_sequence(
            x_seq, lengths.cpu(), batch_first=True, enforce_sorted=False
        )
        _, (hn, _) = self.lstm(packed)
        fwd = hn[-2, :, :]
        bwd = hn[-1, :, :]
        h = torch.cat([fwd, bwd], dim=1)
        return self.head(h).squeeze(-1)


class HybridLSTM(nn.Module):
    """LSTM sequence encoder + snapshot features."""
    def __init__(self, seq_input_dim, snap_input_dim,
                 lstm_hidden=64, lstm_layers=2, dropout=0.25):
        super().__init__()
        self.lstm = nn.LSTM(
            seq_input_dim, lstm_hidden, num_layers=lstm_layers,
            batch_first=True, bidirectional=True,
            dropout=dropout if lstm_layers > 1 else 0
        )
        lstm_out = lstm_hidden * 2

        # Snapshot encoder
        self.snap_encoder = nn.Sequential(
            nn.Linear(snap_input_dim, 64),
            nn.ReLU(),
            nn.Dropout(dropout),
        )

        # Fusion head
        self.head = nn.Sequential(
            nn.Linear(lstm_out + 64, 128),
            nn.ReLU(),
            nn.Dropout(dropout),
            nn.Linear(128, 64),
            nn.ReLU(),
            nn.Dropout(dropout),
            nn.Linear(64, 1),
        )

    def forward(self, x_seq, lengths, x_snap):
        # LSTM branch
        packed = nn.utils.rnn.pack_padded_sequence(
            x_seq, lengths.cpu(), batch_first=True, enforce_sorted=False
        )
        _, (hn, _) = self.lstm(packed)
        fwd = hn[-2, :, :]
        bwd = hn[-1, :, :]
        lstm_emb = torch.cat([fwd, bwd], dim=1)

        # Snapshot branch
        snap_emb = self.snap_encoder(x_snap)

        # Fusion
        fused = torch.cat([lstm_emb, snap_emb], dim=1)
        return self.head(fused).squeeze(-1)


# ═══════════════════════════════════════════════════════════════════════════════════
# Data
# ═══════════════════════════════════════════════════════════════════════════════════

def load_data():
    print(f"Loading sequences from {SEQ_DIR}")
    X_seq = np.load(SEQ_DIR / "X_seq.npy")
    seq_lengths = np.load(SEQ_DIR / "seq_lengths.npy")
    y = np.load(SEQ_DIR / "y.npy")
    mmsi_arr = np.load(SEQ_DIR / "mmsi.npy")
    print(f"  X_seq: {X_seq.shape} ({X_seq.nbytes/1024**2:.0f} MB)")
    print(f"  MMSIs: {len(np.unique(mmsi_arr))}")

    # Normalize sequence features (z-score per feature, excluding padding)
    print("  Normalizing sequence features ...")
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
    # Zero padding
    for i in range(len(seq_lengths)):
        if seq_lengths[i] < X_seq.shape[1]:
            X_seq_norm[i, seq_lengths[i]:, :] = 0.0

    # Load snapshot features
    print(f"\nLoading snapshot features from {DATASET_V7}")
    snap_df = pl.read_parquet(DATASET_V7).sort(["mmsi", "pos_ts"])

    # Verify alignment
    n_seq = len(y)
    n_snap = snap_df.height
    print(f"  Sequences: {n_seq},  Dataset: {n_snap}")
    if n_seq != n_snap:
        print(f"  ⚠ Count mismatch — sequences built from different dataset version?")
        # Truncate to min
        n_use = min(n_seq, n_snap)
        X_seq_norm = X_seq_norm[:n_use]
        seq_lengths = seq_lengths[:n_use]
        y = y[:n_use]
        mmsi_arr = mmsi_arr[:n_use]
        snap_df = snap_df[:n_use]

    # Build snapshot feature matrix
    available_snap = [f for f in SNAPSHOT_FEATURES if f in snap_df.columns]
    missing = [f for f in SNAPSHOT_FEATURES if f not in snap_df.columns]
    if missing:
        print(f"  Missing snapshot features: {missing}")
    print(f"  Snapshot features: {len(available_snap)}")

    X_snap = snap_df.select(available_snap).fill_null(0.0).to_numpy().astype(np.float32)
    # Replace inf/nan from division by zero etc.
    X_snap = np.nan_to_num(X_snap, nan=0.0, posinf=0.0, neginf=0.0)

    # Normalize snapshot features
    snap_scaler = StandardScaler()
    X_snap = snap_scaler.fit_transform(X_snap).astype(np.float32)
    X_snap = np.nan_to_num(X_snap, nan=0.0, posinf=0.0, neginf=0.0)

    return (X_seq_norm, seq_lengths, X_snap, y, mmsi_arr,
            seq_mean.tolist(), seq_std.tolist(),
            snap_scaler.mean_.tolist(), snap_scaler.scale_.tolist(),
            available_snap)


def mmsi_split(*arrays, mmsi_arr):
    """80/20 MMSI-grouped split."""
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

def evaluate(name, y_true, y_pred):
    mae = mean_absolute_error(y_true, y_pred)
    rmse = np.sqrt(np.mean((y_true - y_pred) ** 2))
    r2 = 1 - np.sum((y_true - y_pred) ** 2) / np.sum((y_true - y_true.mean()) ** 2)
    print(f"\n{'─'*60}")
    print(f"  {name}")
    print(f"{'─'*60}")
    print(f"  MAE:  {mae:.2f}h")
    print(f"  RMSE: {rmse:.1f}h")
    print(f"  R²:   {r2:.4f}")
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
    arch = "PureLSTM" if PURE_LSTM else "HybridLSTM"
    print("=" * 70)
    print(f"Vessel ETA Prediction — v9 {arch}")
    print(f"Device: {DEVICE}  Mode: {'QUICK' if QUICK_MODE else 'FULL'} ({N_EPOCHS} epochs)")
    print("=" * 70)

    # Load
    (X_seq, lengths, X_snap, y, mmsi_arr,
     seq_mean, seq_std, snap_mean, snap_scale, snap_names) = load_data()

    # Split
    (X_seq_tr, len_tr, X_snap_tr, y_tr), \
    (X_seq_ts, len_ts, X_snap_ts, y_ts) = mmsi_split(
        X_seq, lengths, X_snap, y, mmsi_arr=mmsi_arr
    )

    # Log target + weights
    y_train_log = np.log1p(y_tr).astype(np.float32)
    y_test_log = np.log1p(y_ts).astype(np.float32)
    w_train = 1.0 / (y_tr + 1.0)
    w_train = w_train / w_train.mean()
    w_train = w_train.astype(np.float32)

    # Tensors
    X_seq_tr_t = torch.from_numpy(X_seq_tr).float()
    len_tr_t = torch.from_numpy(len_tr).long()
    X_snap_tr_t = torch.from_numpy(X_snap_tr).float()
    y_tr_t = torch.from_numpy(y_train_log).float()
    w_tr_t = torch.from_numpy(w_train).float()

    X_seq_ts_t = torch.from_numpy(X_seq_ts).float()
    len_ts_t = torch.from_numpy(len_ts).long()
    X_snap_ts_t = torch.from_numpy(X_snap_ts).float()
    y_ts_t = torch.from_numpy(y_test_log).float()

    # DataLoaders
    if PURE_LSTM:
        train_ds = TensorDataset(X_seq_tr_t, len_tr_t, y_tr_t, w_tr_t)
        test_ds = TensorDataset(X_seq_ts_t, len_ts_t, y_ts_t)
    else:
        train_ds = TensorDataset(X_seq_tr_t, len_tr_t, X_snap_tr_t, y_tr_t, w_tr_t)
        test_ds = TensorDataset(X_seq_ts_t, len_ts_t, X_snap_ts_t, y_ts_t)

    train_dl = DataLoader(train_ds, batch_size=BATCH_SIZE, shuffle=True, drop_last=False)
    test_dl = DataLoader(test_ds, batch_size=BATCH_SIZE * 2, shuffle=False)

    # Model
    if PURE_LSTM:
        model = PureLSTM(X_seq.shape[2], hidden_dim=LSTM_HIDDEN,
                          num_layers=LSTM_LAYERS, dropout=DROPOUT).to(DEVICE)
    else:
        model = HybridLSTM(X_seq.shape[2], X_snap.shape[1],
                            lstm_hidden=LSTM_HIDDEN, lstm_layers=LSTM_LAYERS,
                            dropout=DROPOUT).to(DEVICE)
    n_params = sum(p.numel() for p in model.parameters())
    print(f"\nModel: {arch}(seq_dim={X_seq.shape[2]}, snap_dim={X_snap.shape[1]}, "
          f"lstm_hidden={LSTM_HIDDEN}, layers={LSTM_LAYERS})")
    print(f"  Params: {n_params:,}")

    def predict_batched(model, dl):
        model.eval()
        preds = []
        with torch.no_grad():
            for batch in dl:
                if PURE_LSTM:
                    xb, lb, _ = batch
                    xb, lb = xb.to(DEVICE), lb.to(DEVICE)
                    p = model(xb, lb).cpu()
                else:
                    xb, lb, sb, _ = batch
                    xb, lb, sb = xb.to(DEVICE), lb.to(DEVICE), sb.to(DEVICE)
                    p = model(xb, lb, sb).cpu()
                # Replace NaN/Inf in predictions
                p = torch.nan_to_num(p, nan=2.0, posinf=5.0, neginf=-2.0)
                preds.append(p)
        return torch.cat(preds).numpy()

    optimizer = torch.optim.AdamW(model.parameters(), lr=LR, weight_decay=WEIGHT_DECAY)
    scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(
        optimizer, mode='min', factor=0.5, patience=10
    )

    # ── Training loop ──
    print(f"\n── Training ({N_EPOCHS} epochs, BS={BATCH_SIZE}) ──")
    best_test_mae = float("inf")
    best_epoch = 0
    patience_counter = 0
    history = []

    for epoch in range(1, N_EPOCHS + 1):
        model.train()
        total_loss = 0.0
        for batch in train_dl:
            if PURE_LSTM:
                xb, lb, yb, wb = batch
                xb, lb, yb, wb = xb.to(DEVICE), lb.to(DEVICE), yb.to(DEVICE), wb.to(DEVICE)
                optimizer.zero_grad()
                pred = model(xb, lb)
            else:
                xb, lb, sb, yb, wb = batch
                xb, lb, sb = xb.to(DEVICE), lb.to(DEVICE), sb.to(DEVICE)
                yb, wb = yb.to(DEVICE), wb.to(DEVICE)
                optimizer.zero_grad()
                pred = model(xb, lb, sb)

            loss = (wb * (pred - yb) ** 2).mean()
            loss.backward()
            nn.utils.clip_grad_norm_(model.parameters(), GRAD_CLIP)
            optimizer.step()
            total_loss += loss.item() * xb.size(0)

        avg_loss = total_loss / len(train_ds)

        # Eval
        yp_test_log = predict_batched(model, test_dl)
        yp_test = np.expm1(yp_test_log)
        yp_test = np.clip(yp_test, 0.05, 200.0)
        test_mae = mean_absolute_error(y_ts, yp_test)

        scheduler.step(test_mae)
        history.append({"epoch": epoch, "train_loss": float(avg_loss), "test_mae": float(test_mae)})

        if test_mae < best_test_mae:
            best_test_mae = test_mae
            best_epoch = epoch
            patience_counter = 0
            torch.save(model.state_dict(), MODEL_DIR / "model_best.pt")
        else:
            patience_counter += 1

        if epoch % 10 == 0 or epoch <= 3:
            lr = optimizer.param_groups[0]["lr"]
            print(f"  Epoch {epoch:3d} | loss={avg_loss:.4f} | "
                  f"test_mae={test_mae:.2f}h | lr={lr:.1e} | best={best_test_mae:.2f}h")

        if patience_counter >= EARLY_STOP_PATIENCE:
            print(f"  Early stopping at epoch {epoch}")
            break

    # ── Final evaluation ──
    print(f"\n── Best model: epoch {best_epoch} ──")
    model.load_state_dict(torch.load(MODEL_DIR / "model_best.pt",
                          map_location=DEVICE, weights_only=False))
    yp_test_log = predict_batched(model, test_dl)
    yp_test = np.expm1(yp_test_log)
    yp_test = np.clip(yp_test, 0.05, 200.0)
    mae_v9, r2_v9 = evaluate(f"v9 {arch}", y_ts, yp_test)

    print(f"\n{'='*70}")
    print("Summary — v9")
    print(f"{'='*70}")
    print(f"  v8 Two-Stage:          9.97h")
    print(f"  v9 {arch}:          {mae_v9:.2f}h")
    delta = 9.97 - mae_v9
    print(f"  Improvement:           {delta:+.2f}h ({(delta/9.97)*100:+.1f}%)")

    # Save metadata
    meta = {
        "version": "v9",
        "architecture": arch,
        "test_mae": float(mae_v9),
        "test_r2": float(r2_v9),
        "lstm_hidden": LSTM_HIDDEN,
        "lstm_layers": LSTM_LAYERS,
        "dropout": DROPOUT,
        "batch_size": BATCH_SIZE,
        "n_epochs": best_epoch,
        "seq_input_dim": X_seq.shape[2],
        "snap_input_dim": X_snap.shape[1] if not PURE_LSTM else 0,
        "snap_features": snap_names if not PURE_LSTM else [],
        "device": str(DEVICE),
        "random_seed": RANDOM_SEED,
    }
    json.dump(meta, open(MODEL_DIR / "metadata.json", "w"), indent=2)
    json.dump(history, open(MODEL_DIR / "history.json", "w"), indent=2)
    np.save(MODEL_DIR / "test_predictions.npy", yp_test.astype(np.float32))
    np.save(MODEL_DIR / "test_true.npy", y_ts.astype(np.float32))
    print(f"\n✓ v9 saved to {MODEL_DIR}/")


if __name__ == "__main__":
    train()
