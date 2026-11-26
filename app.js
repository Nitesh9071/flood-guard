const isNativeApp = () => (typeof __FLOODGUARD_NATIVE_APP__ !== "undefined" && __FLOODGUARD_NATIVE_APP__) || Boolean(window.Capacitor?.isNativePlatform?.());
const defaultApiBase = isNativeApp() ? "" : `${location.protocol}//${location.host}/api`;
let API = localStorage.getItem("floodguard_api_base") || defaultApiBase;
function normalizeApiBase(value) {
  const url = new URL(value.trim());
  const host = url.hostname.toLowerCase();
  const octets = host.split(".").map(Number);
  const isPrivateLanIp = octets.length === 4 && octets.every(n => Number.isInteger(n) && n >= 0 && n <= 255) &&
    (octets[0] === 10 || (octets[0] === 172 && octets[1] >= 16 && octets[1] <= 31) ||
      (octets[0] === 192 && octets[1] === 168) || octets[0] === 127);
  if (url.protocol !== "https:" && !(url.protocol === "http:" && (isPrivateLanIp || host === "localhost"))) {
    throw new Error("Use HTTPS, or HTTP only for a private Wi-Fi address such as http://192.168.1.20:8000.");
  }
  if (url.search || url.hash) throw new Error("Enter a server address without query parameters or fragments.");
  if (!(["", "/", "/api", "/api/"]).includes(url.pathname)) throw new Error("Enter the server base address, optionally ending in /api.");
  return url.href.replace(/\/$/, "").replace(/\/api$/, "") + "/api";
}
function getDevicePosition(success, failure, options) {
  if (!isNativeApp()) {
    if (!navigator.geolocation) { failure({code:2}); return; }
    navigator.geolocation.getCurrentPosition(success, failure, options);
    return;
  }
  import("@capacitor/geolocation").then(async ({Geolocation}) => {
    let permissions = await Geolocation.checkPermissions();
    if (permissions.location !== "granted") permissions = await Geolocation.requestPermissions({permissions:["location"]});
    if (permissions.location !== "granted") throw Object.assign(new Error("Location permission was not granted."), {code:1});
    const position = await Geolocation.getCurrentPosition({...options, maximumAge:0, timeout:25000, enableLocationFallback:true});
    success(position);
  }).catch(failure);
}
let reports = [];
let map;
let markerLayer;
let selectionArmed = false;
let selectionMarker;
let routeLayer;
let routePickTarget = null;
let routeOptions = [];
let selectedRouteOption = 1;
let weatherPickArmed = false;
let weatherMarkerLayer;
const $ = (id) => document.getElementById(id);
const escapeHtml = (value) => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const colorFor = (severity) => severity === "high" ? "#dc4c58" : severity === "moderate" ? "#d99020" : "#1b9a75";
const formatDate = (value) => {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "Unknown" : date.toLocaleString();
};

