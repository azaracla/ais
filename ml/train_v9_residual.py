"""Train v9 Residual LSTM — predict correction to eta_naive_h baseline.

Key insight from v9 quick tests:
  - LSTM excels at short horizons (0.5h MAE at 0-1h vs v8's 2.4h)
  - LSTM fails at long horizons (110h at 3-8d vs v8's 57h)
  - Eta_naive_h (dist/sog) is decent at long horizons but noisy at short

Solution: LSTM predicts the residual from eta_naive_h.
  - Near port: LSTM sees deceleration → large correction → good prediction
  - Far from port: LSTM lacks signal → small correction → falls back to eta_naive_h

Architecture:
  baseline = log1p(eta_naive_h)
  residual = LSTM(sequence) → correction in log space
  prediction = expm1(baseline + residual)

Usage:
  uv run python ml/train_v9_residual.py              # full training
  uv run python ml/train_v9_residual.py --quick       # 20 epochs test
"""

import json
import sys
from pathlib import Path

import numpy as np
import polars as pl
from sklearn.metrics import mean_absolute_error

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


# ═══════════════════════════════════════════════════════════════════════════════════
# Model
# ═══════════════════════════════════════════════════════════════════════════════════

class ResidualLSTM(nn.Module):
    """LSTM predicts residual from log(eta_naive_h) baseline."""
    def __init__(self, input_dim, hidden_dim=64, num_layers=2, dropout=0.25):
        super().__init__()
        self.lstm = nn.LSTM(
            input_dim, hidden_dim, num_layers=num_layers,
            batch_first=True, bidirectional=True,
            dropout=dropout if num_layers > 1 else 0
        )
        lstm_out = hidden_dim * 2
        self.head = nn.Sequential(
            nn.Linear(lstm_out + 1, 128),  # +1 for baseline
            nn.ReLU(),
            nn.Dropout(dropout),
            nn.Linear(128, 64),
            nn.ReLU(),
            nn.Dropout(dropout),
            nn.Linear(64, 1),
        )

    def forward(self, x_seq, lengths, baseline):
        # x_seq: (batch, seq_len, input_dim)
        # baseline: (batch,) — log1p(eta_naive_h)
        packed = nn.utils.rnn.pack_padded_sequence(
            x_seq, lengths.cpu(), batch_first=True, enforce_sorted=False
        )
        _, (hn, _) = self.lstm(packed)
        fwd = hn[-2, :, :]
        bwd = hn[-1, :, :]
        h = torch.cat([fwd, bwd], dim=1)  # (batch, hidden*2)

        # Concatenate baseline as a feature
        h = torch.cat([h, baseline.unsqueeze(-1)], dim=1)
        return self.head(h).squeeze(-1)


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

    # Normalize sequence features
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
    for i in range(len(seq_lengths)):
        if seq_lengths[i] < X_seq.shape[1]:
            X_seq_norm[i, seq_lengths[i]:, :] = 0.0

    # Load eta_naive_h as baseline
    print(f"  Loading eta_naive_h from {DATASET_V7}")
    snap_df = pl.read_parquet(DATASET_V7).sort(["mmsi", "pos_ts"])
    n_snap = snap_df.height
    n_seq = len(y)
    if n_seq != n_snap:
        n_use = min(n_seq, n_snap)
        X_seq_norm = X_seq_norm[:n_use]
        seq_lengths = seq_lengths[:n_use]
        y = y[:n_use]
        mmsi_arr = mmsi_arr[:n_use]
        snap_df = snap_df[:n_use]

    eta_naive = snap_df["eta_naive_h"].fill_null(0.0).to_numpy().astype(np.float32)
    eta_naive = np.clip(eta_naive, 0.05, 200.0)

    # Baseline in log space
    baseline = np.log1p(eta_naive).astype(np.float32)

    # Target residual: log1p(true_tta) - log1p(eta_naive)
    y_log = np.log1p(y).astype(np.float32)
    residual = y_log - baseline

    print(f"  Eta naive MAE: {mean_absolute_error(y, eta_naive):.1f}h")
    print(f"  Residual mean: {residual.mean():.3f}  std: {residual.std():.3f}")
    print(f"  Residual range: [{residual.min():.2f}, {residual.max():.2f}]")

    return X_seq_norm, seq_lengths, baseline, residual, y, mmsi_arr


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

