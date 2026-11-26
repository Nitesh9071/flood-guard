# FloodGuard AI — improved prototype

FloodGuard AI is a Bengaluru-focused urban-flood reporting dashboard with a FastAPI/SQLite backend, Leaflet map, incident reporting, flood-aware route comparison, live Open-Meteo weather context, and an experimental ML training pipeline based on historical flood-event records.

## What's included

- **Incident reporting:** choose a Bengaluru place from type-ahead suggestions, use current location, or pin the location on the map; coordinates are filled in the background.
- **Map:** displays saved, geolocated reports. Community reports are not independently verified.
- **Dashboard and status workflow:** report totals, severity counts, review status and status updates.
- **Route comparison:** choose origin and destination from live place suggestions as you type, or use current location/map pins; the backend requests candidate driving routes from OSRM and scores nearby recent reports.
- **Freshness handling:** route scoring only considers geolocated, non-resolved reports no older than 72 hours. Reports in “Under review” receive a lower contribution than reports still marked “Reported”; neither status means independently verified.
- **Validation and tests:** coordinate pair validation, error handling, database indexes, consistent database connection cleanup, and automated unit/API tests.

## Important limitations

This remains a prototype and is **not emergency navigation**. Route scores are heuristic exposure indexes, not probabilities of flooding and not validated predictions. No known report does not mean a road is safe. Route scoring still uses community reports only; the live weather feed is shown as contextual information and is not yet used as a road-by-road route penalty. The app does not consume official road closures, live traffic or municipal systems. The ML model is not present until training completes successfully. Users must obey official closures and emergency instructions.

The default routing endpoint is the public OSRM demo service (`https://router.project-osrm.org`). It is useful for development but has no production service-level guarantee and does not provide live traffic. Place search-as-you-type uses the public Photon geocoder with an approximate Bengaluru Urban bounding box, India/Karnataka filtering, and OpenStreetMap data. Suggestions may miss places where OpenStreetMap coverage is incomplete. Photon public demo use is subject to reasonable-use limits and can be throttled; repeated queries are cached for six hours. Older clients can still call the legacy, user-triggered Nominatim endpoint; it is not used for autocomplete. The OpenStreetMap tile service requires internet and must be used in accordance with its usage policy.

No Google Maps account or API key is required. The frontend embeds no geocoding key. Place queries and approximate coordinates are sent to Photon; route requests go to OSRM, and weather requests go to Open-Meteo.

## Mobile and downloadable Android app

The responsive frontend can be built as an Android APK with Capacitor. In the mobile app, **Server settings** saves the backend URL on that device and verifies `/api/health`. Android location uses the native Geolocation plugin and requests location permission only when a user chooses a current-location action. Place search works in both web and Android interfaces.

The mobile app does not embed the Python API or its SQLite database. **No-cost option:** run the API on your Windows PC, connect the phone and PC to the same private Wi-Fi, and enter `http://<PC-private-IPv4>:8000` in Server settings. Start Uvicorn bound to `0.0.0.0` and allow port 8000 on Windows Firewall for Private networks only. The PC must stay on while the app is in use; its local database stores reports. This is for a trusted home/demo network only: the API has no login, and other devices that can reach it can submit reports and change statuses. Do not expose port 8000 to the public internet. For access away from that Wi-Fi, use an HTTPS service with persistent storage.

**Open API warning:** the current backend has no accounts or role checks. Anyone reaching the API can create reports and change incident statuses. Keep the server private or use a controlled prototype deployment until access control, rate limits, abuse protection, backups, and a privacy/retention policy are implemented. CORS is not authentication.

The project provides the Capacitor/Vite configuration and Windows APK build instructions below. A ready-to-install APK must be compiled with Android Studio/Android SDK and signed for release distribution.

## Android APK (Capacitor)

The Android app packages this same responsive dashboard in a native WebView and uses native Android location. Reporting, weather, route search/comparison, and flood-model predictions continue through the existing FastAPI backend. **The APK is not a self-contained server:** use the same-Wi-Fi PC option above at no hosting cost, or an HTTPS host for remote access. No Google Maps key is needed. Map tiles, geocoding, routing, and weather still need internet access.

