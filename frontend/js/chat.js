/**
 * Chat UI — WebSocket streaming with REST fallback, markdown rendering,
 * citations, feedback, follow-ups, autocomplete, token usage meter, and
 * an in-sidebar department switcher.
 */
(function () {
  if (!OA.requireAuth()) return;

  const $ = (id) => document.getElementById(id);

  // Live monthly budget pulled from the server (admin can set it
  // per-user; otherwise the system default applies). 0 == unlimited.
  // We refresh after every assistant reply.
  const budget = {
    used: 0,
    limit: 0,
    remaining: null,
    period_end: null,
    limit_source: "system_default",
    exceeded: false,
    sessionInput: 0,   // running tally for the current session only
    sessionOutput: 0,
  };

  // --- Local state -------------------------------------------------------
  const state = {
    sessionId: null,
    sessions: [],
    streaming: false,
    ws: null,
    currentAsstEl: null,
    currentAsstBuffer: "",
    user: OA.user(),
    departments: [],
    // Available metadata facets {key: [values]} for the user's depts,
    // and the user's current selection {key: value}. Empty selection =
    // search the whole department.
    facets: {},
    // Structured metadata filter. Sent to the backend on every query.
    //   operator: "AND" | "OR"   — how rules combine
    //   rules:    [{key, values: [...]}]
    // A rule with no values is dropped before sending. Multiple values
    // inside one rule mean "match ANY of these for this key".
    metaFilters: { operator: "AND", rules: [] },
    // Selected Bedrock model id (CrossAdmin/SuperAdmin only; others stay
    // on the server default). null until /chat/models resolves.
    modelId: null,
  };

  // ==========================================================================
  // INITIAL CHROME (icons, labels)
  // ==========================================================================
  const me = OA.user();
  const MULTI_DEPT_ROLES = new Set(["CROSSADMIN", "SUPERADMIN"]);
  const isMultiDept = MULTI_DEPT_ROLES.has(String(me.role || "").toUpperCase());
  // Pipeline diagnostics (hallucination % + LangGraph-style trace) are
  // shown only to SUPERADMIN today; the server also gates this so a
  // tampered client cannot exfiltrate the diagnostics payload.
  const isSuperadmin = String(me.role || "").toUpperCase() === "SUPERADMIN";

  // For multi-dept roles, summarise the active selection in the brand
  // chip instead of just the active dept's name.
  function formatDeptLabel() {
    const u = OA.user();
    const codes = Array.isArray(u.department_codes) ? u.department_codes : [];
    if (isMultiDept && codes.length > 1) {
      return `${codes.length} departments · ${u.department_name || u.department_code || ""}`;
    }
    return u.department_name || u.department_code || "—";
  }

  $("deptLabel").textContent = formatDeptLabel();
  $("userName").textContent = me.full_name || me.email || "User";
  $("userMail").textContent = me.email || "";
  $("avatar").textContent = (me.email || "U")[0].toUpperCase();
  $("helpDept").textContent = me.department_name || me.department_code || "support";

  // Admin shortcut: visible to everyone, gated server-side and again on
  // the /admin.html page itself.
  $("adminLink").classList.remove("hidden");

  // Static-chrome SVG icon injection.
  $("adminLink").innerHTML      = `${Icons.shield}<span style="margin-left:6px">Admin</span>`;
  $("sidebarToggle").innerHTML  = Icons.sidebar;
  $("exportBtn").innerHTML      = Icons.download;
  $("sendBtn").innerHTML        = Icons.send;
  $("logoutBtn").innerHTML      = `${Icons.logout}<span>Sign out</span>`;
  $("usageIcon").innerHTML      = Icons.zap;
  if ($("filtersIcon")) $("filtersIcon").innerHTML = Icons.filter;
  $("deptChevron").innerHTML    = Icons.chevronDown;
  $("themeIconSun").innerHTML   = Icons.sun;
  $("themeIconMoon").innerHTML  = Icons.moon;
  $("helpCtaIcon").innerHTML    = Icons.help;
  // "New" button glyph in the Recent section.
  document.querySelector(".new-chat-icon").innerHTML = Icons.plus;

  // ==========================================================================
  // DEPARTMENT DROPDOWN  (below the brand-name)
  // ==========================================================================
  const DEPT_ICON_KEY = {
    hr: "hr", finance: "finance", it: "it", legal: "legal",
    operations: "operations", security: "security", procurement: "procurement",
    marketing: "marketing", sales: "sales", engineering: "engineering",
    health: "health",
  };
  const deptIconSvg = (code) =>
    Icons.svg(DEPT_ICON_KEY[(code || "").toLowerCase()] || "building");

  async function loadDepartments() {
    try {
      // /auth/departments returns the full list the *user* can pick from
      // (gated by their role) AND already filters out inactive depts on
      // the server. We re-use that for the in-app switcher.
      const list = await OA.get("/auth/departments", { silent: true });
      state.departments = list;
      renderDeptMenu();
      loadFacets();
      // If nothing came back, the user's home dept has been deactivated.
      // Show a friendly fallback panel with admin contacts instead of
      // letting them stare at an unresponsive composer.
      if (!list.length) {
        await showDeptInactiveNotice();
      } else {
        clearDeptInactiveNotice();
      }
    } catch (_) {}
  }

  // ---- All-inactive-department fallback ---------------------------------
  async function showDeptInactiveNotice() {
    const u = OA.user();
    const code = u.department_code || (u.department_codes || [])[0];
    if (!code) return;
    let admins = [];
    try {
      admins = await OA.get(`/auth/dept-admins/${encodeURIComponent(code)}`, { silent: true }) || [];
    } catch (_) {}

    const adminBlock = admins.length
      ? `<ul class="dept-inactive-admins">${admins.map(a => `
          <li>
            <span class="dept-inactive-admin-name">${esc(a.name || a.email)}</span>
            <span class="muted"> · ${a.role}</span>
            <a class="dept-inactive-admin-mail" href="mailto:${esc(a.email)}">${esc(a.email)}</a>
          </li>`).join("")}</ul>`
      : `<p class="muted">No active administrator found. Please reach out to your IT helpdesk.</p>`;

    $("messages").innerHTML = `
      <div class="empty-state dept-inactive-notice">
        <h1>Your department isn't available</h1>
        <p class="muted">
          The <code>${esc(code)}</code> department has been deactivated.
          Contact one of these administrators to restore your access:
        </p>
        ${adminBlock}
      </div>`;
    // Disable the composer so users don't fire pointless chat calls.
    const input = $("input");
    const send = $("sendBtn");
    if (input) { input.disabled = true; input.placeholder = "Department inactive — chat disabled"; }
    if (send) send.disabled = true;
  }

  function clearDeptInactiveNotice() {
    const input = $("input");
    const send = $("sendBtn");
    if (input && input.disabled) {
      input.disabled = false;
      input.placeholder = "Ask a question…";
    }
    if (send && !state.streaming) send.disabled = false;
  }

  function esc(s) {
    return (s == null ? "" : String(s)).replace(/[&<>"']/g, c => (
      {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]
    ));
  }

  // ---- Metadata filters (chat header) ----------------------------------
  //
  // The popover is a small rule-builder:
  //   * Each row is a "rule" = one metadata key + N values (chip picker).
  //   * A top toggle picks AND (every rule must match) or OR (any rule).
  //   * Multiple values inside one rule are an implicit OR for that key.
  //
  // `state.metaFilters` is what's APPLIED (sent with queries).
  // `pendingFilters` is the staged copy edited inside the popover; it
  // doesn't take effect until Apply.
  function emptyFilter() { return { operator: "AND", rules: [] }; }
  function cloneFilter(f) {
    return {
      operator: (f && f.operator === "OR") ? "OR" : "AND",
      rules: (f && Array.isArray(f.rules) ? f.rules : []).map(r => ({
        key: r.key || "",
        values: Array.isArray(r.values) ? r.values.slice() : [],
      })),
    };
  }
  let pendingFilters = emptyFilter();

  async function loadFacets() {
    try {
      const r = await OA.get("/chat/facets", { silent: true });
      state.facets = (r && r.facets) || {};
    } catch (_) {
      state.facets = {};
    }
    // Drop any applied values/keys that no longer exist in the facet map.
    state.metaFilters = pruneFilter(state.metaFilters, state.facets);
    pendingFilters = cloneFilter(state.metaFilters);
    renderFilters();
  }

  function pruneFilter(filter, facets) {
    const out = cloneFilter(filter);
    out.rules = out.rules
      .map(r => {
        const allowed = facets[r.key] || [];
        return { key: r.key, values: r.values.filter(v => allowed.includes(v)) };
      })
      .filter(r => r.key && r.values.length);
    return out;
  }

  function filterCount(filter) {
    if (!filter || !Array.isArray(filter.rules)) return 0;
    return filter.rules.reduce((n, r) => n + (r.values && r.values.length ? 1 : 0), 0);
  }

  // ---- Render ---------------------------------------------------------
  function renderFilters() {
    const wrap = $("filtersWrap");
    const rules = $("filtersRules");
    if (!wrap || !rules) return;

    const facetKeys = Object.keys(state.facets);
    if (!facetKeys.length) {
      wrap.classList.add("hidden");
      rules.innerHTML = "";
      updateFilterCount();
      return;
    }
    wrap.classList.remove("hidden");

    // Hide the AND/OR control when there are fewer than 2 rules — the
    // combinator only matters when rules combine.
    renderCombinator();
    const combinator = document.querySelector(".filters-combinator");
    if (combinator) {
      combinator.classList.toggle("hidden", pendingFilters.rules.length < 2);
    }

    rules.innerHTML = "";
    if (!pendingFilters.rules.length) {
      // Empty-state placeholder — no auto-added rule. Adds a single
      // CTA that mirrors the bottom "Add filter rule" button.
      const empty = document.createElement("div");
      empty.className = "filter-empty-state";
      empty.innerHTML = `
        <div class="filter-empty-icon" aria-hidden="true">${Icons.filter || ""}</div>
        <div class="filter-empty-title">No filter rules yet</div>
        <div class="filter-empty-sub muted">Add a rule to narrow results to specific tags.</div>
      `;
      rules.appendChild(empty);
    } else {
      pendingFilters.rules.forEach((rule, idx) => {
        rules.appendChild(renderRule(rule, idx, facetKeys));
      });
    }

    updateFilterCount();
    updateApplyState();
  }

  function renderCombinator() {
    const op = pendingFilters.operator === "OR" ? "OR" : "AND";
    const toggle = $("filtersOpToggle");
    const hint = $("filtersOpHint");
    if (toggle) {
      toggle.querySelectorAll(".seg-btn").forEach(b => {
        const active = b.dataset.op === op;
        b.classList.toggle("active", active);
        b.setAttribute("aria-checked", String(active));
      });
    }
    if (hint) {
      hint.textContent = op === "OR" ? "Any rule may match" : "Every rule must match";
    }
  }

  function renderRule(rule, idx, facetKeys) {
    const row = document.createElement("div");
    row.className = "filter-rule" + (rule.key ? "" : " filter-rule-empty");
    row.dataset.idx = String(idx);

    // Key picker — only keys that exist in facets, plus the rule's
    // current key (so an admin-removed key doesn't silently disappear).
    const keyOpts = [`<option value="">Select a field…</option>`]
      .concat(facetKeys.map(k =>
        `<option value="${esc(k)}" ${rule.key === k ? "selected" : ""}>${esc(k)}</option>`
      ))
      .join("");

    row.innerHTML = `
      <div class="filter-rule-head">
        <select class="filter-key-select" data-act="key" aria-label="Field">${keyOpts}</select>
        <span class="filter-rule-op muted" aria-hidden="true">is</span>
        <div class="filter-rule-spacer"></div>
        <button type="button" class="filter-rule-remove" data-act="remove"
                title="Remove rule" aria-label="Remove rule">
          <span aria-hidden="true">×</span>
        </button>
      </div>
      ${rule.key
        ? `<div class="filter-rule-body" data-role="values"></div>`
        : ""}
    `;

    // Wire events.
    const keySel = row.querySelector('[data-act="key"]');
    keySel.onchange = () => {
      rule.key = keySel.value;
      rule.values = [];
      renderFilters();
    };
    const removeBtn = row.querySelector('[data-act="remove"]');
    removeBtn.onclick = () => {
      pendingFilters.rules.splice(idx, 1);
      renderFilters();
    };

    if (rule.key) {
      const body = row.querySelector('[data-role="values"]');
      body.appendChild(renderValuePicker(rule));
    }

    return row;
  }

  // Multi-value chip picker. Values render as removable chips, an
  // inline searchable "add value" dropdown sits to their right, and a
  // "+ all" shortcut adds every remaining facet value.
  function renderValuePicker(rule) {
    const wrap = document.createElement("div");
    wrap.className = "filter-value-block";

    const all = state.facets[rule.key] || [];
    const remaining = all.filter(v => !rule.values.includes(v));

    const chipsHtml = rule.values.length
      ? rule.values.map(v => `
          <span class="filter-chip" data-val="${esc(v)}">
            <span class="filter-chip-text">${esc(v)}</span>
            <button type="button" class="filter-chip-x" data-act="chip-remove"
                    aria-label="Remove ${esc(v)}">×</button>
          </span>
        `).join("")
      : `<span class="muted filter-no-chips">No values selected — pick one or more →</span>`;

    const opts = [`<option value="">+ Add value…</option>`]
      .concat(remaining.map(v => `<option value="${esc(v)}">${esc(v)}</option>`))
      .join("");
    const allBtn = remaining.length
      ? `<button type="button" class="filter-value-all" data-act="add-all" title="Add all remaining values">+ all (${remaining.length})</button>`
      : "";

    wrap.innerHTML = `
      <div class="filter-chips" data-role="chips">${chipsHtml}</div>
      <div class="filter-value-add">
        <select class="filter-value-select" data-act="add-value">${opts}</select>
        ${allBtn}
      </div>
    `;

    // Wire chip + add events for THIS rule.
    wrap.querySelectorAll('[data-act="chip-remove"]').forEach(btn => {
      btn.onclick = () => {
        const v = btn.parentElement.dataset.val;
        rule.values = rule.values.filter(x => x !== v);
        renderFilters();
      };
    });
    const addSel = wrap.querySelector('[data-act="add-value"]');
    if (addSel) {
      addSel.onchange = () => {
        const v = addSel.value;
        if (!v) return;
        if (!rule.values.includes(v)) rule.values.push(v);
        renderFilters();
      };
    }
    const addAllBtn = wrap.querySelector('[data-act="add-all"]');
    if (addAllBtn) {
      addAllBtn.onclick = () => {
        rule.values = (state.facets[rule.key] || []).slice();
        renderFilters();
      };
    }
    return wrap;
  }

  // Badge / button highlight reflects APPLIED filters (not staged).
  function updateFilterCount() {
    const n = filterCount(state.metaFilters);
    const badge = $("filtersCount");
    if (badge) {
      badge.textContent = n ? String(n) : "";
      badge.classList.toggle("hidden", n === 0);
    }
    const btn = $("filtersBtn");
    if (btn) btn.classList.toggle("active", n > 0);
  }

  // Apply is enabled only when the staged filter differs from applied,
  // and only when every rule has at least one value (otherwise we'd
  // send invalid rules).
  function updateApplyState() {
    const apply = $("filtersApply");
    if (!apply) return;
    const staged = pruneFilter(pendingFilters, state.facets);
    const applied = pruneFilter(state.metaFilters, state.facets);
    apply.disabled = (canonicalFilter(staged) === canonicalFilter(applied));
  }

  // Canonical serialisation so we can diff staged vs applied.
  function canonicalFilter(f) {
    const op = f.operator === "OR" ? "OR" : "AND";
    const rules = (f.rules || [])
      .map(r => ({ key: r.key || "", values: (r.values || []).slice().sort() }))
      .filter(r => r.key && r.values.length)
      .sort((a, b) => a.key.localeCompare(b.key));
    return JSON.stringify({ op, rules });
  }

  function applyFilters() {
    // Drop rules with no values (otherwise the backend ignores them
    // anyway, and we'd inflate the "active filters" count).
    const cleaned = cloneFilter(pendingFilters);
    cleaned.rules = cleaned.rules.filter(r => r.key && r.values.length);
    state.metaFilters = cleaned;
    pendingFilters = cloneFilter(state.metaFilters);
    renderFilters();
    const pop = $("filtersPop");
    const btn = $("filtersBtn");
    if (pop) pop.classList.remove("open");
    if (btn) btn.setAttribute("aria-expanded", "false");
    const n = filterCount(state.metaFilters);
    OA.toast(n
      ? `Filters applied · ${n} ${n === 1 ? "rule" : "rules"} (${state.metaFilters.operator})`
      : "Filters cleared");
  }

  // Popover open/close + add-rule + AND/OR + clear + apply. The static
  // chrome elements never get replaced so we can bind handlers once.
  (function bindFilters() {
    const btn = $("filtersBtn");
    const pop = $("filtersPop");
    const clear = $("filtersClear");
    const apply = $("filtersApply");
    const addRuleBtn = $("filtersAddRule");
    const opToggle = $("filtersOpToggle");

    if (btn && pop) {
      btn.addEventListener("click", (e) => {
        e.stopPropagation();
        const opening = !pop.classList.contains("open");
        if (opening) {
          // Sync the staged copy to what's applied each time we open.
          pendingFilters = cloneFilter(state.metaFilters);
          renderFilters();
        }
        pop.classList.toggle("open", opening);
        btn.setAttribute("aria-expanded", String(opening));
      });
      pop.addEventListener("click", (e) => e.stopPropagation());
      document.addEventListener("click", (e) => {
        if (!e.target.closest("#filtersWrap")) {
          pop.classList.remove("open");
          btn.setAttribute("aria-expanded", "false");
        }
      });
    }

    if (opToggle) {
      opToggle.addEventListener("click", (e) => {
        const t = e.target.closest(".seg-btn");
        if (!t) return;
        pendingFilters.operator = (t.dataset.op === "OR") ? "OR" : "AND";
        renderCombinator();
        updateApplyState();
      });
    }

    if (addRuleBtn) {
      addRuleBtn.addEventListener("click", () => {
        pendingFilters.rules.push({ key: "", values: [] });
        renderFilters();
      });
    }

    if (clear) {
      clear.addEventListener("click", () => {
        const hadApplied = filterCount(state.metaFilters) > 0;
        const hadStaged = pendingFilters.rules.length > 0;
        if (!hadApplied && !hadStaged) {
          OA.toast("Nothing to clear");
          return;
        }
        // Clear both staged and applied immediately so the chip count
        // updates and the popover shows the empty state without a
        // second "Apply" click.
        pendingFilters = emptyFilter();
        state.metaFilters = emptyFilter();
        renderFilters();
        OA.toast("Filters cleared");
      });
    }
    if (apply) apply.addEventListener("click", applyFilters);
  })();

  // ---- LLM model picker (CrossAdmin / SuperAdmin only) -----------------
  async function loadModels() {
    const wrap = $("modelPicker");
    const sel = $("modelSelect");
    if (!wrap || !sel) return;
    try {
      const r = await OA.get("/chat/models", { silent: true });
      const models = (r && r.models) || [];
      const active = (r && r.active) || (models[0] && models[0].id) || null;
      const canSwitch = !!(r && r.can_switch);
      state.modelId = active;   // informational only; generation uses the global
      if (!canSwitch || models.length <= 1) { wrap.classList.add("hidden"); return; }
      sel.innerHTML = models.map(m =>
        `<option value="${esc(m.id)}" ${m.id === active ? "selected" : ""}>${esc(m.label)}</option>`
      ).join("");
      sel.value = active;
      wrap.classList.remove("hidden");
    } catch (_) {
      wrap.classList.add("hidden");
    }
  }
  (function bindModelPicker() {
    const sel = $("modelSelect");
    if (!sel) return;
    // Sets the model used by CrossAdmin / SuperAdmin only (NOT regular
    // users — those are controlled from the admin portal).
    sel.addEventListener("change", async () => {
      const id = sel.value;
      const label = (sel.options[sel.selectedIndex] || {}).text || id;
      try {
        await OA.post("/chat/models/active", { model_id: id });
        state.modelId = id;
        OA.toast(`Your model · ${label}`);
      } catch (e) {
        OA.toast(e.message || "Couldn't change model", "error");
        if (state.modelId) sel.value = state.modelId;   // revert on failure
      }
    });
  })();

  function renderDeptMenu() {
    // Inject (or reuse) the floating menu under the brand block.
    let menu = document.getElementById("deptMenu");
    if (!menu) {
      menu = document.createElement("div");
      menu.id = "deptMenu";
      menu.className = "dept-menu";
      document.querySelector(".sidebar-head").appendChild(menu);
    }
    const u = OA.user();
    const activeCode = (u.department_code || "").toLowerCase();
    // Only mark depts as active if they're still in the server-filtered
    // list — i.e. still active. A user's JWT may carry codes for depts
    // that an admin has since deactivated; filtering here prevents us
    // from submitting them on Apply.
    const visibleCodes = new Set(state.departments.map(d => d.code.toLowerCase()));
    const activeSet = new Set(
      (u.department_codes || [])
        .map(c => c.toLowerCase())
        .filter(c => visibleCodes.has(c))
    );
    menu.innerHTML = "";

    // Multi-dept users get checkbox-style toggling; single-dept users
    // pick exactly one.
    if (isMultiDept) {
      const head = document.createElement("div");
      head.className = "dept-menu-head muted";
      head.textContent = "Active departments";
      menu.appendChild(head);
    }

    state.departments.forEach(d => {
      const checked = activeSet.has(d.code);
      const item = document.createElement("button");
      item.className = "dept-menu-item"
        + (d.code === activeCode ? " active" : "")
        + (isMultiDept && checked ? " checked" : "");
      item.innerHTML = `
        <span class="icon">${deptIconSvg(d.code)}</span>
        <span class="name">${d.name}</span>
        ${isMultiDept ? '<span class="dept-menu-check">' + (checked ? Icons.check : "") + '</span>' : ""}
      `;
      item.onclick = (e) => {
        e.preventDefault();
        // Critical for multi-dept: stop the event bubbling to the
        // document-level outside-click handler, otherwise the menu
        // collapses every time the user toggles a department.
        e.stopPropagation();
        if (isMultiDept) {
          toggleDepartment(d.code);
        } else {
          switchDepartment(d.code);
        }
      };
      menu.appendChild(item);
    });

    if (isMultiDept) {
      const apply = document.createElement("button");
      apply.className = "dept-menu-apply primary";
      apply.textContent = "Apply";
      apply.onclick = (e) => {
        e.stopPropagation();
        commitMultiDept();
      };
      menu.appendChild(apply);
    }
  }

  // Local pending multi-dept set; committed on Apply.
  let pendingDepts = null;

  function toggleDepartment(code) {
    const u = OA.user();
    if (pendingDepts === null) {
      // Seed the pending set from the user's current scope, but drop any
      // dept that's no longer active (e.g. admin deactivated it after
      // login). Otherwise toggling A would still try to submit the
      // stale inactive dept and the server would 404.
      const visible = new Set(state.departments.map(d => d.code.toLowerCase()));
      pendingDepts = new Set(
        (u.department_codes || [])
          .map(c => c.toLowerCase())
          .filter(c => visible.has(c))
      );
    }
    if (pendingDepts.has(code)) pendingDepts.delete(code);
    else pendingDepts.add(code);
    // Re-render so checkmarks update and forcibly keep the menu open
    // (toggling a department must NEVER close the dropdown — only
    // Apply / outside-click does that).
    renderDeptMenuWithPending();
    const menu = document.getElementById("deptMenu");
    if (menu) {
      menu.classList.add("open");
      $("deptSwitch").setAttribute("aria-expanded", "true");
      $("deptChevron").innerHTML = Icons.chevronUp;
    }
  }

  function renderDeptMenuWithPending() {
    renderDeptMenu();
    if (!pendingDepts) return;
    // Override the checkmarks to reflect the *pending* (uncommitted)
    // selection. The menu items render in the same order as
    // state.departments, so we can index them 1:1.
    const items = document.querySelectorAll("#deptMenu .dept-menu-item");
    state.departments.forEach((d, i) => {
      const el = items[i];
      if (!el) return;
      const checked = pendingDepts.has(d.code);
      el.classList.toggle("checked", checked);
      const slot = el.querySelector(".dept-menu-check");
      if (slot) slot.innerHTML = checked ? Icons.check : "";
    });
  }

  async function commitMultiDept() {
    const codes = Array.from(pendingDepts || []);
    if (!codes.length) {
      OA.toast("Pick at least one department", "error");
      return;
    }
    try {
      const prevHome = (OA.user().department_code || "").toLowerCase();
      
      const res = await OA.post("/auth/departments", { department_codes: codes });
      OA.setToken(res.access_token);
      OA.setUser(res.user);
      OA.toast(`Scope updated · ${codes.length} dept${codes.length > 1 ? "s" : ""}`);
      $("deptLabel").textContent = formatDeptLabel();
      $("helpDept").textContent = res.user.department_name || res.user.department_code || "support";
      pendingDepts = null;
      renderDeptMenu();
      loadFacets();
      document.getElementById("deptMenu").classList.remove("open");

      // The WebSocket reads `depts` from the JWT at connect time, so
      // we have to drop it and reconnect with the new token. The next
      // send() will lazily re-open via ensureWS().
      if (state.ws) {
        try { state.ws.close(); } catch (_) {}
        state.ws = null;
      }

      const newHome = (res.user.department_code || "").toLowerCase();
      // Only reset the chat if the *home/active* dept changed — which
      // happens when the user deselects the dept the current session
      // is anchored to. Mid-session widen/narrow of the multi-dept
      // scope leaves the conversation intact; the next query simply
      // pulls from the new dept set.
      if (newHome !== prevHome) {
        state.sessionId = null;
        state.metaFilters = emptyFilter();
        budget.sessionInput = 0;
        budget.sessionOutput = 0;
        fetchUsage();
        $("messages").innerHTML = "";
        $("chatTitle").textContent = "New conversation";
        showEmpty();
        loadSessions();
        loadSeed();
      }
    } catch (e) {
      OA.toast(e.message || "Couldn't update scope", "error");
    }
  }

  $("deptSwitch").addEventListener("click", (e) => {
    e.stopPropagation();
    const menu = document.getElementById("deptMenu");
    if (!menu) return;
    const opening = !menu.classList.contains("open");
    if (opening) {
      // Always reopen from the *committed* scope: discard any uncommitted
      // pending selection from a previous open and re-render so the
      // checkmarks reflect reality (fixes the stale "Active departments"
      // state when the menu was closed without hitting Apply).
      pendingDepts = null;
      renderDeptMenu();
    }
    menu.classList.toggle("open", opening);
    $("deptSwitch").setAttribute("aria-expanded", String(opening));
    $("deptChevron").innerHTML = opening ? Icons.chevronUp : Icons.chevronDown;
  });
  document.addEventListener("click", (e) => {
    const menu = document.getElementById("deptMenu");
    if (menu && !e.target.closest(".sidebar-head")) {
      menu.classList.remove("open");
      $("deptSwitch").setAttribute("aria-expanded", "false");
      $("deptChevron").innerHTML = Icons.chevronDown;
    }
  });

  async function switchDepartment(code) {
    if (!code) return;
    try {
      // Same endpoint used at first login — re-issues a JWT scoped to
      // the newly-selected department.
      const res = await OA.post("/auth/department", { department_code: code });
      OA.setToken(res.access_token);
      OA.setUser(res.user);
      OA.toast(`Switched to ${res.user.department_name || code}`);
      $("deptLabel").textContent = res.user.department_name || code;
      $("helpDept").textContent = res.user.department_name || code;
      // Reset chat — the new dept has its own KB / sessions.
      state.sessionId = null;
      state.metaFilters = emptyFilter();
      budget.sessionInput = 0;
      budget.sessionOutput = 0;
      fetchUsage();
      $("messages").innerHTML = "";
      $("chatTitle").textContent = "New conversation";
      showEmpty();
      renderDeptMenu();
      loadFacets();
      loadSessions();
      loadSeed();
      document.getElementById("deptMenu").classList.remove("open");
    } catch (e) {
      OA.toast(e.message || "Couldn't switch department", "error");
    }
  }

  // ==========================================================================
  // TOKEN USAGE METER — backed by GET /auth/me/usage (per-user, monthly).
  // ==========================================================================
  function formatResetDate(iso) {
    if (!iso) return "—";
    try {
      const d = new Date(iso);
      return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
    } catch { return "—"; }
  }

  function refreshUsageUI() {
    const used = budget.used;
    const limit = budget.limit;
    const unlimited = !limit || limit <= 0;
    const pct = unlimited ? 0 : Math.min(100, (used / limit) * 100);

    $("usageCount").textContent = used.toLocaleString();
    $("usageUsed").textContent = used.toLocaleString();
    $("usageInput").textContent = budget.sessionInput.toLocaleString();
    $("usageOutput").textContent = budget.sessionOutput.toLocaleString();
    $("usageRemaining").textContent = unlimited
      ? "∞"
      : Math.max(0, limit - used).toLocaleString();
    $("usageQuota").textContent = unlimited ? "Unlimited" : limit.toLocaleString();
    $("usageSub").textContent = unlimited
      ? "monthly · unlimited"
      : `monthly · resets ${formatResetDate(budget.period_end)}`;

    const bar = $("usageBarFill");
    bar.style.width = pct + "%";
    bar.classList.toggle("warn",   !unlimited && pct >= 60 && pct < 85);
    bar.classList.toggle("danger", !unlimited && pct >= 85);

    const trigger = $("usageBtn");
    if (trigger) {
      trigger.classList.toggle("usage-warn",   !unlimited && pct >= 60 && pct < 85);
      trigger.classList.toggle("usage-danger", !unlimited && pct >= 85);
      trigger.title = unlimited
        ? `${used.toLocaleString()} tokens used this month · unlimited`
        : `${used.toLocaleString()} / ${limit.toLocaleString()} tokens this month`;
    }

    const foot = budget.limit_source === "user"
      ? `Custom limit for ${me.email}`
      : `Default limit · ${me.email}`;
    $("usageFootUser").textContent = foot;
  }

  // Pull live usage from the backend. Called on load and after each
  // assistant reply (so the meter reflects the just-spent tokens).
  async function fetchUsage() {
    try {
      const u = await OA.get("/auth/me/usage", { silent: true });
      budget.used = u.used || 0;
      budget.limit = u.limit || 0;
      budget.remaining = u.remaining;
      budget.period_end = u.period_end;
      budget.limit_source = u.limit_source || "system_default";
      budget.exceeded = !!u.exceeded;
    } catch (_) {
      // Network blip — keep prior values; never crash the chat UI.
    }
    refreshUsageUI();
  }

  function addUsage(input, output) {
    budget.sessionInput += (input || 0);
    budget.sessionOutput += (output || 0);
    // Optimistic local bump so the meter moves immediately; the next
    // fetchUsage() will reconcile against the authoritative number.
    budget.used += (input || 0) + (output || 0);
    refreshUsageUI();
  }

  fetchUsage();

  // ==========================================================================
  // MARKDOWN
  // ==========================================================================
  marked.setOptions({
    breaks: true, gfm: true,
    highlight: (code, lang) => {
      try {
        if (lang && hljs.getLanguage(lang)) return hljs.highlight(code, { language: lang }).value;
        return hljs.highlightAuto(code).value;
      } catch { return code; }
    },
  });
  function renderMarkdown(md) {
    const cleaned = (md || "").replace(/```json\s*\{[\s\S]*?\}\s*```\s*$/m, "");
    return DOMPurify.sanitize(marked.parse(cleaned));
  }

  // ==========================================================================
  // "ROBOT WORKING" INDICATOR  (shown while retrieving / generating)
  // Replaces the old three-dot typing blip with a branded animated bot +
  // a status line that cycles through what the assistant is doing.
  // ==========================================================================
  const THINKING_STEPS = [
    "Waking up the assistant…",
    "Searching the knowledge base…",
    "Reading the most relevant sources…",
    "Connecting the dots…",
    "Composing your answer…",
  ];
  let thinkingTimer = null;

  function showThinking(bub) {
    stopThinking();
    bub.classList.add("is-thinking");
    bub.innerHTML = `
      <div class="ai-thinking" role="status" aria-live="polite">
        <span class="ai-thinking-bot">${Icons.robot}</span>
        <span class="ai-thinking-dots"><i></i><i></i><i></i></span>
        <span class="ai-thinking-text">${THINKING_STEPS[0]}</span>
      </div>`;
    let i = 0;
    thinkingTimer = setInterval(() => {
      const t = bub.querySelector(".ai-thinking-text");
      if (!t) { stopThinking(); return; }   // bubble was replaced
      i = (i + 1) % THINKING_STEPS.length;
      t.textContent = THINKING_STEPS[i];
    }, 1500);
  }

  function stopThinking() {
    if (thinkingTimer) { clearInterval(thinkingTimer); thinkingTimer = null; }
  }

  // ==========================================================================
  // SESSIONS
  // ==========================================================================
  async function loadSessions() {
    try {
      const list = await OA.get("/chat/sessions", { silent: true });
      state.sessions = list;
      const ul = $("sessionList");
      ul.innerHTML = "";
      list.forEach(s => {
        const li = document.createElement("li");
        li.dataset.id = s.id;
        if (s.id === state.sessionId) li.classList.add("active");

        const title = document.createElement("span");
        title.className = "session-title";
        title.textContent = s.title;
        title.onclick = () => openSession(s.id);
        li.appendChild(title);

        // Hover-reveal trash icon for the whole conversation. Click is
        // captured (stopPropagation) so it doesn't also open the chat.
        const del = document.createElement("button");
        del.type = "button";
        del.className = "session-del";
        del.title = "Delete this conversation";
        del.setAttribute("aria-label", "Delete conversation");
        del.innerHTML = Icons.trash;
        del.onclick = (e) => {
          e.stopPropagation();
          deleteSession(s.id, s.title);
        };
        li.appendChild(del);

        ul.appendChild(li);
      });
    } catch (_) {}
  }

  async function deleteSession(id, title) {
    if (!confirm(`Delete the conversation "${title || "Untitled"}"? This can't be undone.`)) return;
    try {
      await OA.del(`/chat/sessions/${id}`);
      // If the open chat is the one being deleted, reset to a fresh new
      // conversation; otherwise just refresh the list.
      if (state.sessionId === id) {
        state.sessionId = null;
        $("messages").innerHTML = "";
        $("chatTitle").textContent = "New conversation";
        showEmpty();
        loadSeed();
      }
      await loadSessions();
      OA.toast("Conversation deleted");
    } catch (e) {
      OA.toast(e.message || "Couldn't delete conversation", "error");
    }
  }

  async function openSession(id) {
    state.sessionId = id;
    [...$("sessionList").children].forEach(c => c.classList.toggle("active", +c.dataset.id === id));
    try {
      const msgs = await OA.get(`/chat/sessions/${id}`, { silent: true });
      $("messages").innerHTML = "";
      msgs.forEach(m => appendMessage(m.role, m.content, {
        messageId: m.id, citations: m.citations, confidence: m.confidence,
      }));
      const s = state.sessions.find(x => x.id === id);
      $("chatTitle").textContent = s ? s.title : "Conversation";
      scrollToBottom();
    } catch (_) {}
  }

  $("newChatBtn").onclick = () => {
    state.sessionId = null;
    $("messages").innerHTML = "";
    $("chatTitle").textContent = "New conversation";
    showEmpty();
    loadSeed();
  };

  // ==========================================================================
  // EMPTY STATE + SEED PROMPTS
  // ==========================================================================
  function showEmpty() {
    $("messages").innerHTML = `
      <div class="empty-state">
        <h1>What would you like to know?</h1>
        <p>Ask anything from your department's knowledge base — answers come with citations and confidence scores.</p>
        <div class="suggest-grid" id="suggestGrid"></div>
      </div>`;
  }

  function pickGlyphKey(text) {
    const t = (text || "").toLowerCase();
    if (/(leave|holiday|pto|vacation)/.test(t)) return "palm";
    if (/(salary|payroll|payslip|bonus|reimburs)/.test(t)) return "finance";
    if (/(policy|policies|rule|guideline)/.test(t)) return "book";
    if (/(password|access|login|account|vpn)/.test(t)) return "lock";
    if (/(invoice|expense|claim|receipt)/.test(t)) return "receipt";
    if (/(contract|legal|nda|agreement)/.test(t)) return "legal";
    if (/(laptop|hardware|software|jira)/.test(t)) return "it";
    if (/(onboard|induction|joining|training)/.test(t)) return "school";
    if (/(security|breach|incident)/.test(t)) return "shield";
    if (/(travel|flight|trip)/.test(t)) return "plane";
    if (/(perform|review|kpi|goal)/.test(t)) return "activity";
    return "sparkles";
  }

  async function loadSeed() {
    try {
      const r = await OA.get("/chat/suggestions/seed", { silent: true });
      const grid = $("suggestGrid");
      if (!grid) return;
      grid.innerHTML = "";
      r.suggestions.forEach(s => {
        const c = document.createElement("button");
        c.className = "suggest-card";
        const svg = Icons[pickGlyphKey(s)] || Icons.sparkles;
        c.innerHTML = `
          <span class="suggest-icon">${svg}</span>
          <span class="suggest-text">${s.replace(/[<>]/g, "")}</span>`;
        c.onclick = () => { $("input").value = s; send(); };
        grid.appendChild(c);
      });
    } catch (_) {}
  }

  // ==========================================================================
  // MESSAGE RENDERING
  // ==========================================================================
  function scrollToBottom() {
    const box = $("chatScroll");
    box.scrollTop = box.scrollHeight;
  }

  function appendMessage(role, content, meta = {}) {
    const empty = document.querySelector(".empty-state");
    if (empty) empty.remove();

    const wrap = document.createElement("div");
    wrap.className = `msg ${role}`;
    const av = document.createElement("div");
    av.className = "av";
    av.textContent = role === "user" ? (state.user.email || "U")[0].toUpperCase() : "AI";
    const bub = document.createElement("div");
    bub.className = "bubble";
    if (role === "assistant") bub.innerHTML = renderMarkdown(content || "");
    else bub.textContent = content;
    wrap.appendChild(av);
    wrap.appendChild(bub);

    $("messages").appendChild(wrap);

    if (role === "assistant" && meta.messageId) {
      bub.appendChild(buildMeta(meta));
      bub.appendChild(buildFeedback(meta.messageId));
    }
    scrollToBottom();
    return { wrap, bub };
  }

  function renderBlockedMessage(reasons) {
    // Compose a friendly block notice. Avoid leaking the raw regex
    // that fired the rule — just show a clean summary + suggestion.
    const summary = (Array.isArray(reasons) && reasons.length)
      ? reasons.map(r => r.replace(/\s*\([^)]*\)\s*$/, "").trim()).join("; ")
      : "the request couldn't be processed safely";
    const body =
      `### Blocked by guardrails\n\n` +
      `Your request was held back because **${summary}**. ` +
      `Please rephrase your question — for example, drop any sensitive details, ` +
      `policy-bypass language, or non-${(OA.user().department_name || "department").toLowerCase()} topics.`;
    if (state.currentAsstEl) {
      state.currentAsstEl.classList.remove("is-thinking");
      state.currentAsstEl.classList.add("guardrail-blocked");
      state.currentAsstEl.innerHTML = renderMarkdown(body);
    } else {
      const { bub } = appendMessage("assistant", "");
      bub.classList.add("guardrail-blocked");
      bub.innerHTML = renderMarkdown(body);
    }
    state.currentAsstEl = null;
    state.currentAsstBuffer = "";
    scrollToBottom();
  }

  function sourceChip(src) {
    const map = {
      rule_engine: { label: "Rule engine", cls: "info" },
      kb:          { label: "Knowledge base", cls: "success" },
      llm:         { label: "LLM", cls: "" },
    };
    const cfg = map[src] || null;
    if (!cfg) return null;
    const c = document.createElement("span");
    c.className = "chip " + cfg.cls;
    c.textContent = cfg.label;
    return c;
  }

  function buildMeta(meta) {
    const wrap = document.createElement("div");
    wrap.className = "msg-meta";

    const src = sourceChip(meta.source);
    if (src) wrap.appendChild(src);

    if (typeof meta.confidence === "number") {
      const c = document.createElement("span");
      c.className = "chip " + (meta.confidence >= 0.7 ? "success" : meta.confidence >= 0.4 ? "" : "warn");
      c.textContent = `Confidence ${(meta.confidence * 100).toFixed(0)}%`;
      wrap.appendChild(c);
    }
    if (meta.latency_ms) {
      const c = document.createElement("span");
      c.className = "chip"; c.textContent = `${meta.latency_ms} ms`;
      wrap.appendChild(c);
    }
    if (meta.tokens_input || meta.tokens_output) {
      const c = document.createElement("span");
      c.className = "chip";
      c.textContent = `${(meta.tokens_input || 0) + (meta.tokens_output || 0)} tok`;
      wrap.appendChild(c);
    }

    // Citations come either as { items: [...] } (REST) or as the raw
    // array (WS). The rule engine returns none.
    const cits = (meta.citations && (meta.citations.items || meta.citations)) || [];
    if (cits.length) {
      const det = document.createElement("details");
      det.className = "citations";

      const ul = document.createElement("ul");
      const seen = new Set();
      cits.slice(0, 8).forEach(c => {
        const title = c.title || (c.s3_uri ? c.s3_uri.split("/").pop() : "Document");
        const key = `${title}::${c.page || ""}::${c.department || ""}`;
        if (seen.has(key)) return;
        seen.add(key);
        const li = document.createElement("li");
        const deptTag = c.department ? ` <span class="cit-dept">[${escapeText(c.department)}]</span>` : "";
        const pageTag = c.page ? ` · p.${c.page}` : "";
        li.innerHTML = `${escapeText(title)}${deptTag}${pageTag}`;
        ul.appendChild(li);
      });
      det.innerHTML = `<summary>Sources (${seen.size})</summary>`;
      det.appendChild(ul);
      wrap.appendChild(det);
    }

    // Superadmin-only diagnostics: hallucination % + LangGraph-style trace.
    // Server already strips this for non-superadmins; we also guard here
    // so a payload that leaks through never renders for regular users.
    if (isSuperadmin && meta.diagnostics) {
      const panel = buildDiagnosticsPanel(meta.diagnostics);
      if (panel) wrap.appendChild(panel);
    }
    return wrap;
  }

  // Build the collapsible "Diagnostics" block shown under a superadmin's
  // assistant messages. Always render the hallucination chip + summary
  // stats; the trace table goes inside a <details> so it doesn't dominate
  // the bubble.
  function buildDiagnosticsPanel(d) {
    if (!d || typeof d !== "object") return null;
    const det = document.createElement("details");
    det.className = "diagnostics";

    const hPct = Math.max(0, Math.min(100, Math.round(Number(d.hallucination_pct || 0))));
    // Traffic-light buckets so the chip is *always* colored — the previous
    // 31–60 bucket used "" (the default neutral gray), which looked like
    // no styling at all. Now: green ≤30, amber 31–60, red ≥61.
    const hCls = hPct <= 30 ? "success" : hPct <= 60 ? "warn" : "error";
    // Tint the panel border to match the hallucination level so the cue is
    // visible even when the panel is collapsed.
    det.classList.add(`diag-${hCls}`);
    // Mirror the same traffic-light for the groundedness chip (inverse:
    // high groundedness is good).
    const groundPct    = Math.round(Number(d.groundedness   || 0) * 100);
    const gCls = groundPct >= 70 ? "success" : groundPct >= 40 ? "warn" : "error";
    const modelConfPct = Math.round(Number(d.model_confidence || 0) * 100);
    const retrievalPct = Math.round(Number(d.retrieval_score || 0) * 100);

    const summary = document.createElement("summary");
    summary.innerHTML = `
      <span class="diag-label">Diagnostics</span>
      <span class="chip ${hCls}" title="Higher = answer less grounded in retrieved sources">
        Hallucination ${hPct}%
      </span>
      <span class="chip ${gCls}" title="Final blended grounding score (model + retrieval)">
        Grounded ${groundPct}%
      </span>
    `;
    det.appendChild(summary);

    const body = document.createElement("div");
    body.className = "diag-body";

    // Summary signals row.
    const stats = document.createElement("div");
    stats.className = "diag-stats";
    stats.innerHTML = `
      <span><strong>Model confidence:</strong> ${modelConfPct}%</span>
      <span><strong>Retrieval score:</strong> ${retrievalPct}%</span>
      <span><strong>Groundedness:</strong> ${groundPct}%</span>
      <span><strong>Hallucination:</strong> ${hPct}%</span>
    `;
    body.appendChild(stats);

    // Trace table (one row per pipeline node).
    const steps = Array.isArray(d.trace) ? d.trace : [];
    if (steps.length) {
      const table = document.createElement("table");
      table.className = "diag-trace";
      table.innerHTML = `
        <thead>
          <tr><th>Step</th><th>Status</th><th>ms</th><th>Detail</th></tr>
        </thead>
        <tbody></tbody>
      `;
      const tbody = table.querySelector("tbody");
      steps.forEach(s => {
        const tr = document.createElement("tr");
        const stCls = s.status === "blocked" || s.status === "error" ? "warn"
                    : s.status === "skipped" ? "muted"
                    : "success";
        const detailStr = s.detail && Object.keys(s.detail).length
          ? Object.entries(s.detail)
              .map(([k, v]) => `${k}=${formatTraceVal(v)}`)
              .join(", ")
          : "";
        tr.innerHTML = `
          <td><code>${escapeText(s.step || "")}</code></td>
          <td><span class="chip ${stCls}">${escapeText(s.status || "")}</span></td>
          <td>${Number(s.duration_ms || 0)}</td>
          <td class="diag-detail">${escapeText(detailStr)}</td>
        `;
        tbody.appendChild(tr);
      });
      body.appendChild(table);
    } else {
      const empty = document.createElement("div");
      empty.className = "muted";
      empty.textContent = "(no trace recorded)";
      body.appendChild(empty);
    }

    det.appendChild(body);
    return det;
  }

  function formatTraceVal(v) {
    if (v == null) return "";
    if (Array.isArray(v)) return v.length ? `[${v.join(",")}]` : "[]";
    if (typeof v === "object") {
      try { return JSON.stringify(v); } catch (_) { return String(v); }
    }
    return String(v);
  }

  function escapeText(s) {
    return (s == null ? "" : String(s)).replace(/[&<>"']/g, c => (
      {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]
    ));
  }

  function buildFeedback(messageId) {
    const container = document.createElement("div");
    container.className = "feedback-container";
    const row = document.createElement("div");
    row.className = "feedback-row";
    const up = document.createElement("button");
    up.innerHTML = `${Icons.thumbsUp}<span>Helpful</span>`;
    const dn = document.createElement("button");
    dn.innerHTML = `${Icons.thumbsDown}<span>Not helpful</span>`;
    up.onclick = () => sendFeedback(messageId, "HELPFUL", "", up, dn);
    dn.onclick = () => openNotHelpfulPanel(container, messageId, up, dn);
    row.appendChild(up); row.appendChild(dn);
    container.appendChild(row);
    return container;
  }

  function openNotHelpfulPanel(container, messageId, upBtn, dnBtn) {
    if (container.querySelector(".fb-comment")) return;
    upBtn.disabled = true;
    dnBtn.disabled = true;
    dnBtn.classList.add("active");

    const panel = document.createElement("div");
    panel.className = "fb-comment";
    panel.innerHTML = `
      <label for="fb-${messageId}">
        What was wrong with this response? (required — this opens a support ticket)
      </label>
      <textarea id="fb-${messageId}" placeholder="Describe the issue, missing information, or what you expected…"></textarea>
      <div class="row">
        <button type="button" class="cancel" data-act="cancel">Cancel</button>
        <button type="button" class="submit" data-act="submit">Submit &amp; email support</button>
      </div>`;
    container.appendChild(panel);
    const ta = panel.querySelector("textarea"); ta.focus();

    panel.querySelector('[data-act="cancel"]').onclick = () => {
      panel.remove();
      upBtn.disabled = false;
      dnBtn.disabled = false;
      dnBtn.classList.remove("active");
    };
    panel.querySelector('[data-act="submit"]').onclick = async () => {
      const comment = ta.value.trim();
      if (!comment) { OA.toast("Please describe the issue before submitting.", "error"); ta.focus(); return; }
      panel.querySelectorAll("button, textarea").forEach(el => el.disabled = true);
      try {
        await sendFeedback(messageId, "NOT_HELPFUL", comment, upBtn, dnBtn);
        panel.remove();
      } catch (_) {
        panel.querySelectorAll("button, textarea").forEach(el => el.disabled = false);
      }
    };
  }

  function buildFollowUps(items) {
    if (!items || !items.length) return null;
    const wrap = document.createElement("div");
    wrap.className = "follow-ups";
    items.forEach(s => {
      const b = document.createElement("button");
      b.textContent = s;
      b.onclick = () => { $("input").value = s; send(); };
      wrap.appendChild(b);
    });
    return wrap;
  }

  async function sendFeedback(messageId, kind, comment, upBtn, dnBtn) {
    upBtn.disabled = dnBtn.disabled = true;
    try {
      const r = await OA.post("/feedback", { message_id: messageId, kind, comment: comment || null });
      (kind === "HELPFUL" ? upBtn : dnBtn).classList.add("active");
      OA.toast(r.status === "ticket_created"
        ? `Thanks — ticket #${r.ticket_id} opened and emailed to support.`
        : "Thanks for the feedback.");
    } catch (e) {
      upBtn.disabled = dnBtn.disabled = false;
      throw e;
    }
  }

  // ==========================================================================
  // SENDING (WebSocket + REST fallback)
  // ==========================================================================
  // Recover the composer if a stream is interrupted (socket dropped,
  // server restarted, network blip). Without this, a half-finished reply
  // leaves the typing indicator spinning forever and the Send button
  // permanently disabled.
  function failStreamingUI(msg) {
    if (!state.streaming) return;
    stopThinking();
    if (state.currentAsstEl) {
      state.currentAsstEl.classList.remove("is-thinking");
      state.currentAsstEl.classList.add("guardrail-blocked");
      state.currentAsstEl.innerHTML = renderMarkdown(
        `_${msg || "Connection lost before a reply arrived. Please try again."}_`);
    }
    state.currentAsstEl = null;
    state.currentAsstBuffer = "";
    state.streaming = false;
    $("sendBtn").disabled = false;
  }

  function ensureWS() {
    if (state.ws && state.ws.readyState <= 1) return state.ws;
    const ws = new WebSocket(OA.wsURL("/chat/ws"));
    ws.onmessage = onWSMessage;
    ws.onclose = (ev) => {
      state.ws = null;
      // 1008 = policy violation → the server rejected our (expired/invalid)
      // token. Drop it and return to login.
      if (ev && ev.code === 1008) {
        OA.clearToken();
        location.replace("/index.html");
        return;
      }
      // Only meaningful if a query was in flight; no-ops otherwise
      // (e.g. the deliberate close on a department switch).
      failStreamingUI("Connection closed before a reply arrived. Please try again.");
    };
    ws.onerror = () => { /* onclose fires next; recovery handled there. */ };
    state.ws = ws;
    return ws;
  }

  function onWSMessage(evt) {
    let m;
    

    try { m = JSON.parse(evt.data); } catch { return; }
  
    if (m.type === "meta") {
      state._meta = m;
      if (!state.sessionId) state.sessionId = m.session_id;
    } else if (m.type === "delta") {
      if (!state.currentAsstEl) {
        const { bub } = appendMessage("assistant", "");
        state.currentAsstEl = bub;
        state.currentAsstBuffer = "";
      }
      // First token: tear down the "robot working" indicator.
      stopThinking();
      state.currentAsstEl.classList.remove("is-thinking");
      state.currentAsstBuffer += m.text;
      state.currentAsstEl.innerHTML = renderMarkdown(state.currentAsstBuffer);
      scrollToBottom();
    } else if (m.type === "done") {
      stopThinking();
      if (state.currentAsstEl) {
        const bub = state.currentAsstEl;
        bub.classList.remove("is-thinking");
        const meta = {
          messageId: m.message_id, confidence: m.confidence,
          latency_ms: m.latency_ms,
          tokens_input: m.tokens_input, tokens_output: m.tokens_output,
          citations: state._meta ? state._meta.citations : [],
          source: m.source || "llm",
          diagnostics: m.diagnostics || null,
        };
        bub.appendChild(buildMeta(meta));
        const fu = buildFollowUps(m.suggestions);
        if (fu) bub.appendChild(fu);
        bub.appendChild(buildFeedback(m.message_id));
        addUsage(m.tokens_input, m.tokens_output);
        // Reconcile against the authoritative monthly total so other
        // sessions and other tabs converge on the same number.
        fetchUsage();
        state.currentAsstEl = null;
        state.currentAsstBuffer = "";
        state._meta = null;
        state.streaming = false;
        $("sendBtn").disabled = false;
        loadSessions();
      }
    } else if (m.type === "rule") {
      // Rule engine fast-path — single-shot, no streaming.
      stopThinking();
      if (state.currentAsstEl) {
        // Replace the robot-working indicator with the rule answer.
        state.currentAsstEl.classList.remove("is-thinking");
        state.currentAsstEl.innerHTML = renderMarkdown(m.answer || "");
      } else {
        const { bub } = appendMessage("assistant", m.answer || "");
        state.currentAsstEl = bub;
      }
      const bub = state.currentAsstEl;
      bub.appendChild(buildMeta({
        messageId: m.message_id, confidence: m.confidence,
        latency_ms: m.latency_ms,
        tokens_input: 0, tokens_output: 0,
        citations: [],
        source: m.source || "rule_engine",
        diagnostics: m.diagnostics || null,
      }));
      const fu = buildFollowUps(m.suggestions);
      if (fu) bub.appendChild(fu);
      bub.appendChild(buildFeedback(m.message_id));
      state.currentAsstEl = null;
      state.currentAsstBuffer = "";
      state.streaming = false;
      $("sendBtn").disabled = false;
      if (m.session_id && !state.sessionId) state.sessionId = m.session_id;
      loadSessions();
    } else if (m.type === "blocked") {
      // Replace the robot-working indicator (or insert fresh) with a clear
      // guardrail-block notice. Do NOT surface as an error toast.
      stopThinking();
      renderBlockedMessage(m.reasons || []);
      state.streaming = false;
      $("sendBtn").disabled = false;
    } else if (m.type === "error") {
      stopThinking();
      if (m.code === "TOKEN_BUDGET_EXCEEDED") {
        renderBlockedMessage([
          "You've used your monthly token allowance. Please contact your administrator.",
        ]);
      } else {
        OA.toast(m.message || "Server error", "error");
      }
      state.streaming = false;
      $("sendBtn").disabled = false;
      fetchUsage();
    }
  }

  async function send() {
    
    if (state.streaming) return;
    const q = $("input").value.trim();
    if (!q) return;
    appendMessage("user", q);
    $("input").value = "";
    autosize();
    state.streaming = true;
    $("sendBtn").disabled = true;

    try {
      const ws = ensureWS();
      const payload = { type: "query", query: q, session_id: state.sessionId, metadata_filters: state.metaFilters };
      
      if (ws.readyState === 1) ws.send(JSON.stringify(payload));
      else ws.addEventListener("open", () => ws.send(JSON.stringify(payload)), { once: true });

      const { bub } = appendMessage("assistant", "");
      showThinking(bub);
      state.currentAsstEl = bub;
      state.currentAsstBuffer = "";
    } catch (e) {
      // REST fallback path. We treat HTTP 400 from guardrails as a
      // soft, in-chat block rather than an error toast.
      stopThinking();
      if (state.currentAsstEl) { state.currentAsstEl.remove(); state.currentAsstEl = null; }
      try {
        const r = await OA.post(
          "/chat/query",
          { session_id: state.sessionId, query: q, metadata_filters: state.metaFilters },
          { silent: true },
        );
        state.sessionId = r.session_id;
        const { bub } = appendMessage("assistant", r.answer);
        bub.appendChild(buildMeta({
          messageId: r.message_id, confidence: r.confidence,
          latency_ms: r.latency_ms,
          tokens_input: r.tokens_input, tokens_output: r.tokens_output,
          citations: r.citations,
          source: r.source || "llm",
          diagnostics: r.diagnostics || null,
        }));
        addUsage(r.tokens_input, r.tokens_output);
        fetchUsage();
        const fu = buildFollowUps(r.suggestions);
        if (fu) bub.appendChild(fu);
        bub.appendChild(buildFeedback(r.message_id));
      } catch (err) {
        // GuardrailBlocked surfaces as HTTP 400 with `detail: "Blocked: …"`.
        const detail = String((err && err.message) || "");
        if (err && err.status === 400 && /^Blocked: /i.test(detail)) {
          renderBlockedMessage([detail.replace(/^Blocked:\s*/i, "")]);
        } else {
          OA.toast(detail || "Couldn't reach the assistant", "error");
        }
      } finally {
        state.streaming = false;
        $("sendBtn").disabled = false;
        loadSessions();
      }
    }
  }
  
  $("sendBtn").onclick = send;
  $("input").addEventListener("keydown", (e) => {
    
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      send();
    }
  });

  // ==========================================================================
  // AUTOSIZE + AUTOCOMPLETE
  // ==========================================================================
  function autosize() {
    const el = $("input");
    
    el.style.height = "auto";
    el.style.height = Math.min(el.scrollHeight, 200) + "px";
  }
  $("input").addEventListener("input", () => { autosize(); debouncedAuto(); });

  let acTimer = null;
  function debouncedAuto() { clearTimeout(acTimer); acTimer = setTimeout(runAutocomplete, 220); }
  async function runAutocomplete() {
    const prefix = $("input").value.trim();
    const ac = $("autocomplete");
    if (prefix.length < 2) { ac.classList.remove("open"); ac.innerHTML = ""; return; }
    try {
      const r = await OA.post("/chat/suggestions/autocomplete", { prefix }, { silent: true });
      if (!r.suggestions.length) { ac.classList.remove("open"); return; }
      ac.innerHTML = "";
      r.suggestions.forEach(s => {
        const it = document.createElement("div");
        it.className = "ac-item";
        it.textContent = s;
        it.onclick = () => { $("input").value = s; ac.classList.remove("open"); $("input").focus(); };
        ac.appendChild(it);
      });
      ac.classList.add("open");
    } catch (_) {}
  }
  document.addEventListener("click", (e) => {
    if (!e.target.closest(".composer-wrap")) $("autocomplete").classList.remove("open");
  });

  // ==========================================================================
  // HEADER + FOOTER ACTIONS
  // ==========================================================================
  $("logoutBtn").onclick = () => { OA.clearToken(); location.href = "/index.html"; };
  $("sidebarToggle").onclick = () => {
    if (window.matchMedia("(max-width: 880px)").matches) {
      $("sidebar").classList.toggle("open");
    } else {
      document.querySelector(".chat-body").classList.toggle("sidebar-collapsed");
    }
  };
  $("exportBtn").onclick = async () => {
    if (!state.sessionId) return OA.toast("No active conversation");
    const r = await OA.get(`/chat/sessions/${state.sessionId}/export`);
    const blob = new Blob([r.content], { type: "text/plain" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = r.filename;
    a.click();
  };
  // The help button now lives at the bottom of the body; treat the
  // click as a quick path to support flow.
  $("helpBtn").onclick = () => {
    OA.toast("Tip: tap “Not helpful” on any assistant reply to open a support ticket with the full chat attached.");
  };

  // Toggle popover on tap (mobile) — :hover handles desktop.
  $("usageBtn").onclick = (e) => {
    e.stopPropagation();
    $("usageWrap").classList.toggle("open");
  };
  document.addEventListener("click", (e) => {
    if (!e.target.closest("#usageWrap")) $("usageWrap").classList.remove("open");
  });

  // ==========================================================================
  // BOOT
  // ==========================================================================
  loadSessions();
  loadSeed();
  loadDepartments();
  loadModels();
})();
