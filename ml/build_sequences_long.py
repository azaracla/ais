"""Build long-sequence dataset (7-day window, 100 positions) for v9 LSTM.

Key difference from build_sequences.py:
  - 168h window (7 days) instead of 24h — captures full journey patterns
  - 100 positions instead of 50 — with temporal downsampling for efficiency
  - Downsampling: if a sample has >100 positions in the window, take evenly-spaced
    positions to maintain coverage across the full time range

Usage:
  uv run python ml/build_sequences_long.py              # full build
  uv run python ml/build_sequences_long.py --sample 500   # test subset
"""

import sys
import duckdb
import numpy as np
import polars as pl
import json
from pathlib import Path

from utils import DATA_DIR

DATASET = DATA_DIR / "dataset_v7.parquet"
POSITIONS = DATA_DIR / "positions_filtered.parquet"
PORTS = DATA_DIR / "ports.parquet"
OUTPUT_DIR = DATA_DIR / "sequences_v9_long"
OUTPUT_DIR.mkdir(exist_ok=True)

MAX_SEQ_LEN = 100
SEQ_WINDOW_H = 168  # 7 days

SAMPLE_N = None
for arg in sys.argv:
    if arg == "--sample" or arg.startswith("--sample="):
        idx = sys.argv.index(arg)
        if "=" in arg:
            SAMPLE_N = int(arg.split("=")[1])
        elif idx + 1 < len(sys.argv):
            SAMPLE_N = int(sys.argv[idx + 1])
        break