function setMessage(message, error = false) {
  $("formMessage").textContent = message;
  $("formMessage").style.color = error ? "#bb3340" : "#16815f";
}
async function api(path, options = {}) {
  if (!API) throw new Error("Connect this app to a FloodGuard server first. Tap Server settings.");
  if (!API) throw new Error("Connect this app to a FloodGuard server first. Tap Server settings.");
  const response = await fetch(`${API}${path}`, {
    ...options,
    headers: { ...(options.body ? {"Content-Type":"application/json"} : {}), ...(options.headers || {}) }
  });
  let body;
  try { body = await response.json(); } catch { body = {}; }
  if (!response.ok) {
    const detail = body.detail;
    throw new Error(typeof detail === "string" ? detail : "The request could not be completed.");
  }
  return body;
}
async function reversePlaceAt(latitude, longitude) {
  try {
    const result = await api("/places/reverse?latitude=" + encodeURIComponent(latitude) + "&longitude=" + encodeURIComponent(longitude));
    return result.display_name || "";
  } catch { return ""; }
}
async function loadReports() {
  try {
    reports = await api("/reports");
    $("connectionState").textContent = "API connected";
    $("connectionState").classList.add("connected");
    refresh();
  } catch (error) {
    $("connectionState").textContent = "API unavailable";
    $("connectionState").classList.remove("connected");
    $("incidentRows").innerHTML = `<tr><td colspan="6">Could not load reports. Start the backend, then refresh. ${escapeHtml(error.message)}</td></tr>`;
    $("totalReports").textContent = "—";
    $("highReports").textContent = "—";
    $("reviewReports").textContent = "—";
    setMessage("The dashboard needs the backend. Run the start command in README.md.", true);
  }
}
function updateStats() {
  $("totalReports").textContent = reports.length;
  $("highReports").textContent = reports.filter(r => r.severity === "high").length;
  $("reviewReports").textContent = reports.filter(r => r.status === "Under review").length;
}
function renderRows() {
  const tbody = $("incidentRows");
  if (!reports.length) {
    tbody.innerHTML = '<tr><td colspan="6">No reports yet. Submit an observed incident to see it here.</td></tr>';
    return;
  }
  tbody.innerHTML = reports.map(r => `<tr>
    <td>${escapeHtml(r.location)}</td><td><span class="severity ${escapeHtml(r.severity)}">${escapeHtml(r.severity)}</span></td>
    <td><b>${Number(r.risk)}/100</b></td><td><span class="status">${escapeHtml(r.status)}</span></td>
    <td>${escapeHtml(formatDate(r.created_at))}</td><td><button class="row-action" data-id="${Number(r.id)}">${r.status === "Resolved" ? "Reopen" : r.status === "Under review" ? "Resolve" : "Review"}</button></td>
  </tr>`).join("");
  tbody.querySelectorAll("button[data-id]").forEach(button => button.addEventListener("click", async () => {
    const report = reports.find(r => r.id === Number(button.dataset.id));
    if (!report) return;
    const status = report.status === "Reported" ? "Under review" : report.status === "Under review" ? "Resolved" : "Reported";
    button.disabled = true;
    try { await api(`/reports/${report.id}/status`, {method:"PATCH", body:JSON.stringify({status})}); await loadReports(); }
    catch (error) { setMessage(`Could not update status: ${error.message}`, true); button.disabled = false; }
  }));
}
function renderMap() {
  if (!map || !markerLayer) return;
  markerLayer.clearLayers();
  reports.forEach(r => {
    if (r.latitude == null || r.longitude == null) return;
    const marker = L.circleMarker([Number(r.latitude), Number(r.longitude)], {
      radius: 9, color: "#fff", weight: 2, fillColor: colorFor(r.severity), fillOpacity: .95
    }).addTo(markerLayer);
    marker.bindPopup(`<strong>${escapeHtml(r.location)}</strong><br>Severity: ${escapeHtml(r.severity)}<br>Report score: ${Number(r.risk)}/100<br><small>${escapeHtml(r.description)}</small><br><em>Community report; not independently verified.</em>`);
  });
}
function renderRoutesOnMap() {
  if (!map || !routeLayer) return;
  routeLayer.clearLayers();
  routeOptions.forEach(route => {
    if (!Array.isArray(route.geometry) || !route.geometry.length) return;
    const color = route.exposure_score >= 40 ? "#dc4c58" : route.exposure_score >= 12 ? "#d99020" : "#1b9a75";
    const selected = route.option === selectedRouteOption;
    const latlngs = route.geometry.map(point => [Number(point[1]), Number(point[0])]);
    const line = L.polyline(latlngs, {color, weight:selected ? 7 : 4, opacity:selected ? .95 : .58, lineCap:"round", lineJoin:"round"}).addTo(routeLayer);
    line.bindPopup(`<strong>Route option ${route.option}</strong><br>${Math.round(route.duration_seconds/60)} min · ${route.exposure_score}/100 exposure index<br>${escapeHtml(route.exposure_label)}<br><small>Heuristic, not a forecast.</small>`);
    line.on("click", () => { selectedRouteOption = route.option; renderRoutesOnMap(); renderRouteCards(); });
  });
  if (routeOptions.length) {
    const selected = routeOptions.find(route => route.option === selectedRouteOption) || routeOptions[0];
    if (selected.geometry?.length) map.fitBounds(L.latLngBounds(selected.geometry.map(point => [point[1], point[0]]).concat([[Number($('originLat').value), Number($('originLon').value)], [Number($('destinationLat').value), Number($('destinationLon').value)]])), {padding:[24,24], maxZoom:14});
  }
}
function renderRouteCards() {
  const target = $("routeResults");
  if (!routeOptions.length) {
    target.innerHTML = '<div class="route-empty"><strong>No route comparison yet</strong><p>Choose an origin and destination, then select Compare routes.</p></div>';
    return;
  }
  target.innerHTML = routeOptions.map(route => {
    const selected = route.option === selectedRouteOption;
    const riskClass = route.exposure_score >= 40 ? "risk-high" : route.exposure_score >= 12 ? "risk-medium" : "risk-low";
    const minutes = Math.round(route.duration_seconds / 60);
    const km = (route.distance_m / 1000).toFixed(1);
    const fastest = route.is_fastest ? " · Fastest" : ` · +${route.delay_vs_fastest_minutes} min`;
    const hazardText = route.hazard_count ? `${route.hazard_count} recent report(s) near this route` : "No recent geolocated reports matched this route";
    const topHazards = route.nearby_hazards.slice(0, 2).map(h => `${escapeHtml(h.location)} (${escapeHtml(h.severity)}, ${h.distance_m} m away, ${h.age_hours} h old)`).join("; ");
    return `<article class="route-card ${selected ? "selected" : ""}" data-route-option="${route.option}" tabindex="0" role="button" aria-pressed="${selected}">
      <div class="route-card-head"><div><div class="route-card-title">${route.option === 1 ? "Suggested comparison option" : `Route option ${route.option}`}${fastest}</div><div class="route-card-sub">${minutes} min · ${km} km</div></div><span class="route-chip ${riskClass}">${escapeHtml(route.exposure_label)} · ${route.exposure_score}/100</span></div>
      <p>${escapeHtml(hazardText)}. This is an exposure index, not a probability of flooding.</p>
      ${topHazards ? `<div class="route-hazards">Nearby reports: ${topHazards}</div>` : ""}
    </article>`;
  }).join("");
  target.querySelectorAll("[data-route-option]").forEach(card => {
    const select = () => { selectedRouteOption = Number(card.dataset.routeOption); renderRouteCards(); renderRoutesOnMap(); };
    card.addEventListener("click", select);
    card.addEventListener("keydown", event => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); select(); } });
  });
}

