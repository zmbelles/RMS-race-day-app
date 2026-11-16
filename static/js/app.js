// RMS Race Day — modal forms, confirm-deletes and row links.
(() => {
  const fmtLap = (s) => {
    if (s === null || s === undefined || s === "") return "";
    s = Number(s);
    const m = Math.floor(s / 60);
    const rest = (s - m * 60).toFixed(3);
    return m ? `${m}:${rest.padStart(6, "0")}` : rest;
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

document.querySelectorAll(".modal[data-autoshow]").forEach((el) => bootstrap.Modal.getOrCreateInstance(el).show());

// Race day: drag a setup from the tray onto a driver's card to put them on it.
(() => {
  const form = document.getElementById("drop-setup-form");
  if (!form) return;
  document.querySelectorAll("[data-drag-setup]").forEach((el) => {
    el.addEventListener("dragstart", (e) => {
      e.dataTransfer.setData("text/plain", el.dataset.dragSetup);
      e.dataTransfer.effectAllowed = "copy";
      document.body.classList.add("dragging-setup");
    });
    el.addEventListener("dragend", () => document.body.classList.remove("dragging-setup"));
  });
  document.querySelectorAll("[data-drop-entry]").forEach((card) => {
    card.addEventListener("dragover", (e) => { e.preventDefault(); card.classList.add("drop-target"); });
    card.addEventListener("dragleave", (e) => { if (!card.contains(e.relatedTarget)) card.classList.remove("drop-target"); });
    card.addEventListener("drop", (e) => {
      e.preventDefault();
      card.classList.remove("drop-target");
      const setup = e.dataTransfer.getData("text/plain");
      if (!setup) return;
      form.elements.entry_id.value = card.dataset.dropEntry;
      form.elements.setup_id.value = setup;
      form.submit();
    });
  });
})();

// Weekend format editor: add, remove and reorder sessions, or start from a preset.
(() => {
  const PRESETS = {
    club: [["first", "Practice 1", "Practice"], ["first", "Practice 2", "Practice"], ["first", "Practice 3", "Practice"],
           ["last", "Heat 1", "Heat"], ["last", "Heat 2", "Heat"]],
    weekend: [["first", "Practice 1", "Practice"], ["first", "Practice 2", "Practice"], ["first", "Practice 3", "Practice"],
              ["first", "Practice 4", "Practice"], ["last", "Qualifying", "Qualifying"], ["last", "Heat 1", "Heat"],
              ["last", "Heat 2", "Heat"], ["last", "Final", "Feature"]],
    races: [["first", "Practice 1", "Practice"], ["first", "Practice 2", "Practice"], ["first", "Practice 3", "Practice"],
            ["last", "Qualifying", "Qualifying"], ["last", "Race 1", "Race"], ["last", "Race 2", "Race"]],
    clear: [],
  };
  const kindOf = (name) => {
    const w = (name.trim().split(/\s+/)[0] || "").toLowerCase();
    if (/^(final|main|feature)/.test(w)) return "Feature";
    for (const k of ["Practice", "Qualifying", "Heat", "Race", "Prefinal"]) if (w.startsWith(k.toLowerCase().slice(0, 4))) return k;
    return null;
  };
  const addRow = (form, day, name = "", kind = null) => {
    const body = form.querySelector("[data-format-rows]");
    const row = form.querySelector("template[data-format-row]").content.firstElementChild.cloneNode(true);
    const last = body.lastElementChild;
    row.querySelector("[name=day]").value = day || (last ? last.querySelector("[name=day]").value : row.querySelector("[name=day]").value);
    row.querySelector("[name=name]").value = name;
    if (kind) row.querySelector("[name=kind]").value = kind;
    body.append(row);
    return row;
  };
  document.addEventListener("click", (e) => {
    const form = e.target.closest("[data-format-editor]");
    if (!form) return;
    const days = [...form.querySelector("template[data-format-row]").content.querySelectorAll("[name=day] option")].map((o) => o.value);
    if (e.target.closest("[data-format-add]")) {
      addRow(form).querySelector("[name=name]").focus();
    } else if (e.target.closest("[data-format-remove]")) {
      e.target.closest("tr").remove();
    } else if (e.target.closest("[data-format-move]")) {
      const row = e.target.closest("tr");
      const step = +e.target.closest("[data-format-move]").dataset.formatMove;
      const other = step < 0 ? row.previousElementSibling : row.nextElementSibling;
      if (other) step < 0 ? other.before(row) : other.after(row);
    } else if (e.target.closest("[data-format-preset]")) {
      form.querySelector("[data-format-rows]").replaceChildren();
      for (const [when, name, kind] of PRESETS[e.target.closest("[data-format-preset]").dataset.formatPreset]) {
        addRow(form, when === "first" ? days[0] : days[days.length - 1], name, kind);
      }
    }
  });
  // Changing the dates rebuilds every Day list; a session keeps its day if the race still covers it.
  const dayLabel = (iso) => new Date(iso + "T00:00:00Z").toLocaleDateString("en-US",
    { weekday: "short", month: "short", day: "numeric", timeZone: "UTC" }).replace(",", "");
  document.addEventListener("change", (e) => {
    const form = e.target.closest("[data-format-editor]");
    if (!form || !e.target.matches("[data-format-start], [data-format-end]")) return;
    const startInput = form.querySelector("[data-format-start]");
    const endInput = form.querySelector("[data-format-end]");
    if (!startInput.value) return;
    if (!endInput.value || endInput.value < startInput.value) endInput.value = startInput.value;
    const days = [];
    for (let d = new Date(startInput.value + "T00:00:00Z"); days.length < 14; d.setUTCDate(d.getUTCDate() + 1)) {
      const iso = d.toISOString().slice(0, 10);
      if (iso > endInput.value) break;
      days.push(iso);
    }
    const selects = [...form.querySelectorAll("[name=day]"),
                     ...form.querySelector("template[data-format-row]").content.querySelectorAll("[name=day]")];
    for (const select of selects) {
      const was = select.value;
      select.replaceChildren(...days.map((iso) => new Option(dayLabel(iso), iso)));
      select.value = days.includes(was) ? was : (was > days[days.length - 1] ? days[days.length - 1] : days[0]);
    }
  });
  // Typing a name picks its type: "Heat 2" -> Heat, "Final" -> Feature.
  document.addEventListener("input", (e) => {
    if (e.target.name !== "name" || !e.target.closest("[data-format-editor]")) return;
    const kind = kindOf(e.target.value);
    if (kind) e.target.closest("tr").querySelector("[name=kind]").value = kind;
  });
})();

// Log run on race day: one session, stepping through each driver on the entry list.
(() => {
  const form = document.querySelector("[data-stepper]");
  if (!form) return;
  const steps = [...form.querySelectorAll("[data-step]")];
  const dots = [...form.querySelectorAll("[data-step-go]")];
  const prev = form.querySelector("[data-step-prev]");
  const next = form.querySelector("[data-step-next]");
  const save = form.querySelector("[data-step-save]");
  const session = form.querySelector("[data-step-session]");
  const days = JSON.parse(form.dataset.sessionDays || "{}");
  let at = 0;
  const filled = (step) => [...step.querySelectorAll("input:not([type=hidden])")].some((i) => i.value.trim());
  const show = (i, focus = true) => {
    at = Math.max(0, Math.min(steps.length - 1, i));
    steps.forEach((s, n) => { s.hidden = n !== at; });
    dots.forEach((d, n) => { d.classList.toggle("active", n === at); d.classList.toggle("filled", filled(steps[n])); });
    form.querySelector("[data-step-num]").textContent = at + 1;
    prev.disabled = at === 0;
    next.hidden = at === steps.length - 1;
    save.classList.toggle("btn-outline-rms", at !== steps.length - 1);
    save.classList.toggle("btn-rms", at === steps.length - 1);
    if (focus) steps[at].querySelector("[data-lap]").focus();
  };
  // A driver who already has a lap in this session gets a heads-up (saving adds another run).
  const hints = () => {
    const name = session.value.trim();
    for (const step of steps) {
      const lap = JSON.parse(step.dataset.logged || "{}")[name];
      const hint = step.querySelector("[data-logged-hint]");
      hint.hidden = !lap;
      if (lap) hint.textContent = `Already logged in ${name}: ${lap}. Saving adds another run.`;
    }
  };
  session.addEventListener("input", () => {
    if (days[session.value.trim()]) form.querySelector("[data-step-date]").value = days[session.value.trim()];
    hints();
  });
  prev.addEventListener("click", () => show(at - 1));
  next.addEventListener("click", () => show(at + 1));
  dots.forEach((d) => d.addEventListener("click", () => show(+d.dataset.stepGo)));
  form.addEventListener("keydown", (e) => {
    if (e.key !== "Enter" || !e.target.closest("[data-step]")) return;
    e.preventDefault();
    if (at < steps.length - 1) show(at + 1); else form.requestSubmit();
  });
  form.addEventListener("input", () => dots[at].classList.toggle("filled", filled(steps[at])));
  form.closest(".modal").addEventListener("shown.bs.modal", () => show(at));
  // Clicking a session on the weekend timeline opens Log run for that session, from the first driver.
  document.addEventListener("click", (e) => {
    const chip = e.target.closest("[data-log-session]");
    if (!chip) return;
    session.value = chip.dataset.logSession;
    session.dispatchEvent(new Event("input"));
    show(0, false);
  });
  hints();
})();

// Copy buttons: copy the target element's text.
document.addEventListener("click", async (e) => {
  const btn = e.target.closest("[data-copy-target]");
  if (!btn) return;
  const label = btn.querySelector("span");
  try {
    await navigator.clipboard.writeText(document.querySelector(btn.dataset.copyTarget).textContent);
    if (label) label.textContent = "Copied";
  } catch {
    if (label) label.textContent = "Select and copy";
  }
  setTimeout(() => { if (label) label.textContent = "Copy"; }, 2000);
});

// Results grid: picking PEN opens a popup for positions lost and the reason; the cell then shows "PEN +2 · reason",
// which reopens it. The popup lives inside the results modal so the modal keeps focus.
(() => {
  let popup, cell;
  const label = (td) => {
    const pos = td.querySelector("[data-pen-pos]").value, why = td.querySelector("[data-pen-why]").value;
    const btn = td.querySelector("[data-pen-edit]");
    btn.textContent = `PEN${pos ? " +" + pos : ""}${why ? " · " + why : ""}`;
    btn.title = "Edit penalty";
  };
  document.querySelectorAll("[data-pen-edit]").forEach((btn) => label(btn.closest("td")));
  const build = (host) => {
    popup = document.createElement("div");
    popup.className = "pen-popup";
    popup.hidden = true;
    popup.innerHTML = `<div class="fw-bold mb-1">Penalty</div><div class="small muted mb-2" data-pen-who></div>
      <label class="form-label small mb-1">Positions lost</label>
      <input class="form-control form-control-sm mb-2" type="number" min="0" inputmode="numeric" data-pen-in-pos>
      <label class="form-label small mb-1">Reason</label>
      <input class="form-control form-control-sm mb-2" placeholder="Jumped start, contact, track limits…" data-pen-in-why>
      <div class="d-flex justify-content-end gap-1">
        <button type="button" class="btn btn-sm btn-light" data-pen-cancel>Cancel</button>
        <button type="button" class="btn btn-sm btn-rms" data-pen-ok>Done</button></div>`;
    host.append(popup);
    popup.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); popup.querySelector("[data-pen-ok]").click(); }
      if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); close(); }
    });
    popup.querySelector("[data-pen-cancel]").addEventListener("click", close);
    popup.querySelector("[data-pen-ok]").addEventListener("click", () => {
      cell.querySelector("[data-pen-pos]").value = popup.querySelector("[data-pen-in-pos]").value.trim();
      cell.querySelector("[data-pen-why]").value = popup.querySelector("[data-pen-in-why]").value.trim();
      label(cell);
      close();
    });
  };
  const open = (td) => {
    if (!popup) build(td.closest(".modal-content"));
    cell = td;
    const btn = td.querySelector("[data-pen-edit]");
    popup.querySelector("[data-pen-who]").textContent = btn.dataset.penTitle;
    popup.querySelector("[data-pen-in-pos]").value = td.querySelector("[data-pen-pos]").value;
    popup.querySelector("[data-pen-in-why]").value = td.querySelector("[data-pen-why]").value;
    popup.hidden = false;
    const r = td.getBoundingClientRect(), w = popup.offsetWidth, h = popup.offsetHeight;
    popup.style.left = `${Math.max(8, Math.min(r.left, innerWidth - w - 8))}px`;
    popup.style.top = `${r.bottom + 4 + h > innerHeight ? Math.max(8, r.top - h - 4) : r.bottom + 4}px`;
    popup.querySelector("[data-pen-in-pos]").focus();
  };
  const close = () => { if (popup) popup.hidden = true; };
  document.addEventListener("change", (e) => {
    if (!e.target.matches("[data-classification]")) return;
    const td = e.target.closest("td");
    td.querySelector("[data-pen-edit]").hidden = e.target.value !== "PEN";
    if (e.target.value === "PEN") { label(td); open(td); } else close();
  });
  document.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-pen-edit]");
    if (btn) { open(btn.closest("td")); return; }
    if (popup && !popup.hidden && !popup.contains(e.target) && !e.target.closest("[data-classification]")) close();
  });
  document.addEventListener("hide.bs.modal", close);
})();