def evaluate(name, y_true, y_pred, baseline_h=None):
    mae = mean_absolute_error(y_true, y_pred)
    rmse = np.sqrt(np.mean((y_true - y_pred) ** 2))
    r2 = 1 - np.sum((y_true - y_pred) ** 2) / np.sum((y_true - y_true.mean()) ** 2)
    print(f"\n{'─'*60}")
    print(f"  {name}")
    print(f"{'─'*60}")
    print(f"  MAE:  {mae:.2f}h")
    print(f"  RMSE: {rmse:.1f}h")
    print(f"  R²:   {r2:.4f}")
    if baseline_h is not None:
        bl_mae = mean_absolute_error(y_true, baseline_h)
        print(f"  Baseline (eta_naive) MAE: {bl_mae:.1f}h")
        print(f"  Improvement over baseline: {(1-mae/bl_mae)*100:.1f}%")
    print(f"  Per-horizon MAE:")
    for lo, hi, hname in HORIZON_BINS:
        mask = (y_true >= lo) & (y_true < hi)
        if mask.sum() > 10:
            err = np.abs(y_true[mask] - y_pred[mask])
            bl_err = np.abs(y_true[mask] - baseline_h[mask]) if baseline_h is not None else None
            bl_str = f" (baseline {bl_err.mean():.1f}h)" if bl_err is not None else ""
            print(f"    {hname:6s}: MAE={err.mean():.1f}h  n={mask.sum()}{bl_str}")
    return mae, r2


# ═══════════════════════════════════════════════════════════════════════════════════
# Training
# ═══════════════════════════════════════════════════════════════════════════════════

