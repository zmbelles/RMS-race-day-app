// RMS Race Day — modal forms, confirm-deletes, gear ratio helpers.
(() => {
  const fmtLap = (s) => {
    if (s === null || s === undefined || s === "") return "";
    s = Number(s);
    const m = Math.floor(s / 60);
    const rest = (s - m * 60).toFixed(3);
    return m ? `${m}:${rest.padStart(6, "0")}` : rest;
  };

  // Show only options whose data-key matches the parent's selected data-key
  // (e.g. tire sets belonging to the chosen setup's kart).
  const applyFilters = (form) => {
    form.querySelectorAll("select[data-filter-by]").forEach((child) => {
      const parent = form.querySelector(`[name="${child.dataset.filterBy}"]`);
      const key = parent?.selectedOptions[0]?.dataset.key;
      let keep = false;
      [...child.options].forEach((o) => {
        const show = !o.value || !key || o.dataset.key === key;
        o.hidden = !show;
        if (show && o.selected) keep = true;
      });
      if (!keep) child.value = "";
    });
  };

  const liveRatio = (form) => {
    const out = form.querySelector("[data-live-ratio]");
    if (!out) return;
    const f = +form.elements.front_sprocket.value, r = +form.elements.rear_sprocket.value;
    out.textContent = f && r ? (r / f).toFixed(3) : "—";
  };

  const openForm = (key, record, defaults) => {
    const modalEl = document.getElementById(`modal-${key}`);
    if (!modalEl) return;
    const form = modalEl.querySelector("form");
    const values = record || defaults || {};
    form.reset();
    form.elements.id.value = record?.id ?? "";
    form.querySelector("[data-mode]").textContent = record ? "Edit" : "Add";
    form.querySelectorAll("[name][data-default]").forEach((el) => {
      const has = Object.prototype.hasOwnProperty.call(values, el.name);
      let v = has ? values[el.name] : el.dataset.default;
      if (el.type === "checkbox") el.checked = !!Number(v);
      else el.value = el.hasAttribute("data-lap") && has ? fmtLap(v) : (v ?? "");
    });
    if (!record) {
      // A prefilled driver/event brings its setup, kart, track… unless those were given too.
      form.querySelectorAll("select").forEach((sel) => {
        const fill = sel.value && sel.selectedOptions[0]?.dataset.fill;
        if (!fill) return;
        Object.entries(JSON.parse(fill)).forEach(([name, value]) => {
          const el = form.elements[name];
          if (el && value != null && !(name in values)) el.value = value;
        });
      });
    }
    applyFilters(form);
    // Selecting the filter first can blank a valid child value — restore it.
    form.querySelectorAll("select[data-filter-by]").forEach((el) => {
      if (values[el.name] != null) el.value = values[el.name];
    });
    liveRatio(form);
    bootstrap.Modal.getOrCreateInstance(modalEl).show();
  };

  document.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-form]");
    if (!btn) return;
    e.preventDefault();
    const parse = (s) => (s ? JSON.parse(s) : null);
    bootstrap.Offcanvas.getInstance(document.getElementById("sidebar"))?.hide();
    openForm(btn.dataset.form, parse(btn.dataset.record), parse(btn.dataset.defaults));
  });

  document.addEventListener("change", (e) => {
    const form = e.target.form;
    if (!form?.dataset.formKey) return;
    // Picking a driver/event pre-fills related fields (their setup, kart, track…)
    const fill = e.target.selectedOptions?.[0]?.dataset.fill;
    if (fill) {
      Object.entries(JSON.parse(fill)).forEach(([name, value]) => {
        const el = form.elements[name];
        if (el && value != null) el.value = value;
      });
    }
    applyFilters(form);
  });
  document.addEventListener("input", (e) => {
    if (e.target.form?.dataset.formKey === "setup") liveRatio(e.target.form);
  });

  document.addEventListener("submit", (e) => {
    const msg = e.target.dataset.confirm;
    if (msg && !confirm(msg)) e.preventDefault();
  });

  // Clickable table rows
  document.addEventListener("click", (e) => {
    const row = e.target.closest("tr[data-href]");
    if (row && !e.target.closest("a,button,form,input,select,[data-cmd]")) location.href = row.dataset.href;
  });

  // Gear calculator widget
  const calc = document.querySelector("[data-gear-calc]");
  if (calc) {
    const render = () => {
      const f = +calc.querySelector("[name=calc_front]").value;
      const r = +calc.querySelector("[name=calc_rear]").value;
      const out = calc.querySelector("[data-gear-out]");
      if (!f || !r) { out.innerHTML = ""; return; }
      out.innerHTML = [-2, -1, 0, 1, 2].map((d) => {
        const rr = r + d;
        return `<div class="gear-chip ${d === 0 ? "current" : ""}"><span>${f}/${rr}</span><strong>${(rr / f).toFixed(3)}</strong></div>`;
      }).join("");
    };
    calc.addEventListener("input", render);
    render();
  }
})();