### Android build requirements

- Node.js 22 LTS or newer and npm.
- Android Studio 2025.2.1 or newer with Android SDK Platform 26 or newer; Android Studio supplies the supported JDK.
- A connected Android device with USB debugging enabled, or an emulator.

Capacitor's official setup: [installation](https://capacitorjs.com/docs/getting-started), [Android environment](https://capacitorjs.com/docs/getting-started/environment-setup), and [geolocation permissions](https://capacitorjs.com/docs/apis/geolocation).

### Build and install a debug APK on Windows

From PowerShell, run the build helper from the project root (the folder containing `package.json`):

```powershell
.\scripts\build-android.ps1
```

The installable debug APK is `android\app\build\outputs\apk\debug\app-debug.apk`. Install it on a connected device with `adb install -r app\build\outputs\apk\debug\app-debug.apk`, or copy the APK to the phone and open it. The first app launch asks for the HTTPS API URL. Android will request location access the first time the user taps **Use my location**.

After frontend changes, run `npm run android:sync` and rebuild. For Play Store distribution, create a release build and sign it with your own release key in Android Studio; the debug APK is for testing/sideloading.

### Host the backend for the app

The included `Dockerfile` builds the FastAPI service. Deploy it to a host that provides HTTPS and a persistent writable volume mounted at `/data`. The container stores its SQLite database at `/data/floodguard.db` and the trained model at `/data/models/`. Configure the service health check as `/api/health`. The app's built-in CORS configuration accepts the Capacitor Android origin `https://localhost`. The Android build permits cleartext HTTP for private-LAN setup; the app itself only accepts HTTP API URLs that use localhost or a private IPv4 address. Never expose the unauthenticated API publicly over HTTP.

To train the model on that host, run `python train_model.py` in the service container after deployment. Training needs outbound internet access and can fail if the source has too few matching Bengaluru Urban flood events. The app will keep predictions unavailable if no model is produced.

**Deployment boundary:** this API currently has no user accounts or reviewer authorization. Anyone who can reach a deployed instance can submit reports and change incident status. Use it for a controlled prototype only until authentication, reviewer roles, rate limiting, abuse controls, backups, and privacy/retention rules are added. Do not expose a public community service with the current open write endpoints.

## Run on Windows PowerShell

Run these commands from the project root (the folder containing `main.py`, `frontend/`, and `requirements.txt`):

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m uvicorn main:app --reload
```

Open:

- Dashboard: `http://127.0.0.1:8000/`
- API docs: `http://127.0.0.1:8000/docs`
- Health: `http://127.0.0.1:8000/api/health`