def build():
    print("=" * 70)
    print("Building v9 LONG Sequence Dataset")
    print(f"  Max seq len: {MAX_SEQ_LEN}, window: {SEQ_WINDOW_H}h (7 days)")
    if SAMPLE_N:
        print(f"  TEST MODE: {SAMPLE_N} samples")
    print("=" * 70)

    con = duckdb.connect()

    # ── Load samples ──
    print("\n── Loading samples ──")
    con.execute(f"CREATE TABLE samples AS SELECT * FROM read_parquet('{DATASET}')")
    n_all = con.execute("SELECT count(*) FROM samples").fetchone()[0]

    if SAMPLE_N:
        mmsis = con.execute(
            "SELECT DISTINCT mmsi FROM samples USING SAMPLE 200"
        ).fetchall()
        mmsi_list = ",".join(str(r[0]) for r in mmsis)
        con.execute(f"""
            CREATE TABLE samples_sub AS
            SELECT * FROM samples WHERE mmsi IN ({mmsi_list})
        """)
        con.execute("DROP TABLE samples")
        con.execute("ALTER TABLE samples_sub RENAME TO samples")
    n = con.execute("SELECT count(*) FROM samples").fetchone()[0]
    print(f"  {n} samples")

    # ── Join samples with ports ──
    print("── Joining with ports ──")
    con.execute(f"""
        CREATE TABLE samples_port AS
        SELECT s.*, p.lat AS port_lat, p.lon AS port_lon
        FROM samples s
        LEFT JOIN read_parquet('{PORTS}') p ON s.port_lo_code = p.lo_code
    """)

    # ── Range join with temporal downsampling ──
    # Strategy: join all positions in the 7-day window, then downsample.
    # To avoid excessive memory, we use a 2-step approach:
    # 1. Join positions in window
    # 2. Apply stride-based downsampling
    print(f"\n── Range-joining ([-{SEQ_WINDOW_H}h, 0h]) with downsampling ──")

    con.execute(f"""
        CREATE TABLE seq_all AS
        SELECT
            s.mmsi, s.pos_ts AS snapshot_ts, s.arrival_ts,
            s.time_to_arrival_hours AS tta_h,
            p.ts AS p_ts, p.lat, p.lon, p.sog, p.cog,
            p.true_heading, p.navigational_status, p.rate_of_turn,
            -- hours before snapshot
            EXTRACT(EPOCH FROM s.pos_ts - p.ts) / 3600.0 AS hours_before,
            -- Position count per sample
            COUNT(*) OVER (PARTITION BY s.mmsi, s.pos_ts) AS total_positions,
            -- Row number (DESC = closest first)
            ROW_NUMBER() OVER (
                PARTITION BY s.mmsi, s.pos_ts ORDER BY p.ts DESC
            ) AS rn_desc
        FROM samples_port s
        JOIN read_parquet('{POSITIONS}') p
            ON s.mmsi = p.mmsi
            AND p.ts >= s.pos_ts - INTERVAL '{SEQ_WINDOW_H}' HOUR
            AND p.ts <= s.pos_ts
    """)
    n_all_pos = con.execute("SELECT count(*) FROM seq_all").fetchone()[0]
    print(f"  Total positions joined: {n_all_pos} ({n_all_pos/n:.0f} per sample)")

    # ── Downsample: NTILE into MAX_SEQ_LEN buckets, take first position from each ──
    # This guarantees at most MAX_SEQ_LEN positions, evenly spaced in time
    print(f"  Downsampling to max {MAX_SEQ_LEN} positions via NTILE ...")
    con.execute(f"""
        CREATE TABLE seq_bucketed AS
        SELECT *,
            NTILE({MAX_SEQ_LEN}) OVER (PARTITION BY mmsi, snapshot_ts ORDER BY p_ts DESC) AS bucket
        FROM seq_all
    """)
    # Take the first position from each bucket (closest to snapshot)
    con.execute(f"""
        CREATE TABLE seq_trimmed AS
        SELECT * EXCLUDE (bucket)
        FROM (
            SELECT *, ROW_NUMBER() OVER (
                PARTITION BY mmsi, snapshot_ts, bucket ORDER BY p_ts DESC
            ) AS rn_bucket
            FROM seq_bucketed
        )
        WHERE rn_bucket = 1
    """)
    con.execute("DROP TABLE seq_all; DROP TABLE seq_bucketed")
    n_trimmed = con.execute("SELECT count(*) FROM seq_trimmed").fetchone()[0]
    avg_seq = con.execute("""
        SELECT AVG(n) FROM (
            SELECT COUNT(*) AS n FROM seq_trimmed GROUP BY mmsi, snapshot_ts
        )
    """).fetchone()[0]
    print(f"  Trimmed: {n_trimmed} rows (avg {avg_seq:.1f} per sample)")

    # ── Compute derived features ──
    print("── Computing per-timestep features ──")
    # Re-order chronologically
    con.execute("""
        CREATE TABLE seq_chrono AS
        SELECT *, ROW_NUMBER() OVER (
            PARTITION BY mmsi, snapshot_ts ORDER BY p_ts ASC
        ) AS pos_idx
        FROM seq_trimmed
    """)
    con.execute("DROP TABLE seq_trimmed")

    con.execute("""
        CREATE TABLE seq_features AS
        SELECT *,
            SIN(RADIANS(COALESCE(cog, 0))) AS cog_sin,
            COS(RADIANS(COALESCE(cog, 0))) AS cog_cos,
            SIN(RADIANS(CASE WHEN true_heading < 360 THEN true_heading ELSE 0 END)) AS hdg_sin,
            COS(RADIANS(CASE WHEN true_heading < 360 THEN true_heading ELSE 0 END)) AS hdg_cos,
            COALESCE(navigational_status, 15) AS nav_status_filled,
            COALESCE(rate_of_turn, 0) AS rot_filled,
            COALESCE(
                EXTRACT(EPOCH FROM p_ts - LAG(p_ts) OVER (
                    PARTITION BY mmsi, snapshot_ts ORDER BY p_ts ASC
                )) / 3600.0,
                0.0
            ) AS dt_hours
        FROM seq_chrono
    """)
    con.execute("DROP TABLE seq_chrono")

    n_feat = con.execute("SELECT count(*) FROM seq_features").fetchone()[0]
    print(f"  {n_feat} rows with features")

    # ── Export to polars for haversine computation ──
    print("── Exporting to polars ──")
    step_cols = [
        "lat", "lon",
        "sog", "cog_sin", "cog_cos",
        "hdg_sin", "hdg_cos",
        "nav_status_filled", "rot_filled",
        "dt_hours", "hours_before",
    ]

    query_cols = ["mmsi", "snapshot_ts", "tta_h", "pos_idx"] + step_cols
    col_str = ", ".join(query_cols)
    df = pl.from_arrow(
        con.execute(f"SELECT {col_str} FROM seq_features ORDER BY mmsi, snapshot_ts, pos_idx").to_arrow_table()
    )
    con.close()

    # Fill nulls
    for col in step_cols:
        if df[col].null_count() > 0:
            df = df.with_columns(pl.col(col).fill_null(0.0))

    # ── Group into sequences ──
    print("── Grouping into sequences ──")
    agg_exprs = [
        pl.col("tta_h").first(),
        pl.col("pos_idx").count().alias("seq_len"),
    ] + [pl.col(f).implode().alias(f"seq_{f}") for f in step_cols]
    df_grp = df.group_by(["mmsi", "snapshot_ts"], maintain_order=True).agg(agg_exprs)
    n_samples = df_grp.height
    print(f"  {n_samples} samples")

    # ── Pad to fixed length ──
    print(f"  Padding to max_seq_len={MAX_SEQ_LEN} ...")
    n_feat = len(step_cols)
    X_seq = np.zeros((n_samples, MAX_SEQ_LEN, n_feat), dtype=np.float32)
    seq_lengths = np.zeros(n_samples, dtype=np.int32)
    y = df_grp["tta_h"].to_numpy().astype(np.float32)
    mmsi_arr = df_grp["mmsi"].to_numpy().astype(np.int64)

    for k in range(n_feat):
        list_col = df_grp[f"seq_{step_cols[k]}"]
        for i in range(n_samples):
            vals = list_col[i].to_numpy()
            n_use = min(len(vals), MAX_SEQ_LEN)
            X_seq[i, :n_use, k] = vals[:n_use]
            if k == 0:
                seq_lengths[i] = n_use

    print(f"  Array shape: {X_seq.shape} ({X_seq.nbytes / 1024**2:.0f} MB)")

    # ── Stats ──
    print(f"\n── Sequence Dataset Summary ──")
    print(f"  Samples: {n_samples}")
    print(f"  Step features: {n_feat}")
    print(f"  Max seq len: {MAX_SEQ_LEN}")
    print(f"  Mean seq len: {seq_lengths.mean():.1f}")
    print(f"  Median seq len: {np.median(seq_lengths):.1f}")
    for threshold in [10, 25, 50, 75, 90, 100]:
        count = (seq_lengths >= threshold).sum()
        print(f"  ≥{threshold} positions: {count} ({count/n_samples*100:.1f}%)")
    print(f"  TTA range: {y.min():.1f}h → {y.max():.1f}h (mean={y.mean():.1f}h)")

    # ── Save ──
    print(f"\n── Saving to {OUTPUT_DIR} ──")
    np.save(OUTPUT_DIR / "X_seq.npy", X_seq)
    np.save(OUTPUT_DIR / "seq_lengths.npy", seq_lengths)
    np.save(OUTPUT_DIR / "y.npy", y)
    np.save(OUTPUT_DIR / "mmsi.npy", mmsi_arr)

    meta = {
        "n_samples": int(n_samples),
        "step_features": step_cols,
        "n_step_features": len(step_cols),
        "max_seq_len": MAX_SEQ_LEN,
        "seq_window_h": SEQ_WINDOW_H,
        "mean_seq_len": float(seq_lengths.mean()),
        "target_mean_h": float(y.mean()),
        "target_min_h": float(y.min()),
        "target_max_h": float(y.max()),
    }
    json.dump(meta, open(OUTPUT_DIR / "meta.json", "w"), indent=2)
    print(f"✓ Done")


if __name__ == "__main__":
    build()