// Race-day command bar: live preview while typing, Enter to apply, "/" to focus.
(() => {
  const form = document.querySelector("[data-cmd-form]");
  if (!form) return;
  const input = form.querySelector("input[name=q]");
  const panel = form.querySelector("[data-cmd-preview]");
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const verbs = { create: ["New setup", "plus-circle", "green"], switch: ["Existing setup", "arrow-left-right", "dark"],
                  noop: ["No change", "dash-circle", ""], undo: ["Undo", "arrow-counterclockwise", "dark"], error: ["Error", "x-circle", "red"] };
  let timer, seq = 0;

  const render = (items) => {
    if (!items.length) { panel.hidden = true; return; }
    panel.innerHTML = items.map((o) => {
      const [label, icon, tone] = verbs[o.kind] || verbs.noop;
      const who = o.driver ? `<strong>#${esc(o.driver.number)} ${esc(o.driver.name)}</strong>` : "";
      const changes = o.changes.map(([l, a, b]) => `<span class="cmd-change">${esc(l)} ${esc(a)} → <strong>${esc(b)}</strong></span>`).join("");
      const target = o.ok && o.kind !== "noop" && o.setup_name ? `<div class="small muted">→ ${esc(o.setup_name)}</div>` : "";
      const msg = !o.ok || (o.kind === "noop") || o.kind === "undo" ? `<div class="small ${o.ok ? "muted" : "text-danger"}">${esc(o.message)}</div>` : "";
      const warns = o.warnings.map((w) => `<div class="small cmd-warn"><i class="bi bi-exclamation-triangle"></i> ${esc(w)}</div>`).join("");
      return `<div class="cmd-item"><span class="chip ${tone}"><i class="bi bi-${icon}"></i> ${label}</span>
        <div class="min-w-0"><div>${who} ${changes}</div>${target}${msg}${warns}</div></div>`;
    }).join("") + `<div class="cmd-foot">Enter to apply · Esc to clear</div>`;
    panel.hidden = false;
  };

  const preview = () => {
    const q = input.value.trim();
    if (!q) { render([]); return; }
    const mine = ++seq;
    fetch(`/cmd/preview?q=${encodeURIComponent(q)}`).then((r) => r.json()).then((items) => { if (mine === seq) render(items); });
  };

  input.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(preview, 150); });
  input.addEventListener("focus", preview);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { input.value = ""; render([]); input.blur(); }
  });
  document.addEventListener("click", (e) => { if (!form.contains(e.target)) panel.hidden = true; });
  document.addEventListener("keydown", (e) => {
    if (e.key === "/" && !e.target.closest("input, textarea, select, [contenteditable]")) { e.preventDefault(); input.focus(); }
  });
  // Clicking a value on the lineup starts a command for that driver: "24 SPR "
  document.addEventListener("click", (e) => {
    const cell = e.target.closest("[data-cmd]");
    if (!cell) return;
    e.preventDefault();
    input.value = cell.dataset.cmd;
    input.focus();
    input.setSelectionRange(input.value.length, input.value.length);
    preview();
  });
})();

document.querySelectorAll('[data-bs-toggle="popover"]').forEach((el) => new bootstrap.Popover(el));