## Run on macOS/Linux

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m uvicorn main:app --reload
```

## Run tests

Install requirements, then run from the project root:

```bash
python -m unittest -v test_floodguard.py
```

To run the HTTP smoke test, start the server in one terminal and then run this from another terminal in the project root:

```bash
python smoke_test.py
```

The smoke test creates a report named “Smoke test report” in your local database. Remove it from the dashboard if you do not want to keep it.

## Route comparison usage

1. Scroll to **Flood-aware route comparison**.
2. Search a starting place and destination and select a match, or use **Pick origin on map** and **Pick destination on map**. Manual coordinate entry is available under each location.
3. Optionally use **Use my location** for the origin; the browser asks for location permission.
4. Select **Compare routes**.
5. Select a route card or route line to highlight it on the map.

Geocoding resolves a place to a coordinate; review the selected match and pin/coordinates before routing. If OSRM cannot find a route, try choosing points closer to mapped roads. Alternative routes depend on what the routing service returns; not every trip will have multiple options. Estimated time does not include live traffic.

## API

- `GET /api/health`
- `GET /api/dashboard`
- `GET /api/reports?limit=100&offset=0`
- `POST /api/reports`
- `PATCH /api/reports/{id}/status` with JSON such as `{"status":"Under review"}`
- `GET /api/risk`
- `GET /api/weather?latitude=12.9716&longitude=77.5946`
- `GET /api/prediction/flood?latitude=12.9716&longitude=77.5946`
- `POST /api/risk/estimate`
- `POST /api/routes/compare` with JSON such as:
- `POST /api/geocode` with JSON such as `{"query":"Majestic, Bengaluru"}` (user-triggered place lookup)

```json
{
  "origin_lat": 12.9716,
  "origin_lon": 77.5946,
  "destination_lat": 12.9352,
  "destination_lon": 77.6245
}
```

The route response includes route geometry, distance, duration, a nearby-report list, an exposure index and a disclaimer. The score weights, 250 m route matching radius and 72-hour report window are prototype choices; validate and tune them against real historical data before any public safety use.

## Configuration

- `FLOODGUARD_DB_PATH`: optional database path. Defaults to `floodguard.db` in the project root.
- `FLOODGUARD_OSRM_URL`: optional routing service base URL. Defaults to `https://router.project-osrm.org`.
- `FLOODGUARD_NOMINATIM_URL`: optional place-search service base URL. Defaults to `https://nominatim.openstreetmap.org`.
- `FLOODGUARD_GEOCODE_USER_AGENT`: identifying user-agent for the place-search service. Set a contact URL or email before deployment.
- `FLOODGUARD_MODEL_PATH`: optional model file path; defaults to `models/bengaluru_flood_model.joblib`.
- `FLOODGUARD_MODEL_META_PATH`: optional model metadata path; defaults beside the model.

The included prototype does not need third-party API keys. OSRM route duration is a routing estimate, not live traffic. Flood-aware ranking considers only geolocated, non-resolved community reports from the last 72 hours within about 250 m of route geometry. It does not incorporate official flood-prone area polygons, weather along roads, verified closures, or real-time flooding. Lower known exposure means fewer/weaker matching reports in this database, not a safe route guarantee.

For public deployment, add authenticated reviewer/admin actions, abuse controls, rate limits, monitoring, backups, privacy disclosures and an agreed data-retention policy. Consider PostgreSQL/PostGIS when geographic data and concurrent usage grow.

## Live weather integration

The **Weather & AI** section uses the Open-Meteo Forecast API (no API key required for eligible non-commercial use) for current conditions and hourly precipitation forecasts. The backend caches weather responses for 10 minutes. API endpoints:

- `GET /api/weather?latitude=12.9716&longitude=77.5946`
- `GET /api/prediction/flood?latitude=12.9716&longitude=77.5946`

Weather is fetched at the requested coordinates. The default coordinates are central Bengaluru. You can enter other coordinates in the dashboard. A failed weather request is shown as unavailable rather than replaced with invented live values.

## Experimental machine-learning model

The project includes `train_model.py`. It downloads the India Flood Inventory v3 dataset from Zenodo and daily historical weather for Bengaluru from the Open-Meteo Historical Weather API, creates a district-level event label, trains a Random Forest classifier, reports chronological holdout metrics, and saves the model and metadata in `models/`.

Run after installing the updated requirements, from the project root:

```powershell
.\.venv\Scripts\python.exe train_model.py
```

Then restart the API and click **Refresh weather & prediction** in the dashboard. If training stops because there are too few explicit Bengaluru Urban labels or not enough weather coverage, the app will deliberately keep the ML prediction unavailable. Do not create a fake model or use synthetic labels to make it look trained.

**Research and safety limitations:** the India Flood Inventory is a historical, district-scale flood-event inventory, not a complete Bengaluru street-waterlogging dataset. The trained model's output is an experimental district-level event signal and must not be described as a validated street-level flood probability. Review the chronological holdout metrics and source coverage; collect and validate local flood/road-closure labels before using it for decisions. The Zenodo record lists CC BY-NC 4.0, so check its terms before commercial use.

Sources:

- Open-Meteo Forecast API docs: https://open-meteo.com/en/docs
- Open-Meteo Historical Weather API docs: https://open-meteo.com/en/docs/historical-weather-api
- India Flood Inventory v3 (Zenodo record 16994648): https://doi.org/10.5281/zenodo.16994648
