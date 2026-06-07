/* XBRL Intelligence Engine — small UI behaviours. No framework.
   - drag-and-drop file label (upload page)
   - client-side table sort (progressive; tables work without it)
   - HTMX active-state for segmented controls */
(function () {
  "use strict";

  /* ---- drag & drop upload ---- */
  function initDropzone() {
    var form = document.getElementById("upload-form");
    if (!form) return;
    var input = form.querySelector('input[type=file]');
    var zone = document.getElementById("dropzone");
    var name = document.getElementById("filename");
    if (!input || !zone) return;
    function setName() { if (name) name.textContent = input.files[0] ? input.files[0].name : ""; }
    input.addEventListener("change", setName);
    ["dragenter", "dragover"].forEach(function (ev) {
      zone.addEventListener(ev, function (e) { e.preventDefault(); zone.classList.add("dragging"); });
    });
    ["dragleave", "drop"].forEach(function (ev) {
      zone.addEventListener(ev, function (e) { e.preventDefault(); zone.classList.remove("dragging"); });
    });
    zone.addEventListener("drop", function (e) {
      if (e.dataTransfer.files.length) { input.files = e.dataTransfer.files; setName(); }
    });
  }

  /* ---- accessible client-side sort for tables.sortable ---- */
  function cellValue(row, i) {
    var c = row.children[i]; if (!c) return "";
    var raw = c.getAttribute("data-sort");
    var v = raw != null ? raw : c.textContent.trim();
    var n = parseFloat(v.replace(/[, %+]/g, ""));
    return isNaN(n) ? v.toLowerCase() : n;
  }
  function initSort() {
    document.querySelectorAll("table.sortable").forEach(function (table) {
      var headers = table.tHead ? table.tHead.rows[0].cells : [];
      Array.prototype.forEach.call(headers, function (th, idx) {
        if (th.dataset.noSort != null) return;
        th.classList.add("sortable"); th.tabIndex = 0; th.setAttribute("role", "button");
        function sort() {
          var asc = th.getAttribute("aria-sort") !== "ascending";
          Array.prototype.forEach.call(headers, function (h) { h.removeAttribute("aria-sort"); });
          th.setAttribute("aria-sort", asc ? "ascending" : "descending");
          var body = table.tBodies[0];
          var rows = Array.prototype.slice.call(body.rows);
          rows.sort(function (a, b) {
            var x = cellValue(a, idx), y = cellValue(b, idx);
            if (x < y) return asc ? -1 : 1; if (x > y) return asc ? 1 : -1; return 0;
          });
          rows.forEach(function (r) { body.appendChild(r); });
        }
        th.addEventListener("click", sort);
        th.addEventListener("keydown", function (e) {
          if (e.key === "Enter" || e.key === " ") { e.preventDefault(); sort(); }
        });
      });
    });
  }

  /* ---- segmented control active state on HTMX swaps ---- */
  function initSegmented() {
    document.body.addEventListener("click", function (e) {
      var t = e.target.closest && e.target.closest(".segmented [hx-get], .segmented [data-seg]");
      if (!t) return;
      var group = t.closest(".segmented");
      group.querySelectorAll(".active").forEach(function (a) { a.classList.remove("active"); });
      t.classList.add("active");
    });
  }

  function init() { initDropzone(); initSort(); initSegmented(); }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
