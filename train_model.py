"""Train an experimental Bengaluru Urban flood-event classifier from public historical data.

This is a district-level research baseline, NOT a street-level flood predictor.
Run: python train_model.py
Requires internet access and the dependencies in requirements.txt.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from urllib.request import Request, urlopen
import json
import os
import sys

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import balanced_accuracy_score, classification_report, roc_auc_score

ROOT = Path(__file__).resolve().parent
MODEL_PATH = Path(os.getenv("FLOODGUARD_MODEL_PATH", str(ROOT / "models" / "bengaluru_flood_model.joblib")))
META_PATH = Path(os.getenv("FLOODGUARD_MODEL_META_PATH", str(MODEL_PATH.with_name("bengaluru_flood_model_meta.json"))))
MODEL_DIR = MODEL_PATH.parent
INVENTORY_URL = "https://zenodo.org/records/16994648/files/India_Flood_Inventory_v3.csv?download=1"
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
FEATURES = ["rainfall_1d", "rainfall_3d", "rainfall_7d", "temperature_mean", "humidity_mean", "precipitation_hours"]


def download_bytes(url: str) -> bytes:
    req = Request(url, headers={"User-Agent": "FloodGuardAI/1.2 research prototype"})
    with urlopen(req, timeout=60) as response:
        return response.read()


def main() -> int:
    print("FloodGuard AI experimental model trainer")
    print("Scope: Bengaluru Urban district-level historical flood-event signal, not road-level flooding.")
    print("Downloading India Flood Inventory from Zenodo…")
    try:
        inventory = pd.read_csv(__import__("io").BytesIO(download_bytes(INVENTORY_URL)), encoding="utf-8-sig", low_memory=False)
    except Exception as exc:
        print(f"Could not download/read the flood inventory: {exc}")
        return 2

    if "Districts" not in inventory.columns or "Start Date" not in inventory.columns or "End Date" not in inventory.columns:
        print("Dataset columns did not match the expected inventory schema; stopping rather than training on guessed labels.")
        return 2

    district_names = inventory["Districts"].fillna("").astype(str).str.lower()
    location_names = inventory.get("Location", pd.Series([""] * len(inventory))).fillna("").astype(str).str.lower()
    state_names = inventory.get("State", pd.Series([""] * len(inventory))).fillna("").astype(str).str.lower()
    # Match district-level references only. Do not infer a Bengaluru label from Karnataka alone.
    mask = district_names.str.contains(r"bengaluru\s+urban|bangalore\s+urban", regex=True) | location_names.str.contains(r"bengaluru\s+urban|bangalore\s+urban", regex=True)
    events = inventory.loc[mask].copy()
    events["start"] = pd.to_datetime(events["Start Date"], dayfirst=True, errors="coerce", utc=True).dt.floor("D").dt.tz_localize(None)
    events["end"] = pd.to_datetime(events["End Date"], dayfirst=True, errors="coerce", utc=True).dt.floor("D").dt.tz_localize(None)
    events = events.dropna(subset=["start", "end"])
    if events.empty:
        print("No flood inventory rows explicitly identify Bengaluru Urban/Bangalore Urban. No model was trained.")
        return 3
    start_date = max(pd.Timestamp("1967-01-01"), events["start"].min())
    end_date = min(pd.Timestamp("2023-12-31"), events["end"].max())
    if start_date > end_date:
        print("No usable historical date range was found.")
        return 3

    print(f"Matched {len(events)} district-level flood event record(s); requesting historical weather {start_date.date()} to {end_date.date()}…")
    params = {
        "latitude": 12.9716, "longitude": 77.5946,
        "start_date": start_date.strftime("%Y-%m-%d"), "end_date": end_date.strftime("%Y-%m-%d"),
        "daily": "precipitation_sum,temperature_2m_mean,relative_humidity_2m_mean,precipitation_hours",
        "timezone": "Asia/Kolkata",
    }
    from urllib.parse import urlencode
    try:
        weather_payload = json.loads(download_bytes(ARCHIVE_URL + "?" + urlencode(params)).decode("utf-8"))
    except Exception as exc:
        print(f"Could not fetch historical weather: {exc}")
        return 2
    daily = weather_payload.get("daily") or {}
    if not daily.get("time"):
        print("Historical weather response contained no daily data.")
        return 2
    frame = pd.DataFrame({
        "date": pd.to_datetime(daily["time"]),
        "rainfall_1d": daily.get("precipitation_sum"),
        "temperature_mean": daily.get("temperature_2m_mean"),
        "humidity_mean": daily.get("relative_humidity_2m_mean"),
        "precipitation_hours": daily.get("precipitation_hours"),
    }).sort_values("date")
    frame["rainfall_3d"] = frame["rainfall_1d"].rolling(3, min_periods=1).sum()
    frame["rainfall_7d"] = frame["rainfall_1d"].rolling(7, min_periods=1).sum()
    frame = frame.dropna(subset=FEATURES).copy()

    positive_dates: set[pd.Timestamp] = set()
    for _, event in events.iterrows():
        a, b = max(event["start"], frame["date"].min()), min(event["end"], frame["date"].max())
        if a <= b:
            positive_dates.update(pd.date_range(a, b, freq="D"))
    frame["flood_event"] = frame["date"].isin(positive_dates).astype(int)
    positives = int(frame["flood_event"].sum())
    negatives = int((frame["flood_event"] == 0).sum())
    print(f"Prepared {len(frame)} daily records: {positives} positive flood-event days, {negatives} other days.")
    if positives < 10 or negatives < 50:
        print("Not enough labelled examples for a responsible baseline (need >=10 positive days and >=50 non-event days). No model was trained.")
        return 4

    # Chronological holdout prevents randomly mixing later dates into training.
    split = int(len(frame) * 0.8)
    train, test = frame.iloc[:split], frame.iloc[split:]
    if train["flood_event"].nunique() < 2:
        print("Training period contains only one label class; no model was trained.")
        return 4
    model = RandomForestClassifier(n_estimators=400, min_samples_leaf=3, class_weight="balanced_subsample", random_state=42, n_jobs=-1)
    model.fit(train[FEATURES], train["flood_event"])
    y_true = test["flood_event"].to_numpy()
    y_pred = model.predict(test[FEATURES])
    metrics = {"balanced_accuracy": round(float(balanced_accuracy_score(y_true, y_pred)), 4)}
    if len(set(y_true)) == 2:
        metrics["roc_auc"] = round(float(roc_auc_score(y_true, model.predict_proba(test[FEATURES])[:, 1])), 4)
    print("Chronological holdout metrics:", json.dumps(metrics))
    print(classification_report(y_true, y_pred, labels=[0, 1], target_names=["non-event day", "flood-event day"], zero_division=0))

    # Refit on all available history after reporting the holdout metrics.
    final_model = RandomForestClassifier(n_estimators=400, min_samples_leaf=3, class_weight="balanced_subsample", random_state=42, n_jobs=-1)
    final_model.fit(frame[FEATURES], frame["flood_event"])
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    joblib.dump(final_model, MODEL_PATH)
    metadata = {
        "status": "trained_experimental", "model_type": "RandomForestClassifier",
        "trained_at_utc": datetime.utcnow().isoformat() + "Z", "features": FEATURES,
        "training_rows": int(len(frame)), "positive_event_days": positives,
        "non_event_days": negatives, "start_date": str(frame["date"].min().date()),
        "end_date": str(frame["date"].max().date()), "chronological_holdout_metrics": metrics,
        "target": "date overlaps a flood event explicitly attributed to Bengaluru Urban/Bangalore Urban in India Flood Inventory",
        "scope_limit": "District-level historical event signal, not street-level urban waterlogging. Inventory events may be incomplete and labels are coarse.",
        "inventory_source": "India Flood Inventory v3, Zenodo record 16994648, https://doi.org/10.5281/zenodo.16994648",
        "weather_source": "Open-Meteo Historical Weather API, https://open-meteo.com/en/docs/historical-weather-api",
        "dataset_license_note": "India Flood Inventory record lists CC BY-NC 4.0; check license terms before commercial use.",
        "validated_forecast": False,
    }
    META_PATH.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    print(f"Saved model: {MODEL_PATH}")
    print(f"Saved metadata: {META_PATH}")
    print("This is a research baseline, not a calibrated or safety-certified forecast. Review the metrics and data coverage before using outputs.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
