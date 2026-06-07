"""Build sequence dataset for v9 LSTM/Transformer TTA prediction.

For each snapshot sample in dataset_v7, extract the last N=50 AIS positions
before pos_ts, producing padded sequences with per-timestep features.

All heavy spatial math done in DuckDB. Grouping/padding via polars implode.

Usage:
  uv run python ml/build_sequences.py              # full build
  uv run python ml/build_sequences.py --sample 500   # test subset
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
OUTPUT_DIR = DATA_DIR / "sequences_v9"
OUTPUT_DIR.mkdir(exist_ok=True)

MAX_SEQ_LEN = 50
SEQ_WINDOW_H = 24

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
    print("Building v9 Sequence Dataset")
    print(f"  Max seq len: {MAX_SEQ_LEN}, window: {SEQ_WINDOW_H}h")
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

    # ── Join samples with ports to get port lat/lon ──
    print("── Joining samples with ports ──")
    con.execute(f"""
        CREATE TABLE samples_port AS
        SELECT s.*, p.lat AS port_lat, p.lon AS port_lon
        FROM samples s
        LEFT JOIN read_parquet('{PORTS}') p
            ON s.port_lo_code = p.lo_code
    """)
    n_with_port = con.execute(
        "SELECT count(*) FROM samples_port WHERE port_lat IS NOT NULL"
    ).fetchone()[0]
    print(f"  {n_with_port}/{n} samples have port lat/lon")

    # ── Range join + all features in DuckDB ──
    print(f"\n── Range-joining samples to positions ([-{SEQ_WINDOW_H}h, 0h]) ──")
    con.execute(f"""
        CREATE TABLE seq_joined AS
        SELECT
            s.mmsi,
            s.pos_ts AS snapshot_ts,
            s.arrival_ts,
            s.time_to_arrival_hours AS tta_h,
            s.dist_to_dest_km AS snapshot_dist_km,
            s.port_lat, s.port_lon,
            p.ts AS p_ts,
            p.lat, p.lon, p.sog, p.cog,
            p.true_heading, p.navigational_status, p.rate_of_turn,
            -- hours before snapshot
            EXTRACT(EPOCH FROM s.pos_ts - p.ts) / 3600.0 AS hours_before,
            -- row number DESC (1 = closest to snapshot)
            ROW_NUMBER() OVER (
                PARTITION BY s.mmsi, s.pos_ts
                ORDER BY p.ts DESC
            ) AS rn_desc,
            -- row number ASC (1 = farthest in window)
            ROW_NUMBER() OVER (
                PARTITION BY s.mmsi, s.pos_ts
                ORDER BY p.ts ASC
            ) AS rn_asc
        FROM samples_port s
        JOIN read_parquet('{POSITIONS}') p
            ON s.mmsi = p.mmsi
            AND p.ts >= s.pos_ts - INTERVAL '{SEQ_WINDOW_H}' HOUR
            AND p.ts <= s.pos_ts
    """)
    n_joined = con.execute("SELECT count(*) FROM seq_joined").fetchone()[0]
    print(f"  Joined: {n_joined} rows ({n_joined/n:.0f} per sample)")

    # ── Trim to MAX_SEQ_LEN most recent positions ──
    con.execute(f"""
        CREATE TABLE seq_trimmed AS
        SELECT * FROM seq_joined WHERE rn_desc <= {MAX_SEQ_LEN}
    """)
    con.execute("DROP TABLE seq_joined")
    n_seq = con.execute("SELECT count(*) FROM seq_trimmed").fetchone()[0]
    print(f"  Trimmed: {n_seq} rows")

    # Re-number chronologically within each sequence
    con.execute("""
        CREATE TABLE seq_chrono AS
        SELECT *,
            ROW_NUMBER() OVER (PARTITION BY mmsi, snapshot_ts ORDER BY p_ts ASC) AS pos_idx
        FROM seq_trimmed
    """)
    con.execute("DROP TABLE seq_trimmed")

    # ── Compute all derived features in DuckDB ──
    print("── Computing derived features ──")
    con.execute("""
        CREATE TABLE seq_features AS
        SELECT
            *,
            -- COG sin/cos
            SIN(RADIANS(COALESCE(cog, 0))) AS cog_sin,
            COS(RADIANS(COALESCE(cog, 0))) AS cog_cos,
            -- True heading sin/cos (511 = unknown)
            SIN(RADIANS(CASE WHEN true_heading < 360 THEN true_heading ELSE 0 END)) AS hdg_sin,
            COS(RADIANS(CASE WHEN true_heading < 360 THEN true_heading ELSE 0 END)) AS hdg_cos,
            -- Nav status (null → 15)
            COALESCE(navigational_status, 15) AS nav_status_filled,
            -- Rate of turn (null → 0)
            COALESCE(rate_of_turn, 0) AS rot_filled,
            -- Time delta from previous position (hours)
            COALESCE(
                EXTRACT(EPOCH FROM p_ts - LAG(p_ts) OVER (
                    PARTITION BY mmsi, snapshot_ts ORDER BY p_ts ASC
                )) / 3600.0,
                0.0
            ) AS dt_hours,
            -- Haversine distance to port (km)
            6371.0 * 2.0 * ASIN(SQRT(
                POW(SIN(RADIANS(port_lat - lat) / 2.0), 2) +
                COS(RADIANS(port_lat)) * COS(RADIANS(lat)) *
                POW(SIN(RADIANS(port_lon - lon) / 2.0), 2)
            )) AS dist_to_dest_km,
            -- Bearing to port (degrees, 0-360)
            MOD(
                ATAN2(
                    SIN(RADIANS(port_lon - lon)) * COS(RADIANS(port_lat)),
                    COS(RADIANS(lat)) * SIN(RADIANS(port_lat)) -
                    SIN(RADIANS(lat)) * COS(RADIANS(port_lat)) *
                    COS(RADIANS(port_lon - lon))
                ) * 180.0 / PI() + 360.0,
                360.0
            ) AS bearing_to_dest_deg,
        FROM seq_chrono
    """)
    con.execute("DROP TABLE seq_chrono")

    # Distance delta from previous position (haversine between consecutive positions)
    print("  Computing position-to-position distance deltas ...")
    con.execute("""
        CREATE TABLE seq_features2 AS
        SELECT *,
            -- Distance from previous position (km)
            COALESCE(
                6371.0 * 2.0 * ASIN(SQRT(
                    POW(SIN(RADIANS(
                        LAG(lat) OVER (PARTITION BY mmsi, snapshot_ts ORDER BY p_ts ASC) - lat
                    ) / 2.0), 2) +
                    COS(RADIANS(
                        LAG(lat) OVER (PARTITION BY mmsi, snapshot_ts ORDER BY p_ts ASC)
                    )) * COS(RADIANS(lat)) *
                    POW(SIN(RADIANS(
                        LAG(lon) OVER (PARTITION BY mmsi, snapshot_ts ORDER BY p_ts ASC) - lon
                    ) / 2.0), 2)
                )),
                0.0
            ) AS d_km,
            -- SOG acceleration (kn per hour, from last position)
            COALESCE(
                (sog - LAG(sog) OVER (PARTITION BY mmsi, snapshot_ts ORDER BY p_ts ASC))
                / NULLIF(EXTRACT(EPOCH FROM
                    p_ts - LAG(p_ts) OVER (PARTITION BY mmsi, snapshot_ts ORDER BY p_ts ASC)
                ) / 3600.0, 0),
                0.0
            ) AS sog_accel,
        FROM seq_features
    """)
    con.execute("DROP TABLE seq_features")

    # Zero d_km for first position in each sequence
    con.execute("""
        CREATE TABLE seq_final AS
        SELECT *,
            CASE WHEN dt_hours = 0.0 THEN 0.0 ELSE d_km END AS d_km_clean,
            CASE WHEN dt_hours = 0.0 THEN 0.0 ELSE sog_accel END AS sog_accel_clean
        FROM seq_features2
    """)
    con.execute("DROP TABLE seq_features2")

    n_feat = con.execute("SELECT count(*) FROM seq_final").fetchone()[0]
    print(f"  {n_feat} rows with features")

    # ── Export to polars ──
    print("\n── Exporting to polars ──")
    step_cols = [
        "lat", "lon",
        "sog", "cog_sin", "cog_cos",
        "hdg_sin", "hdg_cos",
        "nav_status_filled", "rot_filled", "sog_accel_clean",
        "dt_hours", "d_km_clean",
        "dist_to_dest_km", "bearing_to_dest_deg",
        "hours_before",
    ]
    STEP_FEATURES = step_cols  # preserve for metadata

    query_cols = ["mmsi", "snapshot_ts", "tta_h", "pos_idx"] + step_cols
    col_str = ", ".join(query_cols)
    df = pl.from_arrow(
        con.execute(f"SELECT {col_str} FROM seq_final ORDER BY mmsi, snapshot_ts, pos_idx").fetch_arrow_table()
    )
    con.close()

    # Fill any remaining nulls
    for col in step_cols:
        if df[col].null_count() > 0:
            df = df.with_columns(pl.col(col).fill_null(0.0))

    # ── Group into sequences with polars implode (fast, native) ──
    print("\n── Grouping into sequences (polars implode) ──")
    agg_exprs = [
        pl.col("tta_h").first(),
        pl.col("pos_idx").count().alias("seq_len"),
    ] + [
        pl.col(f).implode().alias(f"seq_{f}") for f in step_cols
    ]
    df_grp = df.group_by(["mmsi", "snapshot_ts"], maintain_order=True).agg(agg_exprs)
    n_samples = df_grp.height
    print(f"  {n_samples} samples after grouping")

    # ── Pad to fixed length ──
    print(f"  Padding to max_seq_len={MAX_SEQ_LEN} ...")
    X_seq = np.zeros((n_samples, MAX_SEQ_LEN, len(step_cols)), dtype=np.float32)
    seq_lengths = np.zeros(n_samples, dtype=np.int32)
    y = df_grp["tta_h"].to_numpy().astype(np.float32)
    mmsi_arr = df_grp["mmsi"].to_numpy().astype(np.int64)

    for k, col in enumerate(step_cols):
        list_col = df_grp[f"seq_{col}"]
        for i in range(n_samples):
            vals = list_col[i].to_numpy()
            n = min(len(vals), MAX_SEQ_LEN)
            X_seq[i, :n, k] = vals[:n]
            if k == 0:  # only set seq_lengths once per sample
                seq_lengths[i] = n

    print(f"  Array shape: {X_seq.shape} ({X_seq.nbytes / 1024**2:.0f} MB)")

    # ── Stats ──
    print(f"\n── Sequence Dataset Summary ──")
    print(f"  Samples: {n_samples}")
    print(f"  Step features: {len(step_cols)}")
    print(f"  Max seq len: {MAX_SEQ_LEN}")
    print(f"  Mean seq len: {seq_lengths.mean():.1f}")
    print(f"  Median seq len: {np.median(seq_lengths):.1f}")
    print(f"  Min seq len: {seq_lengths.min()}")
    print(f"  Short (<10): {(seq_lengths < 10).sum()} ({(seq_lengths < 10).sum()/n_samples*100:.1f}%)")
    print(f"  Full (50): {(seq_lengths == MAX_SEQ_LEN).sum()} ({(seq_lengths == MAX_SEQ_LEN).sum()/n_samples*100:.1f}%)")
    print(f"  TTA range: {y.min():.1f}h → {y.max():.1f}h  (mean={y.mean():.1f}h)")

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
