"""Fast unit/API tests for FloodGuard AI. Run: python -m unittest -v test_floodguard.py"""
import os
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from fastapi.testclient import TestClient
import main as fg


class FloodGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_db = fg.DB_PATH
        fg.DB_PATH = fg.Path(self.temp_dir.name) / "test.db"
        fg.init_db()
        self.client_context = TestClient(fg.app)
        self.client = self.client_context.__enter__()

    def tearDown(self):
        self.client_context.__exit__(None, None, None)
        fg.DB_PATH = self.old_db
        self.temp_dir.cleanup()

    def test_health_and_dashboard(self):
        self.assertEqual(self.client.get("/api/health").status_code, 200)
        body = self.client.get("/api/dashboard").json()
        self.assertEqual(body["total_reports"], 0)
        self.assertFalse(body["validated_forecast"])

    def test_report_validation_and_status(self):
        bad = self.client.post("/api/reports", json={"location":"Bad", "latitude":100, "longitude":1,
            "severity":"high", "description":"invalid coordinates"})
        self.assertEqual(bad.status_code, 422)
        partial = self.client.post("/api/reports", json={"location":"Some Road", "latitude":12.9,
            "severity":"high", "description":"Observed waterlogging"})
        self.assertEqual(partial.status_code, 422)
        created = self.client.post("/api/reports", json={"location":"Test Road", "latitude":12.9, "longitude":77.6,
            "severity":"high", "description":"Observed waterlogging"})
        self.assertEqual(created.status_code, 201)
        self.assertEqual(created.json()["risk"], 78)
        updated = self.client.patch(f"/api/reports/{created.json()['id']}/status", json={"status":"Under review"})
        self.assertEqual(updated.json()["status"], "Under review")

    def test_risk_estimate_is_not_validated_forecast(self):
        response = self.client.post("/api/risk/estimate", json={"rainfall_mm_24h":85,
            "observed_severity":"moderate", "water_depth_cm":8, "drainage_blocked":False})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["score"], 60)
        self.assertFalse(response.json()["validated_forecast"])

    def test_route_scoring_detects_nearby_report_and_ignores_resolved(self):
        now = datetime.now(timezone.utc).isoformat()
        report = {"id":1,"location":"Test Road","latitude":12.0,"longitude":77.0,
            "severity":"high","description":"Flood report","status":"Reported","risk":78,
            "created_at":now,"age_hours":0.1}
        route = {"distance":1000,"duration":300,"geometry":{"coordinates":[[76.9999,12.0],[77.0001,12.0]]}}
        result = fg.score_route(route, [report])
        self.assertGreater(result["exposure_score"], 0)
        self.assertEqual(result["hazard_count"], 1)
        far_route = {"distance":1000,"duration":300,"geometry":{"coordinates":[[77.01,12.01],[77.011,12.011]]}}
        self.assertEqual(fg.score_route(far_route, [report])["hazard_count"], 0)

    @patch("main.fetch_osrm_routes")
    def test_route_compare_endpoint_returns_ranked_routes(self, mocked_routes):
        mocked_routes.return_value = [
            {"distance":1000,"duration":300,"geometry":{"coordinates":[[77.0,12.0],[77.01,12.01]]}},
            {"distance":1200,"duration":420,"geometry":{"coordinates":[[77.0,12.0],[77.02,12.02]]}},
        ]
        response = self.client.post("/api/routes/compare", json={"origin_lat":12.0,"origin_lon":77.0,
            "destination_lat":12.02,"destination_lon":77.02})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(len(body["routes"]), 2)
        self.assertFalse(body["validated_forecast"])
        self.assertIn("geometry", body["routes"][0])

    @patch("main.geocode_place", return_value=[{"display_name":"Central Bengaluru", "latitude":12.97, "longitude":77.59}])
    def test_place_search_returns_osm_matches_and_attribution(self, mocked_geocode):
        response = self.client.post("/api/geocode", json={"query":"Central Bengaluru"})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["provider"], "OpenStreetMap Nominatim")
        self.assertEqual(body["matches"][0]["latitude"], 12.97)
        self.assertIn("OpenStreetMap contributors", body["attribution"])
        mocked_geocode.assert_called_once_with("Central Bengaluru")

    @patch("main.fetch_osrm_routes")
    def test_route_rank_prefers_lower_known_report_exposure(self, mocked_routes):
        self.client.post("/api/reports", json={"location":"Test Road", "latitude":12.0, "longitude":77.0,
            "severity":"high", "description":"Observed waterlogging"})
        mocked_routes.return_value = [
            {"distance":1000,"duration":300,"geometry":{"coordinates":[[77.0,12.0],[77.002,12.0]]}},
            {"distance":1300,"duration":900,"geometry":{"coordinates":[[77.01,12.01],[77.02,12.02]]}},
        ]
        response = self.client.post("/api/routes/compare", json={"origin_lat":12.0,"origin_lon":77.0,
            "destination_lat":12.02,"destination_lon":77.02})
        self.assertEqual(response.status_code, 200, response.text)
        routes = response.json()["routes"]
        self.assertEqual(routes[0]["hazard_count"], 0)
        self.assertGreater(routes[1]["hazard_count"], 0)
        self.assertFalse(response.json()["validated_forecast"])

    @patch("main.fetch_weather")
    def test_weather_endpoint_returns_cached_provider_payload(self, mocked_weather):
        mocked_weather.return_value = {"current": {"temperature_2m": 25}, "forecast": {"rainfall_next_24h_mm": 12.5}, "source": "Open-Meteo"}
        response = self.client.get("/api/weather?latitude=12.97&longitude=77.59")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["forecast"]["rainfall_next_24h_mm"], 12.5)

    @patch("main.fetch_weather")
    @patch("main.load_flood_model", return_value=(None, None))
    def test_prediction_does_not_fake_ai_when_model_is_untrained(self, mocked_model, mocked_weather):
        mocked_weather.return_value = {
            "current": {"temperature_2m": 25, "relative_humidity_2m": 70},
            "forecast": {"rainfall_next_24h_mm": 20, "rainfall_next_72h_mm_estimate": 35,
                         "rainfall_next_7d_window_mm_estimate": 50, "rain_hours_next_24h": 5},
            "source": "Open-Meteo", "fetched_at": "2026-10-29T00:00:00+00:00"
        }
        response = self.client.get("/api/prediction/flood")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["prediction_available"])
        self.assertEqual(response.json()["model_status"], "not_trained")
        self.assertIn("not been trained", response.json()["message"])


if __name__ == "__main__":
    unittest.main()
