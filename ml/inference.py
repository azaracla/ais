"""Inference module — predict time-to-arrival for a vessel.

Loads the best available model and predicts hours until arrival.
Supports: global LightGBM model or per-horizon ensemble.

Input: dict with features (see NUMERIC_FEATURES) + optional ship_type.
Output: predicted hours, confidence flag, prediction interval.
"""

import json
import pickle
import numpy as np
import polars as pl
from pathlib import Path
import lightgbm as lgb

MODEL_DIR = Path(__file__).parent / "data" / "models"

# These must match build_dataset.py/train_horizon.py
NUMERIC_FEATURES = [
    "dist_to_dest_km", "sog", "cog", "bearing_offset_deg",
    "vessel_length", "vessel_width", "length_width_ratio",
    "hour_of_day", "day_of_week",
    "avg_sog_1h", "avg_sog_6h", "avg_sog_24h", "sog_trend_1h",
    "mmsi_avg_sog", "sog_vs_mmsi_avg", "eta_naive_h",
]

HORIZON_BINS = [
    (0, 1, "0-1h"),
    (1, 6, "1-6h"),
    (6, 24, "6-24h"),
    (24, 72, "1-3d"),
    (72, 200, "3-8d"),
]


class ETAPredictor:
    """Predict vessel time-to-arrival using trained models."""

    def __init__(self):
        self.models = {}
        self.router = None
        self.meta = None
        self._load()

    def _load(self):
        meta_path = MODEL_DIR / "metadata.json"
        if not meta_path.exists():
            raise FileNotFoundError(f"No model metadata at {meta_path}. Run train_horizon.py first.")

        self.meta = json.loads(meta_path.read_text())

        # Load per-horizon models
        for lo, hi, name in HORIZON_BINS:
            model_path = MODEL_DIR / f"model_{name.replace('-', '_')}.txt"
            if model_path.exists():
                self.models[name] = lgb.Booster(model_file=str(model_path))

        # Load router
        router_path = MODEL_DIR / "router.txt"
        if router_path.exists():
            self.router = lgb.Booster(model_file=str(router_path))

        print(f"Loaded {len(self.models)} horizon models + router")

    def _encode_ship_type(self, ship_types: np.ndarray) -> np.ndarray:
        """One-hot encode ship_type to match training features."""
        top_types = sorted(self.meta["top_ship_types"])
        st = np.nan_to_num(ship_types, nan=-1).astype(np.int64)
        columns = []
        for st_val in top_types:
            columns.append((st == st_val).astype(np.float32))
        # Always include st_other (was present during training)
        other = ~np.isin(st, top_types)
        columns.append(other.astype(np.float32))
        return np.column_stack(columns)

    def predict(self, features: dict | list[dict], ship_type: int | list[int] | None = None) -> dict:
        """Predict time-to-arrival for one or more vessels.

        Args:
            features: dict or list of dicts with NUMERIC_FEATURES keys.
            ship_type: optional AIS ship type code(s).

        Returns:
            dict with 'prediction_hours', 'confidence' (high/medium/low),
            'horizon_bin', 'model_name'.
        """
        # Normalize to batch
        if isinstance(features, dict):
            features = [features]
            ship_type = [ship_type] if ship_type is not None else [None]
        elif ship_type is None:
            ship_type = [None] * len(features)

        n = len(features)

        # Build feature matrix
        X_num = np.zeros((n, len(NUMERIC_FEATURES)), dtype=np.float32)
        for i, feat in enumerate(features):
            for j, col in enumerate(NUMERIC_FEATURES):
                X_num[i, j] = float(feat.get(col, 0.0))

        X_ship = self._encode_ship_type(np.array(ship_type, dtype=np.float64))
        X = np.column_stack([X_num, X_ship])

        # Router prediction
        if self.router is not None:
            router_proba = self.router.predict(X)
            bin_idx = np.argmax(router_proba, axis=1)
            bin_confidence = np.max(router_proba, axis=1)
        else:
            # Fallback: use eta_naive
            eta_naive = X[:, NUMERIC_FEATURES.index("eta_naive_h")]
            bins = [0, 1, 6, 24, 72, 200]
            bin_idx = np.clip(np.digitize(eta_naive, bins) - 1, 0, len(HORIZON_BINS) - 1)
            bin_confidence = np.ones(n) * 0.5

        # Predict with appropriate horizon model
        predictions = np.zeros(n)
        for i, (lo, hi, name) in enumerate(HORIZON_BINS):
            mask = bin_idx == i
            if mask.sum() > 0 and name in self.models:
                pred_log = self.models[name].predict(X[mask])
                predictions[mask] = np.expm1(pred_log)

        # Confidence based on horizon bin + router confidence
        confidence_map = {"0-1h": "high", "1-6h": "high", "6-24h": "medium",
                          "1-3d": "low", "3-8d": "low"}

        results = []
        for i in range(n):
            bin_name = HORIZON_BINS[bin_idx[i]][2]
            results.append({
                "prediction_hours": round(float(predictions[i]), 1),
                "confidence": confidence_map.get(bin_name, "low"),
                "horizon_bin": bin_name,
                "router_confidence": round(float(bin_confidence[i]), 2),
            })

        return results[0] if len(results) == 1 else results


# ── CLI ──

if __name__ == "__main__":
    # Test with a near-port vessel
    pred = ETAPredictor()

    # Example: vessel 50km from Rotterdam, moving at 12kn toward port
    test_vessel = {
        "dist_to_dest_km": 50.0,
        "sog": 12.0,
        "cog": 270.0,
        "bearing_offset_deg": 5.0,
        "vessel_length": 200.0,
        "vessel_width": 32.0,
        "length_width_ratio": 6.25,
        "hour_of_day": 14.0,
        "day_of_week": 3.0,
        "avg_sog_1h": 11.5,
        "avg_sog_6h": 11.0,
        "avg_sog_24h": 10.5,
        "sog_trend_1h": 0.5,
        "mmsi_avg_sog": 11.0,
        "sog_vs_mmsi_avg": 1.09,
        "eta_naive_h": 4.17,
    }

    result = pred.predict(test_vessel, ship_type=70)
    print(f"\nTest prediction:")
    print(f"  Vessel at 50km, 12kn, heading to port")
    print(f"  Predicted TTA: {result['prediction_hours']}h")
    print(f"  Confidence:    {result['confidence']}")
    print(f"  Horizon bin:   {result['horizon_bin']}")
    print(f"  True TTA (dist/sog): 50/12 = {50/12:.1f}h")
