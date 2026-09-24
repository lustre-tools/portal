document.addEventListener("DOMContentLoaded", function () {
  var accordionOpen = false;
  var editingChangeNum = null; // Original change # if user clicked Edit; null otherwise

  // The CSRF token the server minted for this session. Read from the
  // meta tag rather than a cookie, so it is never sent automatically.
  function csrfToken() {
    var el = document.querySelector('meta[name="csrf-token"]');
    return el ? el.getAttribute("content") : "";
  }

  // Registry of active/finished runs, keyed by run_id. Each run owns its own
  // socket so it keeps streaming even if the modal is closed.
  // shape: { runId: {socket, changeNum, status, buffer, graphUrl, oldChange} }
  var runs = {};
  var focusedRunId = null; // which run the modal is currently showing

  var accordionBtn = document.getElementById("accordion-btn");
  var accordionBody = document.getElementById("accordion-body");
  var modal = document.getElementById("console-modal");
  var modalTitle = document.getElementById("modal-title");
  var consoleOutput = document.getElementById("console-output");
  var statusBadge = document.getElementById("run-status");
  var resultArea = document.getElementById("result-area");
  var modalFooter = document.querySelector("#console-modal .modal-footer");

  function showResult(url) {
    resultLink.href = url;
    resultArea.style.display = "flex";
    if (modalFooter) modalFooter.style.display = "flex";
  }
  function hideResult() {
    resultArea.style.display = "none";
    if (modalFooter) modalFooter.style.display = "none";
  }
  var resultLink = document.getElementById("result-link");
  var generateBtn = document.getElementById("generate-btn");
  var modalCloseBtn = document.getElementById("modal-close-btn");

  function clearForm() {
    var form = document.getElementById("tool-form");
    if (!form) return;
    form.querySelectorAll('input[type="text"]').forEach(function (input) {
      input.value = "";
    });
    form.querySelectorAll('input[type="checkbox"]').forEach(function (cb) {
      cb.checked = false;
    });
    editingChangeNum = null; // No longer editing an existing entry
  }

  // Accordion toggle
  accordionBtn.addEventListener("click", function () {
    var willOpen = !accordionOpen;
    if (willOpen) {
      // Clicking "New Graph" should always start with a blank form
      clearForm();
    }
    accordionOpen = willOpen;
    accordionBody.style.display = accordionOpen ? "block" : "none";
    accordionBtn.textContent = accordionOpen ? "Close" : "New graph";
  });

  // === Labels autocomplete ===
  // Attaches a dropdown to the labels input. Typing filters ALL_LABELS by
  // prefix-match against the current comma-separated token.
  (function setupLabelsAutocomplete() {
    var input = document.getElementById("labels");
    if (!input || typeof ALL_LABELS === "undefined") return;

    // Wrapper so the dropdown can be positioned absolutely under the input
    var wrapper = document.createElement("div");
    wrapper.className = "labels-autocomplete-wrapper";
    input.parentNode.insertBefore(wrapper, input);
    wrapper.appendChild(input);

    var dropdown = document.createElement("div");
    dropdown.className = "labels-autocomplete";
    dropdown.style.display = "none";
    wrapper.appendChild(dropdown);

    var activeIdx = -1;
    var lastSuggestions = [];

    function getCurrentToken() {
      var caret = input.selectionStart;
      var before = input.value.substring(0, caret);
      var lastComma = before.lastIndexOf(",");
      return {
        token: before.substring(lastComma + 1).trimStart(),
        start: lastComma + 1,
        caret: caret,
      };
    }

    function getUsedLabels() {
      return input.value.split(",").map(function (s) { return s.trim().toLowerCase(); }).filter(Boolean);
    }

    function refresh() {
      var info = getCurrentToken();
      var prefix = info.token.toLowerCase();
      var used = getUsedLabels();
      var suggestions = ALL_LABELS.filter(function (l) {
        return used.indexOf(l) === -1 && (prefix === "" || l.indexOf(prefix) === 0);
      }).slice(0, 8);

      lastSuggestions = suggestions;
      activeIdx = -1;

      if (suggestions.length === 0) {
        dropdown.style.display = "none";
        return;
      }

      dropdown.innerHTML = "";
      suggestions.forEach(function (s, i) {
        var item = document.createElement("div");
        item.className = "labels-autocomplete-item";
        item.textContent = s;
        item.addEventListener("mousedown", function (e) {
          e.preventDefault();
          applySuggestion(s);
        });
        dropdown.appendChild(item);
      });
      dropdown.style.display = "block";
    }

    function applySuggestion(label) {
      var info = getCurrentToken();
      var before = input.value.substring(0, info.start);
      // Skip leading whitespace immediately after the comma in 'before'
      var after = input.value.substring(info.caret);
      // Trim trailing space from before, prefix one space if there's a comma
      var prefix = before;
      if (prefix && !prefix.endsWith(" ")) prefix += (prefix.endsWith(",") ? " " : "");
      // Add the label, then ", " ready for the next one
      input.value = prefix + label + ", " + after.trimStart();
      var newCaret = (prefix + label + ", ").length;
      input.setSelectionRange(newCaret, newCaret);
      dropdown.style.display = "none";
      input.focus();
    }

    function highlight(idx) {
      var items = dropdown.querySelectorAll(".labels-autocomplete-item");
      items.forEach(function (it, i) {
        it.classList.toggle("active", i === idx);
      });
    }

    input.addEventListener("input", refresh);
    input.addEventListener("focus", refresh);
    input.addEventListener("blur", function () {
      // Delay so click on suggestion can register first
      setTimeout(function () { dropdown.style.display = "none"; }, 150);
    });
    input.addEventListener("keydown", function (e) {
      if (dropdown.style.display === "none" || lastSuggestions.length === 0) return;
      if (e.key === "ArrowDown") {
        e.preventDefault();
        activeIdx = (activeIdx + 1) % lastSuggestions.length;
        highlight(activeIdx);
      } else if (e.key === "ArrowUp") {
        e.preventDefault();
        activeIdx = (activeIdx - 1 + lastSuggestions.length) % lastSuggestions.length;
        highlight(activeIdx);
      } else if (e.key === "Enter" && activeIdx >= 0) {
        e.preventDefault();
        applySuggestion(lastSuggestions[activeIdx]);
      } else if (e.key === "Escape") {
        dropdown.style.display = "none";
      }
    });
  })();

  // Close (minimize) the modal. The run keeps going in the background;
  // we just stop showing its console. A toast will fire on completion.
  window.closeModal = function () {
    modal.style.display = "none";
    focusedRunId = null;
    updateRunIndicator();
  };

  // Resolve the graph id from user input. Mirrors the server's
  // _parse_identifier: an uppercased JIRA ticket stays a ticket id
  // (so it keys the same index entry), otherwise the trailing digits
  // of a number/URL, otherwise the raw input.
  function extractChangeNum(input) {
    var s = (input || "").trim();
    var up = s.toUpperCase();
    if (/^[A-Z][A-Z0-9]*-\d+$/.test(up)) return up;
    var m = s.match(/(\d+)\s*$/);
    return m ? m[1] : s;
  }

  // Check if a change number already has a graph and confirm overwrite
  function confirmIfExists(changeNum) {
    var existing = RERUN_DATA[changeNum];
    if (existing) {
      var name = existing.name || "unnamed";
      return confirm(
        'A graph for change ' + changeNum + ' already exists:\n"' +
        name + '"\n\nOverwrite it?'
      );
    }
    return true;
  }

  // Check if a run is already in progress for this change (any active run)
  function checkNotRunning(changeNum) {
    for (var id in runs) {
      var r = runs[id];
      if (r.changeNum === changeNum && (r.status === "running" || r.status === "queued")) {
        alert("Graph for change " + changeNum + " is already being generated.");
        return false;
      }
    }
    return true;
  }

  function activeRunCount() {
    var n = 0;
    for (var id in runs) {
      if (runs[id].status === "running" || runs[id].status === "queued") n++;
    }
    return n;
  }

  // Form submit -> start WebSocket run
  // Params that affect the actual graph contents
  var REGEN_KEYS = [
    "comments", "skip_topic", "skip_hashtag", "skip_ci_details",
    "include_topic", "include_hashtag", "branch", "ticket",
  ];

  function paramsAffectGeneration(stored, current) {
    for (var i = 0; i < REGEN_KEYS.length; i++) {
      var k = REGEN_KEYS[i];
      var sv = stored[k];
      var cv = current[k];
      if (typeof sv === "string" || typeof cv === "string") {
        if ((sv || "").trim() !== (cv || "").trim()) return true;
      } else {
        if (!!sv !== !!cv) return true;
      }
    }
    return false;
  }

  window.startRun = function (e) {
    e.preventDefault();
    var form = document.getElementById("tool-form");
    var formData = new FormData(form);

    var toolId = formData.get("tool_id");
    var params = {};
    for (var pair of formData.entries()) {
      if (pair[0] === "tool_id") continue;
      params[pair[0]] = pair[1];
    }
    // Checkboxes: FormData only includes checked ones
    form.querySelectorAll('input[type="checkbox"]').forEach(function (cb) {
      if (!formData.has(cb.name)) {
        params[cb.name] = false;
      } else {
        params[cb.name] = true;
      }
    });

    var changeNum = extractChangeNum(params.change_number || "");
    if (!changeNum) return;

    if (!checkNotRunning(changeNum)) return;

    // If we're editing and the user changed the change number, the original
    // entry needs to be deleted after regeneration. We also can't take the
    // metadata fast path because we'd be writing to the wrong entry.
    var changedDuringEdit = editingChangeNum && editingChangeNum !== changeNum;

    // Metadata-only fast path: only when editing the same change number
    // (or creating fresh) and only labels/name changed.
    if (!changedDuringEdit) {
      var existing = RERUN_DATA[changeNum];
      if (existing && !paramsAffectGeneration(existing.params || {}, params)) {
        submitMetadataOnly(changeNum, params);
        return;
      }
    }

    if (!confirmIfExists(changeNum)) return;

    doRun(toolId, params, changeNum, changedDuringEdit ? editingChangeNum : null);
  };

  function submitMetadataOnly(changeNum, params) {
    var fd = new FormData();
    fd.append("name", params.name || "");
    fd.append("labels", params.labels || "");
    generateBtn.disabled = true;
    generateBtn.textContent = "Saving...";
    editingChangeNum = null;
    fetch("/gerrit_vis/graphs/metadata/" + changeNum, {
      method: "POST",
      body: fd,
      credentials: "same-origin",
      redirect: "manual",
      headers: { "X-CSRF-Token": csrfToken() },
    }).then(function () {
      window.location.reload();
    }).catch(function (err) {
      generateBtn.disabled = false;
      generateBtn.textContent = "Generate";
      alert("Failed to save metadata: " + err);
    });
  }

  // Rerun from table
  window.rerunGraph = function (changeNumber) {
    if (!checkNotRunning(changeNumber)) return;

    var entry = RERUN_DATA[changeNumber];
    var params;
    if (entry && entry.params) {
      params = Object.assign({}, entry.params);
    } else {
      params = { change_number: changeNumber };
    }
    params.change_number = changeNumber;

    doRun("gc-graph", params, changeNumber);
  };

  // Edit: open accordion with form pre-filled; user clicks Generate to apply
  window.editGraph = function (changeNumber) {
    var entry = RERUN_DATA[changeNumber];
    var params = (entry && entry.params) ? Object.assign({}, entry.params) : {};
    params.change_number = changeNumber;
    // Convert stored labels list to comma-separated for the input field
    if (entry && entry.labels && entry.labels.length) {
      params.labels = entry.labels.join(", ");
    }

    var form = document.getElementById("tool-form");
    if (!form) return;

    // Remember which change we're editing so we can detect change-number changes
    editingChangeNum = changeNumber;

    // Pre-fill form fields
    form.querySelectorAll('input[type="text"]').forEach(function (input) {
      if (input.name in params) {
        input.value = params[input.name];
      } else {
        input.value = "";
      }
    });
    form.querySelectorAll('input[type="checkbox"]').forEach(function (cb) {
      cb.checked = !!params[cb.name];
    });

    // Open accordion
    if (!accordionOpen) {
      accordionOpen = true;
      accordionBody.style.display = "block";
      accordionBtn.textContent = "Close";
    }

    // Scroll into view
    document.getElementById("new-graph-section").scrollIntoView({ behavior: "smooth", block: "start" });
  };

  function setRerunButtonsState(changeNum, disabled) {
    document.querySelectorAll('.btn-rerun[data-change="' + changeNum + '"]').forEach(function (btn) {
      btn.disabled = disabled;
      btn.textContent = disabled ? "Running..." : "Rerun";
    });
  }

  // Render the modal for the currently focused run
  function renderModal(runId) {
    var r = runs[runId];
    if (!r) return;
    focusedRunId = runId;
    modal.style.display = "flex";
    consoleOutput.textContent = r.buffer;
    consoleOutput.scrollTop = consoleOutput.scrollHeight;
    hideResult();
    modalCloseBtn.style.display = "inline-block"; // always allow background/close

    if (r.status === "running" || r.status === "queued") {
      modalTitle.textContent = "Generating graph for change " + r.changeNum + "...";
      statusBadge.textContent = "Running...";
      statusBadge.className = "status-badge status-running";
      modalCloseBtn.textContent = "Run in background";
    } else if (r.status === "done") {
      modalTitle.textContent = "Graph generated";
      statusBadge.textContent = "Done";
      statusBadge.className = "status-badge status-done";
      modalCloseBtn.textContent = "Close";
      if (r.graphUrl) {
        showResult(r.graphUrl);
      }
    } else if (r.status === "failed") {
      modalTitle.textContent = "Generation failed";
      statusBadge.textContent = "Failed";
      statusBadge.className = "status-badge status-error";
      modalCloseBtn.textContent = "Close";
    }
  }

  function doRun(toolId, params, changeNum, oldChangeToDelete) {
    var runId = crypto.randomUUID();

    var r = {
      socket: null,
      changeNum: changeNum,
      status: "running",
      buffer: "",
      graphUrl: null,
      oldChange: oldChangeToDelete || null,
    };
    runs[runId] = r;

    setRerunButtonsState(changeNum, true);
    generateBtn.disabled = false;       // form is free again immediately
    generateBtn.textContent = "Generate";
    renderModal(runId);
    updateRunIndicator();

    var socket = io();
    r.socket = socket;

    socket.on("connect", function () {
      var msg = { run_id: runId, tool_id: toolId, params: params };
      if (oldChangeToDelete) msg.original_change_number = oldChangeToDelete;
      socket.emit("start_tool", msg);
    });

    socket.on("output", function (data) {
      r.buffer += data.line;
      if (focusedRunId === runId) {
        consoleOutput.textContent = r.buffer;
        consoleOutput.scrollTop = consoleOutput.scrollHeight;
      }
    });

    socket.on("complete", function (data) {
      setRerunButtonsState(changeNum, false);
      socket.disconnect();
      r.socket = null;

      if (data.ok) {
        r.status = "done";
        r.graphUrl = data.graph_url || null;
        if (focusedRunId === runId) {
          modalTitle.textContent = "Graph generated";
          statusBadge.textContent = "Done";
          statusBadge.className = "status-badge status-done";
          modalCloseBtn.textContent = "Close";
          if (r.graphUrl) {
            showResult(r.graphUrl);
          }
        }
      } else {
        r.status = "failed";
        r.buffer += "\n--- Failed: " + (data.error || "unknown error") + " ---\n";
        if (focusedRunId === runId) {
          consoleOutput.textContent = r.buffer;
          modalTitle.textContent = "Generation failed";
          statusBadge.textContent = "Failed";
          statusBadge.className = "status-badge status-error";
          modalCloseBtn.textContent = "Close";
        }
        showToast(false, changeNum, null, data.error);
      }

      editingChangeNum = null;
      updateRunIndicator();

      if (data.ok) {
        // The entry's key as the server stored it (a ticket is uppercased).
        var shown = data.change_number || changeNum;
        var inPlace = window.refreshGraphRow
          ? window.refreshGraphRow(shown, r.oldChange)
          : Promise.resolve(false);
        inPlace.then(function (updated) {
          showToast(true, shown, r.graphUrl, null, updated);
          // Could not update the row in place: reload instead, but only if
          // the user is watching this run and nothing else is still running
          // (so we don't yank the page out from under other background runs).
          if (!updated && focusedRunId === runId &&
              modal.style.display !== "none" && activeRunCount() === 0) {
            setTimeout(function () { window.location.reload(); }, 1500);
          }
        });
      }
    });

    socket.on("disconnect", function () {
      if (r.status === "running" || r.status === "queued") {
        r.status = "failed";
        r.buffer += "\n--- Disconnected ---\n";
        if (focusedRunId === runId) {
          consoleOutput.textContent = r.buffer;
          modalTitle.textContent = "Disconnected";
          statusBadge.textContent = "Disconnected";
          statusBadge.className = "status-badge status-error";
        }
        setRerunButtonsState(changeNum, false);
        updateRunIndicator();
      }
    });
  }

  // === Toasts ===
  function getToastContainer() {
    var c = document.getElementById("toast-container");
    if (!c) {
      c = document.createElement("div");
      c.id = "toast-container";
      c.className = "toast-container";
      document.body.appendChild(c);
    }
    return c;
  }

  // inPlace: the list row was already updated, so no "Refresh list" link.
  function showToast(ok, changeNum, graphUrl, error, inPlace) {
    var c = getToastContainer();
    var toast = document.createElement("div");
    toast.className = "toast " + (ok ? "toast-ok" : "toast-error");

    var msg = document.createElement("span");
    msg.textContent = ok
      ? "Graph for change " + changeNum + " ready"
      : "Change " + changeNum + " failed" + (error ? ": " + error : "");
    toast.appendChild(msg);

    if (ok && graphUrl) {
      var link = document.createElement("a");
      link.href = graphUrl;
      link.target = "_blank";
      link.textContent = "View";
      link.className = "toast-link";
      toast.appendChild(link);
    }
    if (ok && graphUrl && !inPlace) {
      var refresh = document.createElement("a");
      refresh.href = "#";
      refresh.textContent = "Refresh list";
      refresh.className = "toast-link";
      refresh.addEventListener("click", function (e) {
        e.preventDefault();
        window.location.reload();
      });
      toast.appendChild(refresh);
    }

    var close = document.createElement("span");
    close.className = "toast-close";
    close.textContent = "×";
    close.addEventListener("click", function () { c.removeChild(toast); });
    toast.appendChild(close);

    c.appendChild(toast);
    // Auto-dismiss after 12s
    setTimeout(function () {
      if (toast.parentNode === c) c.removeChild(toast);
    }, 12000);
  }

  // === Background run indicator ===
  function getRunIndicator() {
    var el = document.getElementById("run-indicator");
    if (!el) {
      el = document.createElement("div");
      el.id = "run-indicator";
      el.className = "run-indicator";
      el.style.display = "none";
      el.addEventListener("click", function () {
        // Reopen the most recent active run's console
        var ids = Object.keys(runs);
        for (var i = ids.length - 1; i >= 0; i--) {
          var r = runs[ids[i]];
          if (r.status === "running" || r.status === "queued") {
            renderModal(ids[i]);
            return;
          }
        }
      });
      document.body.appendChild(el);
    }
    return el;
  }

  function updateRunIndicator() {
    var el = getRunIndicator();
    var n = activeRunCount();
    // Hide if nothing is running OR the modal is already showing a run
    if (n === 0 || (focusedRunId && modal.style.display !== "none")) {
      el.style.display = "none";
    } else {
      el.textContent = "▶ " + n + " running…";
      el.style.display = "block";
    }
  }
});
