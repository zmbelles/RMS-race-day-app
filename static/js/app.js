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
    if (form?.dataset.formKey) applyFilters(form);
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
    if (row && !e.target.closest("a,button,form,input,select")) location.href = row.dataset.href;
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