function setupPlaceSearch(inputId, listId, {onSelect, onClear}) {
  const input = $(inputId), list = $(listId);
  let timer = 0, requestId = 0, controller = null;
  const close = () => { list.hidden = true; input.setAttribute("aria-expanded", "false"); };
  const showMessage = message => {
    list.replaceChildren();
    const row = document.createElement("div");
    row.className = "place-suggestion-message";
    row.textContent = message;
    list.append(row);
    list.hidden = false;
    input.setAttribute("aria-expanded", "true");
  };
  input.addEventListener("input", () => {
    onClear?.();
    clearTimeout(timer);
    controller?.abort();
    const query = input.value.trim();
    requestId += 1;
    const thisRequest = requestId;
    if (query.length < 2) { close(); return; }
    showMessage("Searching Bengaluru places…");
    timer = window.setTimeout(async () => {
      controller = new AbortController();
      try {
        const result = await api("/places/suggest?query=" + encodeURIComponent(query), {signal:controller.signal});
        if (thisRequest !== requestId || input.value.trim() !== query) return;
        list.replaceChildren();
        if (!result.matches?.length) {
          showMessage("No match found in the Bengaluru search area. Try a nearby landmark or area.");
          return;
        }
        result.matches.forEach(match => {
          const option = document.createElement("button");
          option.type = "button";
          option.className = "place-suggestion";
          option.setAttribute("role", "option");
          option.textContent = match.display_name;
          option.addEventListener("pointerdown", event => event.preventDefault());
          option.addEventListener("click", () => {
            input.value = match.display_name;
            close();
            onSelect(match);
            input.focus();
          });
          list.append(option);
        });
        list.hidden = false;
        input.setAttribute("aria-expanded", "true");
      } catch (error) {
        if (error.name === "AbortError" || thisRequest !== requestId) return;
        showMessage("Place search unavailable. Check the server and internet, or pin the location on the map.");
      }
    }, 450);
  });
  input.addEventListener("keydown", event => {
    if (event.key === "Escape") close();
    if (event.key === "ArrowDown" && !list.hidden) {
      const first = list.querySelector(".place-suggestion");
      if (first) { event.preventDefault(); first.focus(); }
    }
  });
  input.addEventListener("blur", () => window.setTimeout(() => {
    if (!list.contains(document.activeElement)) close();
  }, 120));
  list.addEventListener("keydown", event => {
    if (event.key === "Escape") { close(); input.focus(); }
  });
  return {close};
}

