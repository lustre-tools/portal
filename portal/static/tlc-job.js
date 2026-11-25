/*
 * Update control for a page whose data a background job produces
 * (templates/tlc/_job.html), as the Gerrit Dashboard's Refresh works:
 * the button starts the job, a bar shows its progress, and the page
 * reloads once newer data is there.
 *
 * GET  data-job-status -> {running, phase, done, total, version,
 *                          updated_at, next_update_at, error, now}
 *      `version` is what the job last produced; `next_update_at` (epoch
 *      seconds, or null) is the earliest a new run may start.
 * POST data-job-update (X-CSRF-Token header) -> {started, message}
 */
(function () {
  "use strict";
  document.querySelectorAll("[data-job-status]").forEach(function (root) {
    function el(name) { return root.querySelector('[data-job-el="' + name + '"]'); }
    var statusEl = el("status"), progEl = el("prog"), labelEl = el("label");
    var fillEl = el("fill"), button = el("button");
    var shown = root.dataset.jobVersion || "";
    var timer = null, running = false, wait = null, skew = 0;

    function clock(ts) {
      var d = new Date(ts * 1000);
      return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    }

    function render(s) {
      running = !!s.running;
      if (s.now) skew = s.now - Date.now() / 1000;
      statusEl.classList.toggle("hidden", running);
      progEl.classList.toggle("hidden", !running);
      if (running) {
        if (s.total) {
          fillEl.classList.remove("indet");
          fillEl.style.width = Math.min(100, Math.round(100 * s.done / s.total)) + "%";
          labelEl.textContent = s.phase + " " + s.done + "/" + s.total;
        } else {
          fillEl.classList.add("indet");
          labelEl.textContent = (s.phase || "starting…") + (s.done ? " " + s.done : "");
        }
      } else {
        fillEl.style.width = "0";
        if (s.updated_at) {
          statusEl.dataset.gdEpoch = s.updated_at;
          statusEl.dataset.gdRel = "";
          statusEl.dataset.gdPrefix = s.error ? "update failed · last good " : "updated ";
          if (window.gdTime) window.gdTime.render(root);
        } else if (s.error) {
          statusEl.textContent = "update failed";
        }
        statusEl.title = s.error ? "Last update failed: " + s.error : statusEl.title;
      }
      wait = s.next_update_at || null;
      gate();
      if (!running && s.version && s.version !== shown) location.reload();
    }

    // The button says why it cannot be pressed instead of looking dead.
    function gate() {
      var now = Date.now() / 1000 + skew;
      var early = wait && wait > now;
      button.disabled = running || !!early;
      button.title = running ? "An update is running"
        : early ? "The next update can start at " + clock(wait - skew)
        : "Fetch the data again (takes a few minutes)";
    }

    function schedule(ms) { clearTimeout(timer); timer = setTimeout(poll, ms); }

    function poll() {
      fetch(root.dataset.jobStatus, { cache: "no-store", credentials: "same-origin" })
        .then(function (r) { return r.json(); })
        .then(render)
        .catch(function () { /* server briefly away; retry next tick */ })
        .then(function () { schedule(running ? 1000 : 30000); });
    }

    button.addEventListener("click", function () {
      button.disabled = true;
      fetch(root.dataset.jobUpdate, {
        method: "POST", credentials: "same-origin",
        headers: { "X-CSRF-Token": root.dataset.jobCsrf },
      })
        .then(function (r) { return r.json(); })
        .then(function (res) { if (!res.started && res.message) button.title = res.message; })
        .catch(function () {})
        .then(function () { schedule(300); });
    });

    setInterval(gate, 15000);
    poll();
  });
})();