def train():
    print("=" * 70)
    print("Vessel ETA Prediction — v9 Residual LSTM")
    print(f"Device: {DEVICE}  Mode: {'QUICK' if QUICK_MODE else 'FULL'} ({N_EPOCHS} epochs)")
    print("=" * 70)

    # Load
    X_seq, lengths, baseline, residual, y, mmsi_arr = load_data()

    # Split
    (X_seq_tr, len_tr, bl_tr, res_tr, y_tr), \
    (X_seq_ts, len_ts, bl_ts, res_ts, y_ts) = mmsi_split(
        X_seq, lengths, baseline, residual, y, mmsi_arr=mmsi_arr
    )

    # Sample weights
    w_train = 1.0 / (y_tr + 1.0)
    w_train = w_train / w_train.mean()
    w_train = w_train.astype(np.float32)

    # Tensors
    X_tr_t = torch.from_numpy(X_seq_tr).float()
    len_tr_t = torch.from_numpy(len_tr).long()
    bl_tr_t = torch.from_numpy(bl_tr).float()
    res_tr_t = torch.from_numpy(res_tr).float()
    w_tr_t = torch.from_numpy(w_train).float()

    X_ts_t = torch.from_numpy(X_seq_ts).float()
    len_ts_t = torch.from_numpy(len_ts).long()
    bl_ts_t = torch.from_numpy(bl_ts).float()
    res_ts_t = torch.from_numpy(res_ts).float()

    # DataLoaders
    train_ds = TensorDataset(X_tr_t, len_tr_t, bl_tr_t, res_tr_t, w_tr_t)
    test_ds = TensorDataset(X_ts_t, len_ts_t, bl_ts_t, res_ts_t)
    train_dl = DataLoader(train_ds, batch_size=BATCH_SIZE, shuffle=True, drop_last=False)
    test_dl = DataLoader(test_ds, batch_size=BATCH_SIZE * 2, shuffle=False)

    # Model
    model = ResidualLSTM(X_seq.shape[2], hidden_dim=LSTM_HIDDEN,
                          num_layers=LSTM_LAYERS, dropout=DROPOUT).to(DEVICE)
    n_params = sum(p.numel() for p in model.parameters())
    print(f"\nModel: ResidualLSTM(seq_dim={X_seq.shape[2]}, "
          f"hidden={LSTM_HIDDEN}, layers={LSTM_LAYERS})")
    print(f"  Params: {n_params:,}")

    def predict_batched(model, dl):
        model.eval()
        preds_res = []
        preds_bl = []
        with torch.no_grad():
            for xb, lb, blb, _ in dl:
                xb, lb, blb = xb.to(DEVICE), lb.to(DEVICE), blb.to(DEVICE)
                res = model(xb, lb, blb).cpu()
                res = torch.nan_to_num(res, nan=0.0, posinf=3.0, neginf=-3.0)
                preds_res.append(res)
                preds_bl.append(blb.cpu())
        res_all = torch.cat(preds_res).numpy()
        bl_all = torch.cat(preds_bl).numpy()
        # Final prediction: expm1(baseline + residual)
        y_pred_log = bl_all + res_all
        return np.expm1(np.clip(y_pred_log, 0, np.log1p(200.0)))

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
        for xb, lb, blb, resb, wb in train_dl:
            xb, lb, blb, resb, wb = (
                xb.to(DEVICE), lb.to(DEVICE), blb.to(DEVICE),
                resb.to(DEVICE), wb.to(DEVICE)
            )
            optimizer.zero_grad()
            pred_res = model(xb, lb, blb)
            loss = (wb * (pred_res - resb) ** 2).mean()
            loss.backward()
            nn.utils.clip_grad_norm_(model.parameters(), GRAD_CLIP)
            optimizer.step()
            total_loss += loss.item() * xb.size(0)

        avg_loss = total_loss / len(train_ds)

        # Eval
        yp_test = predict_batched(model, test_dl)
        yp_test = np.clip(yp_test, 0.05, 200.0)
        test_mae = mean_absolute_error(y_ts, yp_test)

        scheduler.step(test_mae)
        history.append({"epoch": epoch, "train_loss": float(avg_loss), "test_mae": float(test_mae)})

        if test_mae < best_test_mae:
            best_test_mae = test_mae
            best_epoch = epoch
            patience_counter = 0
            torch.save(model.state_dict(), MODEL_DIR / "residual_lstm_best.pt")
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
    model.load_state_dict(torch.load(MODEL_DIR / "residual_lstm_best.pt",
                          map_location=DEVICE, weights_only=False))
    yp_test = predict_batched(model, test_dl)
    yp_test = np.clip(yp_test, 0.05, 200.0)

    # Baseline (eta_naive) for reference
    baseline_h = np.expm1(bl_ts)
    mae_v9, r2_v9 = evaluate("v9 Residual LSTM", y_ts, yp_test, baseline_h)

    print(f"\n{'='*70}")
    print("Summary — v9 Residual LSTM")
    print(f"{'='*70}")
    print(f"  eta_naive (dist/sog):   {mean_absolute_error(y_ts, baseline_h):.1f}h")
    print(f"  v8 Two-Stage:           9.97h")
    print(f"  v9 Residual LSTM:       {mae_v9:.2f}h")

    # Save
    meta = {
        "version": "v9-residual",
        "architecture": "ResidualLSTM",
        "test_mae": float(mae_v9),
        "test_r2": float(r2_v9),
        "baseline": "eta_naive_h (log1p)",
        "lstm_hidden": LSTM_HIDDEN,
        "lstm_layers": LSTM_LAYERS,
        "batch_size": BATCH_SIZE,
        "n_epochs": best_epoch,
        "random_seed": RANDOM_SEED,
    }
    json.dump(meta, open(MODEL_DIR / "metadata_residual.json", "w"), indent=2)
    json.dump(history, open(MODEL_DIR / "history_residual.json", "w"), indent=2)
    np.save(MODEL_DIR / "test_predictions_residual.npy", yp_test.astype(np.float32))
    np.save(MODEL_DIR / "test_true_residual.npy", y_ts.astype(np.float32))
    print(f"\n✓ v9 residual saved to {MODEL_DIR}/")


if __name__ == "__main__":
    train()