function setRoutePlace(which, match) {
  const prefix = which === "origin" ? "origin" : "destination";
  $(prefix + "Lat").value = Number(match.latitude).toFixed(6);
  $(prefix + "Lon").value = Number(match.longitude).toFixed(6);
  $("routeMessage").classList.remove("error");
  $("routeMessage").textContent = (which === "origin" ? "Starting point" : "Destination") + " set to " + match.display_name + ".";
}
function setRoutePoint(which, latitude, longitude, label) {
  const prefix = which === "origin" ? "origin" : "destination";
  const latText = Number(latitude).toFixed(6), lonText = Number(longitude).toFixed(6);
  $(prefix + "Lat").value = latText;
  $(prefix + "Lon").value = lonText;
  $(prefix + "Place").value = label;
  reversePlaceAt(latitude, longitude).then(place => {
    if (!place || $(prefix + "Lat").value !== latText || $(prefix + "Lon").value !== lonText) return;
    $(prefix + "Place").value = place;
  });
}

setupPlaceSearch("location", "reportPlaceSuggestions", {
  onClear: () => { $("latitude").value = ""; $("longitude").value = ""; },
  onSelect: match => {
    $("latitude").value = Number(match.latitude).toFixed(6);
    $("longitude").value = Number(match.longitude).toFixed(6);
    $("locationHint").textContent = "Location selected. The pin will be included with your report.";
  }
});
setupPlaceSearch("originPlace", "originPlaceSuggestions", {
  onClear: () => { $("originLat").value = ""; $("originLon").value = ""; },
  onSelect: match => setRoutePlace("origin", match)
});
setupPlaceSearch("destinationPlace", "destinationPlaceSuggestions", {
  onClear: () => { $("destinationLat").value = ""; $("destinationLon").value = ""; },
  onSelect: match => setRoutePlace("destination", match)
});

function refresh() { updateStats(); renderRows(); renderMap(); }

$("reportForm").addEventListener("submit", async event => {
  event.preventDefault();
  // Capture the form before awaiting fetch: event.currentTarget becomes null after dispatch.
  const form = event.currentTarget;
  const latRaw = $("latitude").value.trim(), lonRaw = $("longitude").value.trim();
  const latitude = latRaw === "" ? null : Number(latRaw), longitude = lonRaw === "" ? null : Number(lonRaw);
  if (latitude === null || longitude === null) { setMessage("Choose a place suggestion, use your location, or pin the incident on the map.", true); $("location").focus(); return; }
  if (latitude !== null && (!Number.isFinite(latitude) || latitude < -90 || latitude > 90 || longitude < -180 || longitude > 180)) {
    setMessage("Please enter valid coordinates.", true); return;
  }
  const submit = event.currentTarget.querySelector("button[type=submit]");
  submit.disabled = true;
  setMessage("Saving report…");
  try {
    await api("/reports", {method:"POST", body:JSON.stringify({
      location:$("location").value.trim(), latitude, longitude,
      severity:$("severity").value, description:$("description").value.trim()
    })});
    form.reset();
    setMessage("Report saved to the shared SQLite database.");
    await loadReports();
  } catch (error) { setMessage(`Report not saved: ${error.message}`, true); }
  finally { submit.disabled = false; }
});

