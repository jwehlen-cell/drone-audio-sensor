// Leaflet-based map for the status page.
//
// Reads a JSON blob embedded as <script id="status-map-data"> by the server,
// then drops one circle marker per phone (colored by state + dimmed if
// stale) and one circle marker per recent detection.

(function () {
    "use strict";

    function whenLeafletReady(cb) {
        if (typeof L !== "undefined") {
            cb();
            return;
        }
        var fallbackStarted = false;
        function loadFallback() {
            if (typeof L !== "undefined") {
                cb();
                return;
            }
            if (fallbackStarted) return;
            fallbackStarted = true;
            var css = document.createElement("link");
            css.rel = "stylesheet";
            css.href = "https://cdn.jsdelivr.net/npm/leaflet@1.9.4/dist/leaflet.css";
            document.head.appendChild(css);

            var script = document.createElement("script");
            script.src = "https://cdn.jsdelivr.net/npm/leaflet@1.9.4/dist/leaflet.js";
            script.onload = function () { if (typeof L !== "undefined") cb(); };
            script.onerror = function () { showMapError("Map library failed to load."); };
            document.head.appendChild(script);
        }
        // Leaflet is loaded with `defer` — fall back to a tick.
        document.addEventListener("DOMContentLoaded", function () {
            if (typeof L !== "undefined") cb();
            else setTimeout(loadFallback, 250);
        });
        setTimeout(loadFallback, 1500);
    }

    whenLeafletReady(function () {
        var mapEl = document.getElementById("status-map");
        var dataEl = document.getElementById("status-map-data");
        if (!mapEl || !dataEl) return;

        var data;
        try {
            data = JSON.parse(dataEl.textContent || "{}");
        } catch (e) {
            console.error("Bad map data payload", e);
            return;
        }

        var phones = Array.isArray(data.phones) ? data.phones : [];
        var detections = Array.isArray(data.detections) ? data.detections : [];
        var staleWarn = Number(data.stale_warning_seconds) || 30;
        var staleOffline = Number(data.stale_offline_seconds) || 300;

        // Use OpenStreetMap tiles directly — no API key, but be kind to the
        // public tile server (this is internal admin traffic, low volume).
        var map = L.map(mapEl, { zoomControl: true });
        L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
            attribution: "© OpenStreetMap contributors",
            maxZoom: 19,
        }).addTo(map);

        var bounds = L.latLngBounds([]);

        function phoneColorFor(state, freshSec) {
            if (state === "wipe_sent") return "#000000";
            if (state === "wipe_requested") return "#c62828";
            if (state === "revoked") return "#4b5560";
            if (state === "lost") return "#b59100";
            if (state === "setup_pending") return "#1f6feb";
            if (freshSec !== null && freshSec > staleOffline) return "#4b5560";
            if (freshSec !== null && freshSec > staleWarn) return "#b59100";
            return "#2e7d32";
        }

        function fmtFreshness(s) {
            if (s === null || s === undefined) return "—";
            if (s < 60) return Math.round(s) + " s";
            if (s < 3600) return Math.round(s / 60) + " min";
            return Math.round(s / 3600) + " h";
        }

        phones.forEach(function (p) {
            if (typeof p.lat !== "number" || typeof p.lon !== "number") return;
            var fill = phoneColorFor(p.state, p.freshness_seconds);
            var marker = L.circleMarker([p.lat, p.lon], {
                radius: 9,
                color: "#ffffff",
                weight: 2,
                fillColor: fill,
                fillOpacity: 0.95,
            }).addTo(map);
            var html = [
                "<strong>" + escapeHtml(p.device_id) + "</strong>",
                p.site ? "<br>site: " + escapeHtml(p.site) : "",
                "<br>state: <code>" + escapeHtml(p.state) + "</code>",
                "<br>fresh: " + escapeHtml(fmtFreshness(p.freshness_seconds)),
                p.network_type ? "<br>net: " + escapeHtml(p.network_type) : "",
                Number.isFinite(p.battery_percent) && p.battery_percent >= 0
                    ? "<br>battery: " + p.battery_percent + "%"
                    : "",
            ].join("");
            marker.bindPopup(html);
            bounds.extend([p.lat, p.lon]);
        });

        detections.forEach(function (d) {
            if (typeof d.lat !== "number" || typeof d.lon !== "number") return;
            var marker = L.circleMarker([d.lat, d.lon], {
                radius: 6,
                color: "#ffffff",
                weight: 1,
                fillColor: "#ff5252",
                fillOpacity: 0.9,
            }).addTo(map);
            var html = [
                "<strong>Detection</strong>",
                "<br>device: <code>" + escapeHtml(d.device_id) + "</code>",
                d.site ? "<br>site: " + escapeHtml(d.site) : "",
                "<br>avg: " + (Number(d.average_score) || 0).toFixed(2) +
                    " peak: " + (Number(d.peak_score) || 0).toFixed(2),
                d.published_at_ms
                    ? "<br>at: " + new Date(d.published_at_ms).toLocaleString()
                    : "",
            ].join("");
            marker.bindPopup(html);
            bounds.extend([d.lat, d.lon]);
        });

        if (bounds.isValid()) {
            map.fitBounds(bounds.pad(0.25));
        } else {
            // No points yet — default to continental US view.
            map.setView([39.0, -98.0], 4);
        }
    });

    function escapeHtml(s) {
        if (s === null || s === undefined) return "";
        return String(s)
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;")
            .replace(/'/g, "&#39;");
    }

    function showMapError(message) {
        var mapEl = document.getElementById("status-map");
        if (!mapEl) return;
        mapEl.innerHTML = "";
        var div = document.createElement("div");
        div.style.padding = "16px";
        div.style.color = "#c62828";
        div.textContent = message;
        mapEl.appendChild(div);
    }
})();
