"""Standard-library integration smoke checks. Start the API first, then run from project root."""
import json
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8000"

def call(path, method="GET", payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"} if data is not None else {},
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        raw = response.read()
        return response.status, json.loads(raw) if "json" in response.headers.get("Content-Type", "") else raw.decode()

assert call("/api/health")[1]["status"] == "ok"
assert "FloodGuard AI" in call("/")[1]
status, report = call("/api/reports", "POST", {
    "location": "Smoke test report", "latitude": 12.9, "longitude": 77.6,
    "severity": "moderate", "description": "Temporary integration smoke test",
})
assert status == 201 and report["risk"] == 48
updated = call(f"/api/reports/{report['id']}/status", "PATCH", {"status": "Under review"})[1]
assert updated["status"] == "Under review"
estimate = call("/api/risk/estimate", "POST", {
    "rainfall_mm_24h": 85, "observed_severity": "moderate",
    "water_depth_cm": 8, "drainage_blocked": False,
})[1]
assert estimate["score"] == 60 and estimate["validated_forecast"] is False
try:
    call("/api/reports", "POST", {"location": "Bad", "latitude": 100, "longitude": 0,
                                     "severity": "high", "description": "Invalid coordinates"})
except urllib.error.HTTPError as error:
    assert error.code == 422
else:
    raise AssertionError("Invalid coordinates were accepted")
print("PASS: health, dashboard, report save/status, risk estimate, validation")
print("Note: smoke test created a report in the local demo database; remove it in the UI if desired.")