$("routeForm").addEventListener("submit", async event => {
  event.preventDefault();
  const values = ["originLat", "originLon", "destinationLat", "destinationLon"].map(id => Number($(id).value));
  if (values.some(value => !Number.isFinite(value))) { $("routeMessage").textContent = "Choose a suggested Bengaluru place, use your location, or pin both route points on the map."; $("routeMessage").classList.add("error"); return; }
  if (values[0] < -90 || values[0] > 90 || values[2] < -90 || values[2] > 90 || values[1] < -180 || values[1] > 180 || values[3] < -180 || values[3] > 180) {
    $("routeMessage").textContent = "Coordinates are outside valid latitude/longitude ranges."; $("routeMessage").classList.add("error"); return;
  }
  const button = $("compareRoutes"); button.disabled = true; button.textContent = "Finding routes…";
  $("routeMessage").classList.remove("error"); $("routeMessage").textContent = "Requesting candidate routes and checking recent reports…";
  try {
    const result = await api("/routes/compare", {method:"POST", body:JSON.stringify({origin_lat:values[0], origin_lon:values[1], destination_lat:values[2], destination_lon:values[3]})});
    routeOptions = result.routes || []; selectedRouteOption = routeOptions[0]?.option || 1;
    renderRouteCards(); renderRoutesOnMap();
    $("routeMessage").textContent = `${routeOptions.length} route option(s) returned. ${result.known_report_count} recent geolocated report(s) were eligible for matching. ${result.disclaimer}`;
  } catch (error) {
    routeOptions = []; if (routeLayer) routeLayer.clearLayers(); renderRouteCards();
    $("routeMessage").textContent = `Route comparison failed: ${error.message}`; $("routeMessage").classList.add("error");
  } finally { button.disabled = false; button.innerHTML = 'Compare routes <span>→</span>'; }
});
function armRoutePick(target) {
  if (!map) { $("routeMessage").textContent = "Map is unavailable. Search for a Bengaluru place instead."; return; }
  routePickTarget = target;
  selectionArmed = false;
  weatherPickArmed = false;
  map.getContainer().classList.add("pick-location");
  $("routeMessage").classList.remove("error");
  $("routeMessage").textContent = `Click the map to set the ${target === "origin" ? "origin" : "destination"}.`;
  $("map-section").scrollIntoView({behavior:"smooth", block:"center"});
  window.setTimeout(() => map.invalidateSize(), 350);
}
$("pickOrigin").addEventListener("click", () => armRoutePick("origin"));
$("pickDestination").addEventListener("click", () => armRoutePick("destination"));
$("useRouteLocation").addEventListener("click", () => {
  const button = $("useRouteLocation"); button.disabled = true; $("routeMessage").textContent = "Requesting location permission…";
  getDevicePosition(position => {
    const accuracy = Number(position.coords.accuracy);
    setRoutePoint("origin", position.coords.latitude, position.coords.longitude, "My location (±" + Math.round(accuracy) + " m)");
    $("routeMessage").textContent = `Current location set as origin (about ±${Math.round(accuracy)} m accuracy). Verify it on the map before routing.`; button.disabled = false;
    if (map) map.flyTo([position.coords.latitude, position.coords.longitude], Math.max(map.getZoom(), 13));
  }, error => {
    $("routeMessage").textContent = error.code === 1 ? "Location permission was denied. Allow precise location in Android settings, or pick the origin on the map." : "Could not determine location. Turn on device location and try again, or pick the origin on the map.";
    button.disabled = false;
  }, {enableHighAccuracy:true, timeout:25000, maximumAge:0});
});


function setWeatherMarker(latitude, longitude) {
  if (!weatherMarkerLayer || !window.L) return;
  weatherMarkerLayer.clearLayers();
  L.circleMarker([latitude, longitude], {radius:9, color:"#fff", weight:3, fillColor:"#2674e8", fillOpacity:.95})
    .addTo(weatherMarkerLayer).bindPopup("Weather location");
}

