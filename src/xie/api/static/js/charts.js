/* XBRL Intelligence Engine — ApexCharts builders (progressive enhancement).
   Charts read data from an inline <script type="application/json"> tag, so the
   page is fully usable (tables) even if this script or ApexCharts fails to load.
   Glass is chrome only; charts render on solid panels with high-contrast ink. */
(function () {
  "use strict";
  if (typeof ApexCharts === "undefined") return;

  var REDUCED = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  var INK = "#94A3B8", GRID = "rgba(255,255,255,.07)", ACCENT = "#22C55E";
  var SERIES_COLORS = ["#22C55E", "#60A5FA", "#F59E0B", "#A78BFA", "#F87171"];
  var DASHES = [0, 4, 2, 6, 3]; // distinguish series without relying on color

  function fmtFull(n) {
    if (n == null || isNaN(n)) return "—";
    return new Intl.NumberFormat("en-US", { maximumFractionDigits: 2 }).format(n);
  }
  function fmtCompact(n) {
    if (n == null || isNaN(n)) return "";
    return new Intl.NumberFormat("en-US", { notation: "compact", maximumFractionDigits: 1 }).format(n);
  }
  function readJSON(id) {
    var el = document.getElementById(id);
    if (!el) return null;
    try { return JSON.parse(el.textContent); } catch (e) { return null; }
  }

  var baseTheme = {
    chart: { background: "transparent", foreColor: INK, fontFamily: "Fira Sans, sans-serif",
             toolbar: { show: false }, animations: { enabled: !REDUCED } },
    theme: { mode: "dark" },
    grid: { borderColor: GRID, strokeDashArray: 3 },
    tooltip: { theme: "dark", y: { formatter: fmtFull } },
    legend: { labels: { colors: INK }, markers: { radius: 3 } },
    states: { hover: { filter: { type: "lighten", value: 0.08 } } }
  };

  function merge() {
    var out = {}; for (var i = 0; i < arguments.length; i++) { var s = arguments[i];
      for (var k in s) if (s.hasOwnProperty(k)) out[k] = s[k]; } return out;
  }

  function timeSeries(host, d) {
    var opts = merge(baseTheme, {
      chart: merge(baseTheme.chart, { type: "area", height: host.dataset.height || 320,
        zoom: { enabled: false } }),
      series: d.series,
      colors: SERIES_COLORS,
      stroke: { curve: "smooth", width: 2.5, dashArray: d.series.map(function (_, i) { return DASHES[i % DASHES.length]; }) },
      fill: { type: "gradient", gradient: { shadeIntensity: 1, opacityFrom: 0.22, opacityTo: 0.02, stops: [0, 95] } },
      dataLabels: { enabled: false },
      markers: { size: 3, strokeWidth: 0, hover: { size: 5 } },
      xaxis: { categories: d.categories, axisBorder: { color: GRID }, axisTicks: { color: GRID },
               labels: { style: { colors: INK } } },
      yaxis: { labels: { formatter: fmtCompact, style: { colors: INK } } },
      accessibility: { description: d.aria || "Financial time series" }
    });
    return opts;
  }

  function barH(host, d) {
    return merge(baseTheme, {
      chart: merge(baseTheme.chart, { type: "bar", height: host.dataset.height || 280 }),
      series: [{ name: d.name || "Count", data: d.values }],
      colors: d.colors || [ACCENT],
      plotOptions: { bar: { horizontal: true, borderRadius: 4, distributed: !!d.colors,
        dataLabels: { position: "top" } } },
      dataLabels: { enabled: true, formatter: fmtCompact, style: { colors: ["#E2E8F0"] }, offsetX: 18 },
      legend: { show: false },
      xaxis: { categories: d.categories, labels: { formatter: fmtCompact, style: { colors: INK } } },
      yaxis: { labels: { style: { colors: INK } } },
      tooltip: { theme: "dark", y: { formatter: fmtFull } },
      accessibility: { description: d.aria || "Comparison bar chart" }
    });
  }

  function spark(host, d) {
    var pos = d.values.length < 2 || d.values[d.values.length - 1] >= d.values[0];
    return {
      chart: { type: "area", height: 40, sparkline: { enabled: true }, background: "transparent",
               animations: { enabled: !REDUCED } },
      series: [{ name: d.name || "", data: d.values }],
      colors: [pos ? "#22C55E" : "#F87171"],
      stroke: { curve: "smooth", width: 2 },
      fill: { type: "gradient", gradient: { opacityFrom: 0.35, opacityTo: 0 } },
      tooltip: { enabled: false }
    };
  }

  var BUILDERS = { line: timeSeries, area: timeSeries, bar: barH, spark: spark };

  function init() {
    var hosts = document.querySelectorAll("[data-chart]");
    Array.prototype.forEach.call(hosts, function (host) {
      if (host.dataset.rendered) return;
      var data = readJSON(host.dataset.series);
      var build = BUILDERS[host.dataset.chart];
      if (!data || !build) return;
      try {
        var chart = new ApexCharts(host, build(host, data));
        chart.render();
        host.dataset.rendered = "1";
      } catch (e) { /* leave the table fallback in place */ }
    });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
  // re-init after HTMX swaps
  document.body && document.body.addEventListener("htmx:afterSwap", init);
})();