function setWeatherLocation(latitude, longitude, label, shouldRefresh = true) {
  const lat = Number(latitude), lon = Number(longitude);
  const latText = lat.toFixed(6), lonText = lon.toFixed(6);
  $("weatherLat").value = latText;
  $("weatherLon").value = lonText;
  $("weatherPlace").value = label;
  $("weatherLocationHint").textContent = "Weather location: " + label + ".";
  setWeatherMarker(lat, lon);
  if (map) map.flyTo([lat, lon], Math.max(map.getZoom(), 13));
  reversePlaceAt(lat, lon).then(place => {
    if (!place || $("weatherLat").value !== latText || $("weatherLon").value !== lonText) return;
    $("weatherPlace").value = place;
    $("weatherLocationHint").textContent = "Weather location: " + place + ".";
  });
  if (shouldRefresh) refreshWeatherAndPrediction();
}

setupPlaceSearch("weatherPlace", "weatherPlaceSuggestions", {
  onClear: () => { $("weatherLat").value = ""; $("weatherLon").value = ""; },
  onSelect: match => setWeatherLocation(match.latitude, match.longitude, match.display_name)
});

$("weatherUseLocation").addEventListener("click", () => {
  const button = $("weatherUseLocation");
  button.disabled = true;
  $("weatherLocationHint").textContent = "Getting your current location…";
  getDevicePosition(position => {
    const accuracy = Math.round(Number(position.coords.accuracy));
    setWeatherLocation(position.coords.latitude, position.coords.longitude, "My location (about ±" + accuracy + " m)");
    button.disabled = false;
  }, error => {
    $("weatherLocationHint").textContent = error.code === 1
      ? "Location permission was denied. Allow location access or search for a Bengaluru place."
      : "Could not determine your location. Turn on device location or search for a place.";
    button.disabled = false;
  }, {enableHighAccuracy:true, timeout:25000, maximumAge:0});
});

$("weatherPickOnMap").addEventListener("click", () => {
  if (!map) { $("weatherLocationHint").textContent = "Map unavailable. Search for a Bengaluru place or use your location."; return; }
  weatherPickArmed = true;
  selectionArmed = false;
  routePickTarget = null;
  map.getContainer().classList.add("pick-location");
  $("weatherLocationHint").textContent = "Tap the map to set the weather location.";
  $("map-section").scrollIntoView({behavior:"smooth", block:"center"});
  window.setTimeout(() => map.invalidateSize(), 350);
});

async function refreshWeatherAndPrediction() {
  const latitude = Number($("weatherLat").value);
  const longitude = Number($("weatherLon").value);
  if (!Number.isFinite(latitude) || latitude < -90 || latitude > 90 || !Number.isFinite(longitude) || longitude < -180 || longitude > 180) {
    $("weatherSummary").textContent = "Choose a suggested Bengaluru place, use your location, or pin a point on the map.";
    return;
  }
  const button = $("refreshWeather");
  button.disabled = true;
  $("weatherSummary").textContent = "Fetching live weather…";
  $("predictionResult").textContent = "Evaluating model status…";
  try {
    const result = await api(`/prediction/flood?latitude=${encodeURIComponent(latitude)}&longitude=${encodeURIComponent(longitude)}`);
    const weather = result.weather || {};
    const current = weather.current || {};
    const forecast = weather.forecast || {};
    const temp = current.temperature_2m;
    const humidity = current.relative_humidity_2m;
    const rainNow = current.precipitation;
    const rainNext = forecast.rainfall_next_24h_mm;
    const rainProbability = forecast.precipitation_probability_max_next_24h_pct;
    $("weatherSummary").innerHTML = `<div class="weather-metrics">
      <div><small>Temperature</small><strong>${temp == null ? "—" : `${Number(temp).toFixed(1)} °C`}</strong></div>
      <div><small>Humidity</small><strong>${humidity == null ? "—" : `${Number(humidity).toFixed(0)}%`}</strong></div>
      <div><small>Rain now</small><strong>${rainNow == null ? "—" : `${Number(rainNow).toFixed(1)} mm`}</strong></div>
      <div><small>Forecast rain · 24h</small><strong>${rainNext == null ? "—" : `${Number(rainNext).toFixed(1)} mm`}</strong></div>
      <div><small>Max rain chance · 24h</small><strong>${rainProbability == null ? "—" : `${Number(rainProbability).toFixed(0)}%`}</strong></div>
      <div><small>Next 24h rainy hours</small><strong>${forecast.rain_hours_next_24h ?? "—"}</strong></div>
      </div><p>Source: ${escapeHtml(weather.source || "Open-Meteo")} · Updated ${escapeHtml(formatDate(weather.fetched_at))} · Cache ${Number(weather.cache_seconds || 600) / 60} min.</p>`;
    $("weatherStat").textContent = "Live connected";
    $("weatherStatDetail").textContent = `Next 24h rain: ${rainNext == null ? "—" : `${Number(rainNext).toFixed(1)} mm`}`;
    if (result.prediction_available) {
      const metrics = result.model_metadata?.chronological_holdout_metrics;
      const metricText = metrics ? `Holdout metrics: ${escapeHtml(JSON.stringify(metrics))}` : "Holdout metrics unavailable.";
      $("predictionResult").innerHTML = `<div class="prediction-score"><small>Experimental model signal</small><strong>${Number(result.probability_percent).toFixed(1)}%</strong><span>${escapeHtml(result.risk_band)}</span></div><p>Model: ${escapeHtml(result.model_metadata?.model_type || "trained model")}</p><p>${metricText}</p><p>Training examples: ${Number(result.model_metadata?.training_rows || 0)} daily records.</p><small>${escapeHtml(result.disclaimer || "Experimental model output; not a calibrated forecast.")}</small>`;
    } else {
      $("predictionResult").innerHTML = `<div class="model-not-ready"><strong>ML model not trained yet</strong><p>Live weather is working, but the app will not invent an AI probability. Train the model from historical weather plus the India Flood Inventory.</p><ol><li>Install the updated requirements.</li><li>Run <code>python train_model.py</code> from the project folder.</li><li>Refresh this panel after training completes.</li></ol></div>`;
    }
  } catch (error) {
    $("weatherSummary").textContent = `Live weather unavailable: ${error.message}. Check internet access and retry.`;
    $("predictionResult").textContent = "Prediction unavailable because live weather could not be loaded.";
    $("weatherStat").textContent = "Unavailable";
    $("weatherStatDetail").textContent = "Check internet connection";
  } finally {
    button.disabled = false;
    button.innerHTML = 'Refresh weather & prediction <span>↻</span>';
  }
}
$("weatherLocationForm").addEventListener("submit", event => { event.preventDefault(); refreshWeatherAndPrediction(); });

$("serverSettingsButton").addEventListener("click", () => {
  $("serverUrl").value = API ? API.replace(/\/api$/, "") : "";
  $("serverMessage").textContent = "";
  $("serverDialog").showModal();
});
$("serverCancel").addEventListener("click", () => $("serverDialog").close());
$("serverForm").addEventListener("submit", async event => {
  event.preventDefault();
  const submit = event.currentTarget.querySelector("button[type=submit]");
  submit.disabled = true; $("serverMessage").textContent = "Checking server…";
  try {
    const candidate = normalizeApiBase($("serverUrl").value);
    const response = await fetch(candidate + "/health", {signal:AbortSignal.timeout(10000)});
    const health = await response.json();
    if (!response.ok || health.status !== "ok") throw new Error("The server did not return a healthy FloodGuard API response.");
    API = candidate; localStorage.setItem("floodguard_api_base", API);
    $("serverDialog").close();
    await loadReports(); await refreshWeatherAndPrediction();
  } catch (error) { $("serverMessage").textContent = error.message || "Could not reach that server. Check its URL and internet connection."; }
  finally { submit.disabled = false; }
});

document.querySelectorAll(".nav").forEach(button => button.addEventListener("click", () => {
  document.querySelectorAll(".nav").forEach(nav => nav.classList.remove("active"));
  button.classList.add("active");
  document.getElementById(button.dataset.view)?.scrollIntoView({behavior:"smooth",block:"start"});
}));
function setSelectedLocation(latitude, longitude, label) {
  const lat = Number(latitude), lon = Number(longitude);
  const latText = lat.toFixed(6), lonText = lon.toFixed(6);
  $("latitude").value = latText;
  $("longitude").value = lonText;
  $("location").value = label;
  $("locationHint").textContent = label + " selected. This location will be included with the report.";
  if (selectionMarker && window.L) {
    selectionMarker.clearLayers();
    L.marker([lat, lon]).addTo(selectionMarker).bindPopup(escapeHtml(label)).openPopup();
    map.flyTo([lat, lon], Math.max(map.getZoom(), 15), {duration: .5});
  }
  reversePlaceAt(lat, lon).then(place => {
    if (!place || $("latitude").value !== latText || $("longitude").value !== lonText) return;
    $("location").value = place;
    $("locationHint").textContent = place + " selected from your location pin.";
  });
}

$("pickOnMap").addEventListener("click", () => {
  if (!map) { $("locationHint").textContent = "The map is unavailable. Search for a place or use your current location."; return; }
  selectionArmed = true;
  routePickTarget = null;
  weatherPickArmed = false;
  map.getContainer().classList.add("pick-location");
  $("pickOnMap").textContent = "Now click the map…";
  $("locationHint").textContent = "Click the map at the incident location. Your pin will be added to the report.";
  $("map-section").scrollIntoView({behavior:"smooth",block:"center"});
  window.setTimeout(() => map.invalidateSize(), 350);
});

$("useMyLocation").addEventListener("click", () => {
  const button = $("useMyLocation");
  button.disabled = true;
  $("locationHint").textContent = "Requesting precise location permission…";
  getDevicePosition(position => {
    const accuracy = Number(position.coords.accuracy);
    const label = accuracy > 100 ? `Approximate current location (±${Math.round(accuracy)} m)` : `Current location (±${Math.round(accuracy)} m)`;
    setSelectedLocation(position.coords.latitude, position.coords.longitude, label);
    button.disabled = false;
  }, error => {
    const reason = error.code === 1 ? "Location permission was denied." : "Could not determine your location.";
    $("locationHint").textContent = `${reason} Choose a place suggestion or pin the location on the map.`;
    button.disabled = false;
  }, {enableHighAccuracy:true, timeout:25000, maximumAge:0});
});
if (window.L) {
  map = L.map("map").setView([12.9716, 77.5946], 11);
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {maxZoom:19, attribution:'&copy; OpenStreetMap contributors'}).addTo(map);
  markerLayer = L.layerGroup().addTo(map);
  selectionMarker = L.layerGroup().addTo(map);
  weatherMarkerLayer = L.layerGroup().addTo(map);
  setWeatherMarker(12.9716, 77.5946);
  routeLayer = L.layerGroup().addTo(map);
  map.on("click", event => {
    if (routePickTarget) {
      const target = routePickTarget;
      setRoutePoint(target, event.latlng.lat, event.latlng.lng, "Pinned map location");
      routePickTarget = null;
      map.getContainer().classList.remove("pick-location");
      $("routeMessage").textContent = (target === "origin" ? "Origin" : "Destination") + " pinned. Choose the other point, then compare routes.";
      return;
    }
    if (weatherPickArmed) {
      weatherPickArmed = false;
      map.getContainer().classList.remove("pick-location");
      setWeatherLocation(event.latlng.lat, event.latlng.lng, "Pinned map location");
      $("weatherLocationHint").textContent = "Map point selected. Loading weather and model signal…";
      return;
    }
    if (!selectionArmed) return;
    setSelectedLocation(event.latlng.lat, event.latlng.lng, "Map point selected");
    selectionArmed = false;
    map.getContainer().classList.remove("pick-location");
    $("pickOnMap").textContent = "Choose on map";
    $("locationHint").textContent = "Looking up the pinned place…";
    $("report-section").scrollIntoView({behavior:"smooth",block:"center"});
  });
} else {
  $("map").innerHTML = "<p style='padding:20px'>Map library unavailable. Check your internet connection.</p>";
}
if (isNativeApp() && !API) $("serverDialog").showModal();
loadReports();
refreshWeatherAndPrediction();



