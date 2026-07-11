/**
 * Admin console — overview, departments, users, KB management, tickets, audit.
 *
 * Highlights of this revision:
 *   • CROSSADMIN role acknowledged in the gate + select options.
 *   • KB tab redesigned with a multi-file dropzone + per-file progress
 *     and a paginated/filterable file list.
 *   • Each KB row has a delete button → confirmation modal → post-delete
 *     KB resync prompt.
 *   • Departments tab supports activate / deactivate / permanent delete
 *     (the permanent delete is SUPERADMIN-only and confirmed via modal).
 */
(function () {
  if (!OA.requireAuth()) return;
  const me = OA.user();

  const ADMIN_ROLES = new Set(["ADMIN", "CROSSADMIN", "SUPERADMIN"]);
  const role = String(me.role || "").toUpperCase();

  if (!ADMIN_ROLES.has(role)) {
    document.body.className = "access-denied";
    document.body.innerHTML = `
      <div style="max-width:520px;margin:18vh auto;text-align:center;
                  background:var(--bg-1);border:1px solid var(--border);
                  border-radius:14px;padding:36px 28px;">
        <div style="display:grid;place-items:center;width:64px;height:64px;border-radius:18px;background:var(--accent-soft);color:var(--accent);margin:0 auto 14px">${window.Icons.svg("shield")}</div>
        <h2>Admin access required</h2>
        <p class="muted">Your account (<b>${esc(me.email || "")}</b>, role
        <code>${esc(role || "USER")}</code>) doesn't have permission to
        view the admin portal.</p>
        <p><a class="primary" href="/chat.html"
              style="display:inline-block;padding:10px 18px;border-radius:8px;
                     background:var(--accent);color:#fff;text-decoration:none;
                     margin-top:8px;">Back to chat</a></p>
      </div>`;
    setTimeout(() => { location.replace("/chat.html"); }, 4000);
    return;
  }

  document.getElementById("adminUser").textContent = `${me.email} · ${me.role}`;

  // Roles that can manage departments themselves (create / activate /
  // deactivate / hard-delete). Everyone else just sees the list of
  // departments they belong to.
  const SUPER_ONLY = role === "SUPERADMIN";
  // Roles that may grant multi-department access (i.e. mint CROSSADMIN
  // users). An ADMIN is pinned to a single dept and would only be able
  // to create users in that dept anyway, so the CROSSADMIN role option
  // is meaningless for them.
  const CAN_GRANT_MULTI = role === "SUPERADMIN" || role === "CROSSADMIN";
  // Roles that may create / activate / deactivate / delete departments.
  // CROSSADMIN is scoped server-side to their granted departments (and
  // the list they see is already limited to those).
  const CAN_MANAGE_DEPTS = role === "SUPERADMIN" || role === "CROSSADMIN";

  const $ = (id) => document.getElementById(id);

  // ---------- Populate nav SVG icons ----------
  document.querySelectorAll(".nav-btn[data-icon-key]").forEach(b => {
    const slot = b.querySelector(".nav-icon");
    if (slot) slot.innerHTML = window.Icons.svg(b.dataset.iconKey);
  });
  const themeSun = $("themeIconSun");
  if (themeSun) themeSun.innerHTML = window.Icons.svg("sun");
  const themeMoon = $("themeIconMoon");
  if (themeMoon) themeMoon.innerHTML = window.Icons.svg("moon");
  const logoutBtn = $("logoutBtn");
  if (logoutBtn) logoutBtn.innerHTML = `${window.Icons.svg("logout")}<span style="margin-left:6px">Sign out</span>`;
  const goChatBtn = $("goChat");
  if (goChatBtn) goChatBtn.innerHTML = `${window.Icons.svg("sparkles")}<span style="margin-left:6px">Open chat</span>`;

  // Static KB-tab icons.
  if ($("uplDropIcon")) $("uplDropIcon").innerHTML = window.Icons.svg("uploadCloud");
  if ($("kbSearchIcon")) $("kbSearchIcon").innerHTML = window.Icons.svg("search");
  // The Sync KB button is now a labelled primary button — no SVG.
  if ($("kbRefreshBtn")) $("kbRefreshBtn").innerHTML = window.Icons.svg("refresh");

  // ---------- Tabs ----------
  document.querySelectorAll(".nav-btn").forEach(b => {
    b.onclick = () => activate(b.dataset.tab);
  });
  function activate(tab) {
    // Close any open menu (e.g. dept picker) so it doesn't bleed into
    // the next tab.
    document.querySelectorAll(".dept-picker-menu.open").forEach(m => m.classList.remove("open"));
    document.querySelectorAll(".nav-btn").forEach(b => b.classList.toggle("active", b.dataset.tab === tab));
    document.querySelectorAll(".tab").forEach(t => t.classList.add("hidden"));
    $(`tab-${tab}`).classList.remove("hidden");
    $("tabTitle").textContent = ({
      overview: "Overview", departments: "Departments", users: "Users",
      uploads: "Knowledge Base", tickets: "Tickets", audit: "Audit Log",
    }[tab]) || tab;
    if (tab === "overview")     loadOverview();
    if (tab === "departments")  loadDepartments();
    if (tab === "users")        loadUsersTab();
    if (tab === "uploads")      loadUploadsTab();
    if (tab === "tickets")      loadTickets();
    if (tab === "audit")        loadAudit();
  }

  // ==========================================================================
  // OVERVIEW
  // ==========================================================================
  async function loadOverview() {
    try {
      const a = await OA.get("/analytics/overview");
      const m = $("metricsGrid");
      m.innerHTML = "";
      [
        ["Daily active users", a.daily_active_users, null],
        ["Total messages (24h)", a.total_messages, null],
        ["Sessions (24h)", a.total_sessions, null],
        ["Avg latency", `${Math.round(a.avg_latency_ms)} ms`, null],
        ["Avg confidence", a.avg_confidence ? (a.avg_confidence * 100).toFixed(0) + "%" : "—", null],
        ["Feedback", `${a.feedback_helpful} helpful · ${a.feedback_not_helpful} not`, null],
        // Last entry has an action — clicking opens the details modal.
        ["Details Block", a.guardrail_violations, "guardrail"],
      ].forEach(([label, v, action]) => {
        const c = document.createElement("div");
        c.className = "metric-card" + (action ? " metric-clickable" : "");
        c.innerHTML = `<div class="metric-label">${label}${action ? ' <span class="metric-link">View details →</span>' : ''}</div><div class="metric-value">${v}</div>`;
        if (action === "guardrail") {
          c.onclick = openGuardrailDetails;
          c.setAttribute("role", "button");
          c.setAttribute("tabindex", "0");
          c.addEventListener("keydown", (e) => {
            if (e.key === "Enter" || e.key === " ") {
              e.preventDefault();
              openGuardrailDetails();
            }
          });
        }
        m.appendChild(c);
      });
      const tq = $("topQueries").querySelector("tbody");
      tq.innerHTML = a.top_queries.map(r => `<tr><td>${esc(r.query)}</td><td>${r.count}</td></tr>`).join("") || `<tr><td colspan="2" class="muted">No data</td></tr>`;
      const du = $("deptUsage").querySelector("tbody");
      du.innerHTML = a.department_usage.map(r => `<tr><td>${esc(r.department)}</td><td>${r.sessions}</td></tr>`).join("") || `<tr><td colspan="2" class="muted">No data</td></tr>`;
    } catch (_) {}
    loadUserModelsCard();
  }

  // ---- User-accessible model set ----
  async function refreshUserModels() {
    try {
      const r = await OA.get("/admin/user-models", { silent: true });
      const allModels = (r && r.all_models) || [];
      const selectedIds = new Set((r && r.user_model_ids) || []);
      userSelectableModels = allModels.filter(m => selectedIds.has(m.id));
      return r;
    } catch (_) {
      return null;
    }
  }


  async function loadUserModelsCard() {
    const availEl   = $("availableModels");
    const selEl     = $("selectedModels");
    const applyBtn  = $("applyBtn");
    const avSearch  = $("availableSearch");
    const slSearch  = $("selectedSearch");
    if (!availEl || !selEl) return;

    let _all = [], _granted = new Set(), _initial = new Set(), _canManage = false;
    let _avSel = new Set(), _slSel = new Set();

    function updateCounts() {
      const ac = $("availableCount"), sc = $("selectedCount");
      if (ac) ac.textContent = _all.length - _granted.size;
      if (sc) sc.textContent = _granted.size;
    }

    function syncApply() {
      if (!applyBtn) return;
      const changed = _granted.size !== _initial.size || [..._granted].some(id => !_initial.has(id));
      applyBtn.disabled = !changed;
    }

    function renderPanel(container, models, selSet, isGranted) {
      container.innerHTML = "";
      if (!models.length) {
        container.innerHTML = `<div class="shuttle-empty"><i class="ti ti-inbox" aria-hidden="true"></i><span>${isGranted ? "No models granted yet" : "All models granted"}</span></div>`;
        return;
      }
      models.forEach(m => {
        const row = document.createElement("div");
        row.className = "shuttle-item" + (selSet.has(m.id) ? " sel" : "");
        row.dataset.id = m.id;
        row.setAttribute("role", "option");
        row.setAttribute("aria-selected", selSet.has(m.id));
        row.innerHTML =
          `<i class="ti ti-cpu" aria-hidden="true"></i>` +
          `<span class="shuttle-item-label">${esc(m.label)}</span>` +
          (m.default ? `<span class="shuttle-item-badge">Default</span>` : "") +
          `<i class="ti ti-check shuttle-item-check" aria-hidden="true"></i>`;
        if (_canManage) {
          row.addEventListener("click", e => {
            const id = row.dataset.id;
            if (e.ctrlKey || e.metaKey) {
              selSet.has(id) ? selSet.delete(id) : selSet.add(id);
            } else {
              selSet.clear();
              selSet.add(id);
            }
            renderLists();
          });
          row.addEventListener("dblclick", () => {
            if (isGranted) { _granted.delete(m.id); _slSel.delete(m.id); }
            else           { _granted.add(m.id);    _avSel.delete(m.id); }
            renderLists();
          });
        }
        container.appendChild(row);
      });
    }

    function renderLists() {
      const af = avSearch ? avSearch.value.trim().toLowerCase() : "";
      const sf = slSearch ? slSearch.value.trim().toLowerCase() : "";
      const avail   = _all.filter(m => !_granted.has(m.id) && (!af || m.label.toLowerCase().includes(af)));
      const granted = _all.filter(m =>  _granted.has(m.id) && (!sf || m.label.toLowerCase().includes(sf)));
      renderPanel(availEl, avail,   _avSel, false);
      renderPanel(selEl,   granted, _slSel, true);
      updateCounts();
      syncApply();
    }

    function addSelected()    { _avSel.forEach(id => { _granted.add(id);    _avSel.delete(id); }); renderLists(); }
    function removeSelected() { _slSel.forEach(id => { _granted.delete(id); _slSel.delete(id); }); renderLists(); }
    function addAll()         { _avSel.clear(); _all.forEach(m => _granted.add(m.id)); renderLists(); }
    function removeAll()      { _slSel.clear(); _granted.clear(); renderLists(); }

    try {
      const r   = await OA.get("/admin/user-models");
      _all       = r.all_models || [];
      _initial   = new Set(r.user_model_ids || []);
      _granted   = new Set(_initial);
      _canManage = !!(r && r.can_manage);

      renderLists();

      const btnR = $("moveRight"), btnL = $("moveLeft");
      const btnAR = $("moveAllRight"), btnAL = $("moveAllLeft");

      if (_canManage) {
        if (btnR)  btnR.onclick  = addSelected;
        if (btnL)  btnL.onclick  = removeSelected;
        if (btnAR) btnAR.onclick = addAll;
        if (btnAL) btnAL.onclick = removeAll;
        if (avSearch) avSearch.addEventListener("input", renderLists);
        if (slSearch) slSearch.addEventListener("input", renderLists);
        if (applyBtn) {
          applyBtn.onclick = async () => {
            const chosen = [..._granted];
            applyBtn.disabled = true;
            const prev = applyBtn.textContent;
            applyBtn.textContent = "Saving…";
            try {
              await OA.post("/admin/user-models", { model_ids: chosen });
              OA.toast("User model access updated");
              _initial = new Set(_granted);
              userSelectableModels = _all.filter(m => _granted.has(m.id));
              syncApply();
            } catch (e) {
              OA.toast(e.message || "Couldn't update user models", "error");
              applyBtn.disabled = false;
            } finally {
              applyBtn.textContent = prev;
            }
          };
        }
      } else {
        [btnR, btnL, btnAR, btnAL].forEach(b => { if (b) b.disabled = true; });
        if (applyBtn) applyBtn.style.display = "none";
        const hint = $("shuttleHint");
        if (hint) hint.textContent = "Only CrossAdmin / SuperAdmin can change user model access.";
      }
    } catch (e) {
      OA.toast(e.message || "Couldn't load user models", "error");
    }
  }


  // ==========================================================================
  // GUARDRAIL BLOCKS — details modal
  // ==========================================================================
  async function openGuardrailDetails() {
    const root = $("guardrailRoot");
    if (!root) return;
    root.classList.remove("hidden");
    const close = () => root.classList.add("hidden");
    root.querySelectorAll('[data-act="gr-cancel"]').forEach(el => el.onclick = close);

    const body = $("guardrailBody");
    body.innerHTML = `<p class="muted">Loading recent guardrail events…</p>`;

    let data;
    try {
      data = await OA.get("/admin/guardrail-events?limit=50");
    } catch (e) {
      body.innerHTML = `<p class="err-text">${esc(e.message || "Couldn't load events")}</p>`;
      return;
    }
    body.innerHTML = renderGuardrailDetails(data);
  }

  function renderGuardrailDetails(data) {
    const stack = data.stack || {};
    const events = data.events || [];
    const model_name=document.getElementById("adminActiveModel").textContent;
    const stackHtml = `
      <div class="gr-stack">
        <div class="gr-stack-title">Retrieval stack</div>
        <div class="gr-stack-grid">
          <div><span class="muted">Vector store</span><strong>${esc(stack.vector_store || "—")}</strong></div>
          <div><span class="muted">Search override</span><strong>${esc(stack.search_type_override || "auto")}</strong></div>
          <div><span class="muted">Rerank enabled</span><strong>${stack.rerank_enabled ? "yes" : "no"}</strong></div>
          <div><span class="muted">AWS rerank ARN</span><strong>${esc(stack.rerank_aws_model_arn || "—")}</strong></div>
          <div><span class="muted">Local reranker</span><strong>${esc(stack.rerank_local_model || "—")}</strong></div>
          <div><span class="muted">LLM</span><strong>${model_name || "—"}</strong></div>
          <div><span class="muted">Embeddings</span><strong>${esc(stack.embedding_model_id || "—")}</strong></div>
          <div><span class="muted">Bedrock Guardrail</span><strong>${esc(stack.guardrail_id || "—")} · ${esc(stack.guardrail_version || "")}</strong></div>
        </div>
      </div>`;

    if (!events.length) {
      return stackHtml + `<p class="muted" style="margin-top:14px;">No guardrail-blocked messages in recent history.</p>`;
    }

    const rows = events.map(ev => {
      const reasons = (ev.block_reasons && Array.isArray(ev.block_reasons.reasons))
        ? ev.block_reasons.reasons.join(", ")
        : "—";
      const stage = (ev.block_reasons && ev.block_reasons.stage) || "—";
      const citCount = (ev.citations && ev.citations.count) || 0;
      const citDepts = (ev.citations && ev.citations.depts) ? ev.citations.depts.join(", ") : "—";
      const citSource = (ev.citations && ev.citations.source) || "—";
      return `
        <details class="gr-event">
          <summary>
            <span class="gr-when">${ev.created_at ? new Date(ev.created_at).toLocaleString() : "—"}</span>
            <span class="gr-stage">${esc(stage)}</span>
            <span class="muted">${esc(ev.user_email || "anon")}</span>
            <span class="muted">·</span>
            <span class="muted">dept ${esc(ev.department_code || "—")}</span>
          </summary>
          <div class="gr-event-body">
            <div class="gr-grid">
              <div><span class="muted">Reasons</span><strong>${esc(reasons)}</strong></div>
              <div><span class="muted">Model</span><strong>${esc(ev.model_id || "—")}</strong></div>
              <div><span class="muted">Confidence</span><strong>${ev.confidence == null ? "—" : (ev.confidence * 100).toFixed(0) + "%"}</strong></div>
              <div><span class="muted">Latency</span><strong>${ev.latency_ms == null ? "—" : ev.latency_ms + " ms"}</strong></div>
              <div><span class="muted">Tokens (in/out)</span><strong>${ev.tokens_input || 0} / ${ev.tokens_output || 0}</strong></div>
              <div><span class="muted">Source</span><strong>${esc(citSource)}</strong></div>
              <div><span class="muted">Citations</span><strong>${citCount} (${esc(citDepts)})</strong></div>
              <div><span class="muted">Session</span><strong>#${ev.session_id || "—"}</strong></div>
            </div>
            <div class="gr-answer">
              <span class="muted">Answer excerpt</span>
              <pre>${esc(ev.answer_excerpt || "")}</pre>
            </div>
          </div>
        </details>`;
    }).join("");

    return stackHtml
      + `<div class="gr-events-head">Recent blocked messages (${events.length})</div>`
      + `<div class="gr-events">${rows}</div>`;
  }

  // ==========================================================================
  // DEPARTMENTS
  // ==========================================================================
  async function loadDepartments() {
    // Surface / hide the "Add department" card based on role. Backend
    // also enforces SUPERADMIN-only on the POST, but hiding the form
    // makes the intent explicit.
    const addCard = $("cardAddDept");
    if (addCard) addCard.classList.toggle("hidden", !CAN_MANAGE_DEPTS);
    const heading = $("deptTableHeading");
    if (heading) heading.textContent = SUPER_ONLY ? "Departments" : "Your departments";

    const list = await OA.get("/admin/departments");
    const tb = $("deptTable").querySelector("tbody");
    const canHardDelete = CAN_MANAGE_DEPTS;

    tb.innerHTML = list.map(d => `
      <tr data-id="${d.id}" data-code="${esc(d.code)}" data-active="${d.is_active}">
        <td><code>${esc(d.code)}</code></td>
        <td>${esc(d.name)}</td>
        <td>${esc(d.support_email || "")}</td>
        <td>
          ${d.is_active
            ? `<span class="status-pill ok">${window.Icons.svg("check")}<span>Active</span></span>`
            : `<span class="status-pill off">${window.Icons.svg("x")}<span>Inactive</span></span>`
          }
        </td>
        <td class="col-actions">
          <div class="row-actions">
            ${CAN_MANAGE_DEPTS ? (d.is_active
              ? `<button class="ghost icon-btn" data-act="deactivate" title="Deactivate" aria-label="Deactivate">${window.Icons.svg("power")}</button>`
              : `<button class="ghost icon-btn ok-ghost" data-act="activate" title="Activate" aria-label="Activate">${window.Icons.svg("play")}</button>`
            ) : ""}
            ${canHardDelete
              ? `<button class="ghost icon-btn danger-ghost" data-act="delete" title="Delete permanently" aria-label="Delete">${window.Icons.svg("trash")}</button>`
              : ""
            }
          </div>
        </td>
      </tr>`).join("");

    // Wire actions.
    tb.querySelectorAll("button[data-act]").forEach(b => {
      b.onclick = async (e) => {
        const tr  = e.currentTarget.closest("tr");
        const id  = tr.dataset.id;
        const code = tr.dataset.code;
        const act = e.currentTarget.dataset.act;
        if (act === "activate") {
          await OA.post(`/admin/departments/${id}/activate`, {});
          OA.toast(`${code} re-activated`);
          loadDepartments();
        } else if (act === "deactivate") {
          confirmModal({
            title: "Deactivate department?",
            body: `Users won't be able to select <b>${esc(code)}</b> until you re-activate it. Existing data is preserved.`,
            confirmLabel: "Deactivate",
            onConfirm: async () => {
              await OA.post(`/admin/departments/${id}/deactivate`, {});
              OA.toast(`${code} deactivated`);
              loadDepartments();
            },
          });
        } else if (act === "delete") {
          confirmModal({
            title: "Delete department permanently?",
            body: `This will <b>permanently delete</b> <code>${esc(code)}</code> and remove all CrossAdmin grants. Existing KB files for the dept must be deleted first. This cannot be undone.`,
            confirmLabel: "Delete forever",
            danger: true,
            onConfirm: async () => {
              try {
                await OA.del(`/admin/departments/${id}`);
                OA.toast(`${code} deleted`);
                loadDepartments();
              } catch (e) {
                OA.toast(e.message || "Couldn't delete", "error");
              }
            },
          });
        }
      };
    });
  }
  $("addDeptBtn").onclick = async () => {
    try {
      await OA.post("/admin/departments", {
        code: $("deptCode").value.trim().toLowerCase(),
        name: $("deptName").value.trim(),
        description: $("deptDesc").value.trim() || null,
        support_email: $("deptEmail").value.trim() || null,
      });
      ["deptCode","deptName","deptDesc","deptEmail"].forEach(id => $(id).value = "");
      loadDepartments();
    } catch (_) {}
  };

  async function loadDeptOptions(selectId, { includeInactive = false } = {}) {
    const sel = $(selectId);
    const list = await OA.get(
      "/admin/departments" + (includeInactive ? "?include_inactive=true" : "?include_inactive=false"),
      { silent: true }
    );
    const visible = list.filter(d => includeInactive || d.is_active);
    if (sel) {
      sel.innerHTML = visible
        .map(d => `<option value="${esc(d.code)}">${esc(d.name)} (${esc(d.code)})</option>`)
        .join("");
    }
    // Feed the multi-picker any time the Users-tab options are reloaded.
    if (selectId === "uDept") {
      deptPicker.setDepartments(list);
      deptPicker.setMode($("uRole").value);
    }
    return list;
  }

  // ==========================================================================
  // USERS
  // ==========================================================================
  // ---- Per-user model editor (modal) --------------------------------------
  function openModelEditor({ id, email, model }) {
    const root = $("modelEditRoot");
    if (!root) return;
    const sel = $("meSelect");

    $("meEmail").textContent = email || "";
    const currentLabel = model
      ? ((userSelectableModels.find(m => m.id === model) || {}).label || "Custom")
      : "Default (standard)";
    $("meCurrent").textContent = currentLabel;

    sel.innerHTML = `<option value="">Default (standard)</option>`
      + userSelectableModels.map(m =>
        `<option value="${esc(m.id)}" ${m.id === model ? "selected" : ""}>${esc(m.label)}</option>`
      ).join("");
    sel.value = model || "";

    root.classList.remove("hidden");
    const close = () => root.classList.add("hidden");
    root.querySelectorAll('[data-act="me-cancel"]').forEach(el => el.onclick = close);

    $("meSaveBtn").onclick = async () => {
      const chosen = sel.value || null;
      try {
        await OA.patch(`/admin/users/${id}`, { preferred_model: chosen });
        OA.toast("Model preference updated");
        close();
        loadUsers();
      } catch (e) {
        OA.toast(e.message || "Couldn't update model", "error");
      }
    };
  }

  // ---- Per-user token-limit editor (modal) ------------------------------
  function openTokenLimitEditor({ id, email, used, limit }) {
    const root = $("tokenLimitRoot");
    if (!root) return;
    const input = $("tlInput");
    const currentLabel = limit === "" || limit == null
      ? "default"
      : (parseInt(limit, 10) === 0 ? "disabled" : parseInt(limit, 10).toLocaleString());

    $("tlEmail").textContent = email || "";
    $("tlUsed").textContent = (used || 0).toLocaleString();
    $("tlCurrent").textContent = currentLabel;
    // Pre-fill with the existing limit so admins can just bump it.
    input.value = (limit === "" || limit == null) ? "" : String(limit);

    root.classList.remove("hidden");
    const close = () => root.classList.add("hidden");
    root.querySelectorAll('[data-act="tl-cancel"]').forEach(el => el.onclick = close);
    setTimeout(() => input.focus(), 50);

    $("tlSaveBtn").onclick = async () => {
      const raw = input.value.trim();
      // Build the patch body. Blank => null (system default); a number
      // overrides; 0 explicitly disables. PATCH /admin/users/{id}
      // accepts monthly_token_limit and ignores other fields here.
      let payload;
      if (raw === "") {
        payload = { monthly_token_limit: null };
      } else {
        const n = parseInt(raw, 10);
        if (Number.isNaN(n) || n < 0) {
          OA.toast("Token limit must be a non-negative integer", "error");
          return;
        }
        payload = { monthly_token_limit: n };
      }
      try {
        await OA.patch(`/admin/users/${id}`, payload);
        OA.toast("Token limit updated");
        close();
        loadUsers();
      } catch (e) {
        OA.toast(e.message || "Couldn't update limit", "error");
      }
    };
  }

  // ---- Per-user role editor (modal) -------------------------------------
  async function openRoleEditor({ id, email, role: currentRole, home, extras }) {
    const root = $("roleEditRoot");
    if (!root) return;

    // Refresh dept lists for the role-editor selects.
    let depts = [];
    try {
      depts = await OA.get("/admin/departments?include_inactive=false", { silent: true });
    } catch (_) { depts = []; }
    const activeDepts = (depts || []).filter(d => d.is_active);

    const ROLE_RANK = { USER: 0, ADMIN: 1, CROSSADMIN: 2, SUPERADMIN: 3 };
    const myRank = ROLE_RANK[role] ?? -1;
    const roleSel = $("reRole");
    Array.from(roleSel.options).forEach(opt => {
      const r = opt.value;
      // SUPERADMIN can assign any role; others strictly below their own rank.
      const allowed = (role === "SUPERADMIN") || (ROLE_RANK[r] < myRank);
      const meaningful = (r !== "CROSSADMIN") || CAN_GRANT_MULTI;
      opt.hidden = !(allowed && meaningful);
    });
    roleSel.value = currentRole;

    $("reEmail").textContent = email || "";
    $("reCurrent").textContent = currentRole || "—";

    // Populate dept selectors with active depts only.
    const singleSel = $("reSingleDept");
    const multiSel = $("reDeptList");
    singleSel.innerHTML = activeDepts
      .map(d => `<option value="${esc(d.code)}">${esc(d.name)} (${esc(d.code)})</option>`)
      .join("");
    multiSel.innerHTML = activeDepts
      .map(d => `<option value="${esc(d.code)}">${esc(d.name)} (${esc(d.code)})</option>`)
      .join("");

    // Preselect current values.
    if (home) singleSel.value = home;
    const extraSet = new Set((extras || "").split(",").filter(Boolean));
    Array.from(multiSel.options).forEach(o => { o.selected = extraSet.has(o.value); });

    function updateVisibility() {
      const r = roleSel.value;
      $("reDeptWrap").classList.toggle("hidden", r !== "CROSSADMIN");
      $("reSingleDeptWrap").classList.toggle("hidden", r === "CROSSADMIN");
    }
    updateVisibility();
    roleSel.onchange = updateVisibility;

    root.classList.remove("hidden");
    const close = () => root.classList.add("hidden");
    root.querySelectorAll('[data-act="re-cancel"]').forEach(el => el.onclick = close);

    $("reSaveBtn").onclick = async () => {
      const newRole = roleSel.value;
      const payload = { role: newRole };
      if (newRole === "CROSSADMIN") {
        const codes = Array.from(multiSel.selectedOptions).map(o => o.value);
        if (!codes.length) {
          OA.toast("Pick at least one department for CrossAdmin", "error");
          return;
        }
        payload.department_codes = codes;
        payload.department_code = codes[0];
      } else {
        const c = singleSel.value || null;
        payload.department_code = c;
        // Wipe extras when leaving CrossAdmin.
        payload.department_codes = [];
      }
      try {
        await OA.patch(`/admin/users/${id}`, payload);
        OA.toast("Role updated");
        close();
        loadUsers();
      } catch (e) {
        OA.toast(e.message || "Couldn't update role", "error");
      }
    };
  }

  // User-selectable models (the 2 models regular users can pick from).
  // Populated from the /admin/model endpoint alongside the model card.
  let userSelectableModels = [];

  // Full users list as fetched; the table is (re)rendered from this so the
  // department filter can run client-side without re-hitting the server.
  let usersCache = [];

  async function loadUsersTab() {
    await refreshUserModels();
    const uModelSel = $("uModel");
    if (uModelSel) {
      uModelSel.innerHTML = `<option value="">Default (standard)</option>`
        + userSelectableModels.map(m => `<option value="${esc(m.id)}">${esc(m.label)}</option>`).join("");
    }
    const depts = await loadDeptOptions("uDept");
    // Populate the Users department filter. SUPERADMIN gets every dept;
    // CROSSADMIN/ADMIN get their accessible ones. "All departments" first.
    const sel = $("userDeptFilter");
    if (sel) {
      const prev = sel.value;
      sel.innerHTML = `<option value="">All departments</option>`
        + (depts || []).map(d => `<option value="${esc(d.code)}">${esc(d.name)} (${esc(d.code)})</option>`).join("");
      if (prev) sel.value = prev;
      if (!sel._bound) {
        sel._bound = true;
        sel.addEventListener("change", renderUsers);
      }
    }
    await loadUsers();
  }

  async function loadUsers() {
    usersCache = await OA.get("/admin/users");
    renderUsers();
  }

  function renderUsers() {
    const filter = $("userDeptFilter") ? $("userDeptFilter").value : "";
    const list = filter
      ? usersCache.filter(u =>
          u.department_code === filter || (u.department_codes || []).includes(filter))
      : usersCache;
    const tb = $("userTable").querySelector("tbody");
    const myEmail = (me.email || "").toLowerCase();
    if (!list.length) {
      tb.innerHTML = `<tr><td colspan="9" class="muted" style="padding:20px 0;text-align:center;">No users${filter ? " in this department" : ""}</td></tr>`;
      return;
    }
    tb.innerHTML = list.map(u => {
      const used = u.monthly_tokens_used || 0;
      const limit = (u.monthly_token_limit == null) ? null : u.monthly_token_limit;
      const limitText = limit == null ? "2,00,000" : (limit === 0 ? "disabled" : limit.toLocaleString());
      const usageCell = `<div class="usage-cell">
        <div>${used.toLocaleString()} <span class="muted">/ ${limitText}</span></div>
      </div>`;
      const isSelf = (u.email || "").toLowerCase() === myEmail;
      // No editing your OWN token limit (server enforces this too).
      const editLimitBtn = isSelf
        ? ""
        : `<button class="ghost icon-btn" data-act="edit-limit" data-id="${u.id}" data-email="${esc(u.email)}" data-used="${used}" data-limit="${limit == null ? '' : limit}" title="Edit token limit" aria-label="Edit token limit">${window.Icons.svg("edit") || "✎"}</button>`;
      const editRoleBtn = isSelf
        ? ""
        : `<button class="ghost icon-btn" data-act="edit-role" data-id="${u.id}" data-email="${esc(u.email)}" data-role="${esc(u.role)}" data-home="${esc(u.department_code || "")}" data-extras="${esc((u.department_codes || []).join(","))}" title="Change role" aria-label="Change role">${window.Icons.svg("shield") || "⛨"}</button>`;
      const actBtn = isSelf
        ? ""
        : (u.is_active
            ? `<button class="ghost icon-btn danger-ghost" data-act="deactivate" data-id="${u.id}" title="Remove access" aria-label="Remove access">${window.Icons.svg("power")}</button>`
            : `<button class="ghost icon-btn ok-ghost" data-act="activate" data-id="${u.id}" title="Restore access" aria-label="Restore access">${window.Icons.svg("play")}</button>`);
      // Merge the home dept + any extra grants into a single "DEPT" cell.
      const homeCode = (u.department_code || "").toLowerCase();
      const extras = (u.department_codes || []).map(c => (c || "").toLowerCase());
      const allDeptsSeen = new Set();
      const allDepts = [];
      [homeCode, ...extras].forEach(c => {
        if (c && !allDeptsSeen.has(c)) { allDeptsSeen.add(c); allDepts.push(c); }
      });
      const deptCell = allDepts.length
        ? allDepts.map(c => `<span class="dept-chip">${esc(c)}</span>`).join(" ")
        : `<span class="muted">—</span>`;
      const modelLabel = u.preferred_model
        ? ((userSelectableModels.find(m => m.id === u.preferred_model) || {}).label || "Custom")
        : "Default";
      const editModelBtn = isSelf
        ? ""
        : `<button class="ghost icon-btn" data-act="edit-model" data-id="${u.id}" data-email="${esc(u.email)}" data-model="${esc(u.preferred_model || "")}" title="Change model" aria-label="Change model">${window.Icons.svg("sparkles") || "⚙"}</button>`;
      return `
      <tr data-id="${u.id}">
        <td>${esc(u.email)}</td>
        <td>${esc(u.full_name || "")}</td>
        <td>${u.role}</td>
        <td>${deptCell}</td>
        <td><span class="dept-chip">${esc(modelLabel)}</span></td>
        <td>${usageCell}</td>
        <td>${u.is_active ? '<span class="status-yes">'+window.Icons.svg("check")+'</span>' : '<span class="status-no">'+window.Icons.svg("x")+'</span>'}</td>
        <td>${u.last_login_at ? new Date(u.last_login_at).toLocaleString() : "—"}</td>
        <td class="col-actions"><div class="row-actions">${editModelBtn}${editLimitBtn}${editRoleBtn}${actBtn}</div></td>
      </tr>`;
    }).join("");

    tb.querySelectorAll("button[data-act]").forEach(b => {
      b.onclick = (e) => {
        const id = e.currentTarget.dataset.id;
        const act = e.currentTarget.dataset.act;
        if (act === "edit-model") {
          openModelEditor({
            id,
            email: e.currentTarget.dataset.email,
            model: e.currentTarget.dataset.model,
          });
          return;
        }
        if (act === "edit-limit") {
          openTokenLimitEditor({
            id,
            email: e.currentTarget.dataset.email,
            used: parseInt(e.currentTarget.dataset.used || "0", 10),
            limit: e.currentTarget.dataset.limit,
          });
          return;
        }
        if (act === "edit-role") {
          openRoleEditor({
            id,
            email: e.currentTarget.dataset.email,
            role: e.currentTarget.dataset.role,
            home: e.currentTarget.dataset.home,
            extras: e.currentTarget.dataset.extras,
          });
          return;
        }
        if (act === "deactivate") {
          confirmModal({
            title: "Remove access?",
            body: "The user will be signed out and won't be able to log in until access is restored. Their data is preserved.",
            confirmLabel: "Remove access",
            danger: true,
            onConfirm: async () => {
              try {
                await OA.post(`/admin/users/${id}/deactivate`, {});
                OA.toast("Access removed");
                loadUsers();
              } catch (e) {
                OA.toast(e.message || "Couldn't deactivate", "error");
              }
            },
          });
        } else if (act === "activate") {
          (async () => {
            try {
              await OA.post(`/admin/users/${id}/activate`, {});
              OA.toast("Access restored");
              loadUsers();
            } catch (e) {
              OA.toast(e.message || "Couldn't activate", "error");
            }
          })();
        }
      };
    });
  }
  // -- Department picker: single-select OR multi-select chips ---------------
  const deptPicker = {
    departments: [],
    selected: new Set(),
    bound: false,
    init() {
      if (this.bound) return;
      this.bound = true;
      $("uDeptMultiTrigger").addEventListener("click", (e) => {
        e.stopPropagation();
        this.toggleMenu();
      });
      $("uDeptMultiClose").addEventListener("click", () => this.closeMenu());
      document.addEventListener("click", (e) => {
        if (!e.target.closest("#uDeptMulti")) this.closeMenu();
      });
    },
    setDepartments(list) {
      this.departments = list.filter(d => d.is_active);
      this.renderList();
      this.renderChips();
    },
    setMode(role) {
      const isSuper = role === "SUPERADMIN";
      const isMulti = role === "CROSSADMIN";
      // SUPERADMIN ⇒ "All departments" (no picker). CROSSADMIN ⇒ multi.
      // Everyone else ⇒ single-select.
      $("uDept").classList.toggle("hidden", isMulti || isSuper);
      $("uDeptMulti").classList.toggle("hidden", !isMulti);
      const allEl = $("uDeptAll");
      if (allEl) allEl.classList.toggle("hidden", !isSuper);
      if (!isMulti) this.selected.clear();
      else this.renderChips();
    },
    toggleMenu() {
      const menu = $("uDeptMultiMenu");
      const open = menu.classList.toggle("open");
      $("uDeptMultiTrigger").setAttribute("aria-expanded", String(open));
      $("uDeptMultiChevron").innerHTML = window.Icons.svg(open ? "chevronUp" : "chevronDown");
    },
    closeMenu() {
      const menu = $("uDeptMultiMenu");
      if (menu.classList.contains("open")) {
        menu.classList.remove("open");
        $("uDeptMultiTrigger").setAttribute("aria-expanded", "false");
        $("uDeptMultiChevron").innerHTML = window.Icons.svg("chevronDown");
      }
    },
    toggle(code) {
      if (this.selected.has(code)) this.selected.delete(code);
      else this.selected.add(code);
      this.renderList();
      this.renderChips();
      this.updateCount();
    },
    updateCount() {
      const n = this.selected.size;
      $("uDeptMultiCount").textContent = `${n} selected`;
    },
    renderList() {
      const ul = $("uDeptMultiList");
      ul.innerHTML = this.departments.map(d => {
        const checked = this.selected.has(d.code);
        return `
          <li class="dept-picker-item ${checked ? "checked" : ""}" data-code="${esc(d.code)}">
            <span class="dept-picker-box">${checked ? window.Icons.svg("check") : ""}</span>
            <span class="dept-picker-name">${esc(d.name)}</span>
            <span class="dept-picker-code muted">${esc(d.code)}</span>
          </li>`;
      }).join("");
      ul.querySelectorAll("li").forEach(li => {
        li.onclick = (e) => {
          e.stopPropagation();
          this.toggle(li.dataset.code);
        };
      });
      this.updateCount();
    },
    renderChips() {
      const box = $("uDeptMultiChips");
      if (!this.selected.size) {
        box.innerHTML = `<span class="dept-picker-placeholder">Select departments…</span>`;
        return;
      }
      const byCode = Object.fromEntries(this.departments.map(d => [d.code, d]));
      box.innerHTML = Array.from(this.selected).map(code => {
        const d = byCode[code] || { name: code, code };
        return `
          <span class="dept-picker-chip" data-code="${esc(code)}">
            ${esc(d.name)}
            <button type="button" class="dept-picker-chip-x" data-code="${esc(code)}" aria-label="Remove">${window.Icons.svg("x")}</button>
          </span>`;
      }).join("");
      box.querySelectorAll(".dept-picker-chip-x").forEach(b => {
        b.onclick = (e) => {
          e.stopPropagation();
          this.toggle(b.dataset.code);
        };
      });
    },
    clear() {
      this.selected.clear();
      this.renderList();
      this.renderChips();
    },
    getCodes() {
      return Array.from(this.selected);
    },
  };

  deptPicker.init();
  $("uDeptMultiChevron").innerHTML = window.Icons.svg("chevronDown");

  // Constrain the role <select> by what THIS admin is allowed to create.
  // Backend enforces the same rule; we hide the impossible options so
  // the user doesn't see a confusing 403.
  (function constrainRoleOptions() {
    const roleSel = $("uRole");
    if (!roleSel) return;
    const ROLE_RANK = { USER: 0, ADMIN: 1, CROSSADMIN: 2, SUPERADMIN: 3 };
    const myRank = ROLE_RANK[role] ?? -1;
    Array.from(roleSel.options).forEach(opt => {
      const r = opt.value;
      // SUPERADMIN can pick any role. Otherwise: strictly below own rank.
      const allowed = (role === "SUPERADMIN") || (ROLE_RANK[r] < myRank);
      // ADMIN can't grant CROSSADMIN (would be useless anyway — only one
      // dept available to them).
      const meaningful = (r !== "CROSSADMIN") || CAN_GRANT_MULTI;
      opt.hidden = !(allowed && meaningful);
    });
    // Reset to the first visible option.
    const firstVisible = Array.from(roleSel.options).find(o => !o.hidden);
    if (firstVisible) roleSel.value = firstVisible.value;
  })();

  $("uRole").addEventListener("change", () => {
    deptPicker.setMode($("uRole").value);
  });

  $("addUserBtn").onclick = async () => {
    const role = $("uRole").value;
    const email = $("uEmail").value.trim().toLowerCase();
    const fullName = $("uName").value.trim() || null;

    if (!email || !email.includes("@")) {
      OA.toast("Enter a valid email", "error"); return;
    }

    let payload = { email, full_name: fullName, role };
    if (role === "CROSSADMIN") {
      const codes = deptPicker.getCodes();
      if (!codes.length) {
        OA.toast("Pick at least one department for CrossAdmin", "error"); return;
      }
      payload.department_code = codes[0];
      payload.department_codes = codes;
    } else if (role === "SUPERADMIN") {
      // Global access — no home department to assign.
      payload.department_code = null;
    } else {
      payload.department_code = $("uDept").value || null;
    }

    // Monthly token budget: blank => null (use system default);
    // 0 explicitly disables chat for this user.
    const rawLimit = $("uTokenLimit").value.trim();
    if (rawLimit !== "") {
      const n = parseInt(rawLimit, 10);
      if (Number.isNaN(n) || n < 0) {
        OA.toast("Token limit must be a non-negative integer", "error"); return;
      }
      payload.monthly_token_limit = n;
    }

    // Per-user model preference.
    const modelVal = $("uModel") ? $("uModel").value : "";
    if (modelVal) payload.preferred_model = modelVal;

    try {
      await OA.post("/admin/users", payload);
      OA.toast("User created");
      $("uEmail").value = "";
      $("uName").value = "";
      $("uTokenLimit").value = "";
      if ($("uModel")) $("uModel").value = "";
      deptPicker.clear();
      loadUsers();
    } catch (_) {}
  };

  // ==========================================================================
  // KNOWLEDGE BASE — uploads + management
  // ==========================================================================
  const ALLOWED_EXT = [".pdf", ".docx", ".xlsx", ".csv", ".pptx", ".ppt", ".txt"];
  const MAX_BYTES = 50 * 1024 * 1024;
  const PAGE_SIZE = 20;

  const kbState = {
    queued: [],          // {file, status, pct, error}
    page: 1,
    departments: [],
    pendingResync: false,
  };

  async function loadUploadsTab() {
    kbState.departments = await loadDeptOptions("uplDept");
    // Populate the filter dropdown with every department the admin can see.
    const filterSel = $("kbDeptFilter");
    if (filterSel) {
      filterSel.innerHTML = `<option value="">All departments</option>` +
        kbState.departments.map(d => `<option value="${esc(d.code)}">${esc(d.name)} (${esc(d.code)})</option>`).join("");
    }
    resetMetaRows();
    bindUploadHandlers();
    kbState.page = 1;
    await loadKbDocuments();
  }

  // -- Dynamic metadata rows on the upload form ---------------------------
  // The admin can attach any number of {key: value} pairs; each becomes a
  // Bedrock metadata attribute and a chat filter facet.
  function addMetaRow(key = "", value = "") {
    const wrap = $("uplMetaRows");
    if (!wrap) return;
    const row = document.createElement("div");
    row.className = "meta-row";
    row.innerHTML = `
      <input class="meta-key" placeholder="key (e.g. region)" value="${esc(key)}" />
      <span class="meta-eq">=</span>
      <input class="meta-val" placeholder="value (e.g. india, russia)" value="${esc(value)}" />
      <button type="button" class="ghost icon-btn meta-rm" title="Remove" aria-label="Remove">${window.Icons.svg("x")}</button>`;
    row.querySelector(".meta-rm").onclick = () => row.remove();
    wrap.appendChild(row);
  }

  function resetMetaRows() {
    const wrap = $("uplMetaRows");
    if (wrap) wrap.innerHTML = "";
    addMetaRow();   // start with one blank row
  }

  // Collect non-empty rows into a {key: value} object.
  function collectMeta() {
    const out = {};
    document.querySelectorAll("#uplMetaRows .meta-row").forEach(r => {
      const k = (r.querySelector(".meta-key").value || "").trim();
      const v = (r.querySelector(".meta-val").value || "").trim();
      if (k && v) out[k] = v;
    });
    return out;
  }

  function bindUploadHandlers() {
    const drop = $("uplDrop");
    const fileInput = $("uplFile");
    const pickBtn = $("uplPickBtn");

    if (drop._bound) return;
    drop._bound = true;

    pickBtn.onclick = () => fileInput.click();
    drop.onclick = (e) => {
      if (e.target.closest("button")) return;
      fileInput.click();
    };
    ["dragenter", "dragover"].forEach(ev => drop.addEventListener(ev, (e) => {
      e.preventDefault();
      drop.classList.add("active");
    }));
    ["dragleave", "drop"].forEach(ev => drop.addEventListener(ev, (e) => {
      e.preventDefault();
      drop.classList.remove("active");
    }));
    drop.addEventListener("drop", (e) => {
      const files = Array.from(e.dataTransfer.files || []);
      addToQueue(files);
    });
    fileInput.addEventListener("change", () => {
      addToQueue(Array.from(fileInput.files || []));
      fileInput.value = "";
    });

    $("uplBtn").onclick = startUploads;
    $("uplClearBtn").onclick = () => {
      kbState.queued = kbState.queued.filter(q => q.status === "done");
      renderQueue();
    };
    $("resyncBtn").onclick = async () => {
      await OA.post("/admin/kb/resync", {});
      OA.toast("KB resync queued");
    };
    $("kbRefreshBtn").onclick = () => loadKbDocuments();

    // Add another metadata key/value row.
    $("uplMetaAdd").onclick = () => addMetaRow();

    $("kbSearch").addEventListener("input", debounce(() => { kbState.page = 1; loadKbDocuments(); }, 280));
    $("kbDeptFilter").addEventListener("change", () => { kbState.page = 1; loadKbDocuments(); });
    $("kbStatusFilter").addEventListener("change", () => { kbState.page = 1; loadKbDocuments(); });
    $("kbPrevBtn").onclick = () => { if (kbState.page > 1) { kbState.page--; loadKbDocuments(); } };
    $("kbNextBtn").onclick = () => { kbState.page++; loadKbDocuments(); };
  }

  function addToQueue(files) {
    const accepted = [];
    files.forEach(f => {
      const lower = f.name.toLowerCase();
      const ext = lower.slice(lower.lastIndexOf("."));
      if (!ALLOWED_EXT.includes(ext)) {
        accepted.push({ file: f, status: "error", pct: 0, error: "Unsupported file type", meta: [], metaOpen: false });
        return;
      }
      if (f.size > MAX_BYTES) {
        accepted.push({ file: f, status: "error", pct: 0, error: "File >50 MB", meta: [], metaOpen: false });
        return;
      }
      // Dedup against already-queued (by name + size).
      const dup = kbState.queued.some(q => q.file.name === f.name && q.file.size === f.size);
      if (dup) return;
      accepted.push({
        file: f, status: "queued", pct: 0,
        // Per-file metadata rows: [{key, value}]. Independent of the
        // shared "default metadata" block; on upload the two are merged
        // with per-file overriding default for shared keys.
        meta: [],
        metaOpen: false,
      });
    });
    kbState.queued = kbState.queued.concat(accepted);
    renderQueue();
  }

  function renderQueue() {
    const ul = $("uplQueue");
    if (!kbState.queued.length) {
      ul.innerHTML = "";
      $("uplActions").style.display = "none";
      return;
    }
    $("uplActions").style.display = "flex";
    ul.innerHTML = "";
    kbState.queued.forEach((q, i) => {
      ul.appendChild(renderQueueItem(q, i));
    });
  }

  function renderQueueItem(q, i) {
    const li = document.createElement("li");
    const cls = q.status === "error" ? "err"
              : q.status === "done"  ? "ok"
              : q.status === "uploading" ? "up"
              : "";
    li.className = `kb-q ${cls}`;
    li.dataset.i = String(i);

    const metaCount = (q.meta || []).filter(m => m.key && m.value).length;
    const metaBadge = metaCount
      ? `<span class="kb-q-meta-badge">${metaCount} field${metaCount === 1 ? "" : "s"}</span>`
      : "";
    const locked = (q.status === "uploading" || q.status === "done");
    const metaToggleLabel = q.metaOpen ? "Hide metadata" : "Add metadata";

    li.innerHTML = `
      <div class="kb-q-row">
        <span class="kb-q-icon">${window.Icons.svg(extIcon(q.file.name))}</span>
        <div class="kb-q-body">
          <div class="kb-q-name">${esc(q.file.name)}</div>
          <div class="kb-q-meta muted">
            ${formatBytes(q.file.size)}
            ${q.error ? ' · <span class="err-text">' + esc(q.error) + '</span>' : ""}
            ${q.status === "done" ? ' · Uploaded' : ""}
            ${q.status === "skipped" ? ' · Duplicate (skipped)' : ""}
            ${metaBadge}
          </div>
          <div class="kb-q-bar"><span style="width:${q.pct || 0}%"></span></div>
        </div>
        <button class="ghost icon-btn kb-q-meta-toggle"
                data-act="meta-toggle"
                ${locked ? "disabled" : ""}
                title="${metaToggleLabel}"
                aria-expanded="${q.metaOpen ? "true" : "false"}"
                aria-label="${metaToggleLabel}">
          ${window.Icons.svg("tag")}
        </button>
        <button class="ghost icon-btn" data-act="rm" aria-label="Remove">${window.Icons.svg("x")}</button>
      </div>
      <div class="kb-q-meta-panel${q.metaOpen ? " open" : ""}">
        <div class="kb-q-meta-rows" data-role="meta-rows"></div>
        <button type="button" class="ghost kb-q-meta-add" data-act="meta-add">+ Add field</button>
        <small class="muted kb-q-meta-hint">
          These override the default metadata above for matching keys.
          Use comma-separated values for multi-value tags (e.g. <code>region = india, usa</code>).
        </small>
      </div>
    `;

    // Wire row-level buttons.
    li.querySelector('[data-act="rm"]').onclick = () => {
      if (q.status === "uploading") {
        OA.toast("Can't remove while uploading", "error");
        return;
      }
      kbState.queued.splice(i, 1);
      renderQueue();
    };

    const toggleBtn = li.querySelector('[data-act="meta-toggle"]');
    toggleBtn.onclick = () => {
      q.metaOpen = !q.metaOpen;
      if (q.metaOpen && (!q.meta || !q.meta.length)) {
        // Open with one blank row so the admin sees the input.
        q.meta = [{ key: "", value: "" }];
      }
      renderQueue();
    };

    // Wire per-file metadata rows (only if the panel is open).
    if (q.metaOpen) {
      const rows = li.querySelector('[data-role="meta-rows"]');
      (q.meta || []).forEach((m, mi) => rows.appendChild(renderPerFileMetaRow(q, m, mi)));
      li.querySelector('[data-act="meta-add"]').onclick = () => {
        q.meta = q.meta || [];
        q.meta.push({ key: "", value: "" });
        renderQueue();
      };
    }

    return li;
  }

  function renderPerFileMetaRow(q, m, mi) {
    const row = document.createElement("div");
    row.className = "kb-q-meta-row";
    row.innerHTML = `
      <input class="meta-key" placeholder="key (e.g. region)" value="${esc(m.key || "")}" />
      <span class="meta-eq">=</span>
      <input class="meta-val" placeholder="value (e.g. india, usa)" value="${esc(m.value || "")}" />
      <button type="button" class="ghost icon-btn meta-rm" title="Remove" aria-label="Remove">${window.Icons.svg("x")}</button>
    `;
    const keyEl = row.querySelector(".meta-key");
    const valEl = row.querySelector(".meta-val");
    keyEl.oninput = () => { m.key = keyEl.value; };
    valEl.oninput = () => { m.value = valEl.value; };
    row.querySelector(".meta-rm").onclick = () => {
      q.meta.splice(mi, 1);
      // If the user removed the only row, leave the panel open with no
      // rows; they can hit "+ Add field" to reopen.
      renderQueue();
    };
    return row;
  }

  // Merge the shared defaults from #uplMetaRows with one file's per-file
  // metadata. Per-file keys win on collision. Returns a {key:value} dict
  // suitable for serialising to JSON for the upload endpoint.
  function mergedMetaFor(q, defaults) {
    const out = { ...(defaults || {}) };
    (q.meta || []).forEach(({ key, value }) => {
      const k = (key || "").trim();
      const v = (value || "").trim();
      if (k && v) out[k] = v;
    });
    return out;
  }

  function extIcon(name) {
    const n = name.toLowerCase();
    if (n.endsWith(".pdf"))                return "fileText";
    if (n.endsWith(".docx") || n.endsWith(".txt")) return "fileText";
    if (n.endsWith(".xlsx") || n.endsWith(".csv")) return "file";
    return "file";
  }

  async function startUploads() {
    const dept = $("uplDept").value;
    if (!dept) { OA.toast("Pick a department", "error"); return; }
    const defaults = collectMeta();
    const pending = kbState.queued.filter(q => q.status === "queued");
    if (!pending.length) return;

    $("uplBtn").disabled = true;
    try {
      // Per-file XHR so we can report progress. Each file's metadata
      // is the shared defaults merged with that file's per-file rows
      // (per-file wins on conflict).
      for (const item of pending) {
        item.status = "uploading"; item.pct = 0;
        renderQueue();
        const merged = mergedMetaFor(item, defaults);
        const metaJson = Object.keys(merged).length ? JSON.stringify(merged) : "";
        try {
          await xhrUpload(item, dept, metaJson);
          item.status = "done"; item.pct = 100;
        } catch (e) {
          item.status = "error"; item.error = e.message || "Upload failed";
        }
        renderQueue();
      }
      const ok = kbState.queued.filter(q => q.status === "done").length;
      OA.toast(`${ok} file${ok === 1 ? "" : "s"} uploaded. Click Sync to index them.`);
      // **No auto-sync, no modal prompt.** The dedicated Sync button
      // in the KB card header runs ingestion when the admin is ready.
      await loadKbDocuments();
    } finally {
      $("uplBtn").disabled = false;
    }
  }

  function xhrUpload(item, dept, metaJson) {
    return new Promise((resolve, reject) => {
      const fd = new FormData();
      fd.append("file", item.file);
      fd.append("department_code", dept);
      if (metaJson) fd.append("metadata", metaJson);
      const xhr = new XMLHttpRequest();
      xhr.open("POST", "/api/v1/admin/upload");
      const tok = OA.token();
      if (tok) xhr.setRequestHeader("Authorization", "Bearer " + tok);
      xhr.upload.onprogress = (e) => {
        if (e.lengthComputable) {
          item.pct = Math.round((e.loaded / e.total) * 100);
          renderQueue();
        }
      };
      xhr.onload = () => {
        if (xhr.status >= 200 && xhr.status < 300) {
          resolve(safeJson(xhr.responseText));
        } else {
          let detail = "HTTP " + xhr.status;
          try {
            const data = JSON.parse(xhr.responseText);
            detail = data.detail || detail;
          } catch (_) {}
          reject(new Error(detail));
        }
      };
      xhr.onerror = () => reject(new Error("Network error"));
      xhr.send(fd);
    });
  }
  function safeJson(s) { try { return JSON.parse(s); } catch { return null; } }

  // ----- KB file list -------------------------------------------------------

  async function loadKbDocuments() {
    const q = $("kbSearch") ? $("kbSearch").value.trim() : "";
    const dept = $("kbDeptFilter") ? $("kbDeptFilter").value : "";
    const status = $("kbStatusFilter") ? $("kbStatusFilter").value : "";
    const params = new URLSearchParams();
    if (q) params.set("q", q);
    if (dept) params.set("department_code", dept);
    if (status) params.set("status", status);
    params.set("page", kbState.page);
    params.set("page_size", PAGE_SIZE);

    let resp;
    try {
      resp = await OA.get("/admin/kb/documents?" + params.toString(), { silent: true });
    } catch (e) {
      OA.toast(e.message || "Couldn't load KB documents", "error");
      return;
    }
    const tb = $("kbTable").querySelector("tbody");
    if (!resp || !resp.items.length) {
      tb.innerHTML = `<tr><td colspan="8" class="muted" style="padding:24px 0;text-align:center;">No documents</td></tr>`;
    } else {
      tb.innerHTML = resp.items.map(d => {
        const meta = (d.metadata && typeof d.metadata === "object") ? d.metadata : {};
        const metaKeys = Object.keys(meta);
        // Multi-value tags arrive as arrays (admin entered comma-separated
        // values, e.g. region = ["india","russia","usa"]); render them as a
        // single readable chip joined with ", ".
        const fmtMetaVal = (v) => Array.isArray(v) ? v.join(", ") : String(v == null ? "" : v);
        const metaCell = metaKeys.length
          ? metaKeys.map(k => {
              const vs = fmtMetaVal(meta[k]);
              return `<span class="dept-chip" title="${esc(k)}=${esc(vs)}">${esc(k)}=${esc(vs)}</span>`;
            }).join(" ")
          : `<span class="muted">—</span>`;
        return `
        <tr data-id="${d.id == null ? '' : d.id}" data-key="${esc(d.s3_key || '')}" data-external="${d.external ? '1' : '0'}">
          <td>
            <div class="kb-row-file">
              <span class="kb-row-icon">${window.Icons.svg(extIcon(d.filename || ""))}</span>
              <div>
                <div class="kb-row-name">${esc(d.filename || "")}</div>
                <div class="kb-row-key muted">${esc(d.s3_key || "")}</div>
              </div>
            </div>
          </td>
          <td><code>${esc(d.department_code || "")}</code></td>
          <td class="kb-meta-cell">${metaCell}</td>
          <td>${esc(d.uploader_email || (d.external ? "(external)" : ""))}</td>
          <td>${formatBytes(d.size_bytes || 0)}</td>
          <td title="${esc(d.created_at)}">${new Date(d.created_at).toLocaleString()}</td>
          <td>${statusBadge(d.status, d.external)}</td>
          <td class="col-actions">
            <div class="row-actions">
              <button class="ghost icon-btn accent-ghost" data-act="edit-meta" title="Edit metadata" aria-label="Edit metadata">${window.Icons.svg("edit")}</button>
              <button class="ghost icon-btn danger-ghost" data-act="del" title="Delete" aria-label="Delete">${window.Icons.svg("trash")}</button>
            </div>
          </td>
        </tr>`;
      }).join("");

      // Edit-metadata button → open the KB metadata modal seeded with the
      // file's current tags. External files (uploaded outside the app)
      // have no DB row, so we "adopt" them first (create a DB record),
      // then open the editor.
      tb.querySelectorAll("button[data-act='edit-meta']").forEach(b => {
        b.onclick = async (e) => {
          const tr = e.currentTarget.closest("tr");
          const id = tr.dataset.id;
          const key = tr.dataset.key;
          const external = tr.dataset.external === "1";
          let item = resp.items.find(x => String(x.id) === String(id));

          // External file (id=null) — adopt it into the database first.
          if (external || !id) {
            if (!key) {
              OA.toast("Cannot adopt this file — no S3 key", "error");
              return;
            }
            const btn = e.currentTarget;
            const prev = btn.disabled;
            btn.disabled = true;
            btn.textContent = btn.innerHTML.includes("svg") ? "" : "Adopting…";
            try {
              const adopted = await OA.post(`/admin/kb/adopt-external?s3_key=${encodeURIComponent(key)}`, {});
              OA.toast("File adopted. You can now edit its metadata.");
              item = adopted;
              // Refresh the table so the file is no longer marked external.
              await loadKbDocuments();
            } catch (err) {
              OA.toast(err.message || "Could not adopt file", "error");
            } finally {
              btn.disabled = prev;
              btn.innerHTML = window.Icons.svg("edit");
            }
            return;
          }

          if (!item) return;
          openKbMetaModal(item);
        };
      });

      tb.querySelectorAll("button[data-act=del]").forEach(b => {
        b.onclick = (e) => {
          const tr = e.currentTarget.closest("tr");
          const id = tr.dataset.id;
          const key = tr.dataset.key;
          const external = tr.dataset.external === "1";
          const name = tr.querySelector(".kb-row-name").textContent;
          confirmModal({
            title: "Delete this file?",
            body: `<b>${esc(name)}</b> will be removed from S3. ${external
              ? "This file was uploaded outside the app — there is no soft-delete tombstone."
              : "It will be hidden from the assistant after the next KB sync."} This cannot be undone.`,
            confirmLabel: "Delete file",
            danger: true,
            onConfirm: async () => {
              try {
                if (external || !id) {
                  await OA.del(`/admin/kb/objects?s3_key=${encodeURIComponent(key)}`);
                } else {
                  await OA.del(`/admin/kb/documents/${id}`);
                }
                OA.toast("File deleted. Click Sync when you're done removing files.");
                await loadKbDocuments();
              } catch (e) {
                OA.toast(e.message || "Couldn't delete file", "error");
              }
            },
          });
        };
      });
    }

    const total = (resp && resp.total) || 0;
    const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));
    $("kbPagerInfo").textContent = `Page ${kbState.page} of ${totalPages} · ${total} file${total === 1 ? "" : "s"}`;
    $("kbPrevBtn").disabled = kbState.page <= 1;
    $("kbNextBtn").disabled = kbState.page >= totalPages;
  }

  function statusBadge(s, external = false) {
    if (external) {
      return `<span class="chip warn" title="Uploaded outside the app">External</span>`;
    }
    const map = {
      ACTIVE:           { label: "Active",       cls: "status-yes" },
      INGESTING:        { label: "Ingesting",    cls: "" },
      INGESTION_FAILED: { label: "Failed",       cls: "status-no" },
      DELETED:          { label: "Deleted",      cls: "status-no" },
      EXTERNAL:         { label: "External",     cls: "warn" },
    };
    const cfg = map[s] || { label: s, cls: "" };
    return `<span class="chip ${cfg.cls}">${cfg.label}</span>`;
  }

  // ==========================================================================
  // TICKETS (unchanged behaviour)
  // ==========================================================================
  $("reloadTicketsBtn").onclick = loadTickets;
  async function loadTickets() {
    const s = $("tStatusFilter").value;
    const list = await OA.get("/tickets" + (s ? `?status=${s}` : ""));
    const tb = $("ticketTable").querySelector("tbody");
    tb.innerHTML = list.map(t => `
      <tr data-id="${t.id}">
        <td>#${t.id}</td>
        <td>${esc(t.subject)}</td>
        <td>${t.department_id}</td>
        <td><span class="chip">${t.status}</span></td>
        <td>${t.priority}</td>
        <td>${new Date(t.created_at).toLocaleString()}</td>
        <td><button class="ghost" data-act="open">Open</button></td>
      </tr>`).join("");
    tb.querySelectorAll("button[data-act=open]").forEach(b => {
      b.onclick = (e) => openTicket(+e.target.closest("tr").dataset.id);
    });
  }
  // Close button on the ticket detail card. Bound once on first load
  // since the markup is static.
  (function bindTicketDetailClose() {
    const btn = $("tdCloseBtn");
    if (btn && !btn._bound) {
      btn._bound = true;
      btn.onclick = () => $("ticketDetail").classList.add("hidden");
    }
  })();

  async function openTicket(id) {
    const t = await OA.get(`/tickets/${id}`);
    $("ticketDetail").classList.remove("hidden");
    $("tdSubject").textContent = `#${t.id} · ${t.subject}`;
    $("tdMeta").textContent = `Status: ${t.status} · Priority: ${t.priority} · Dept #${t.department_id}`;
    $("tdQuery").textContent = t.query || "";
    $("tdAnswer").textContent = t.ai_response || "";
    $("tdStatus").value = t.status;
    $("tdNotes").value = t.resolution_notes || "";
    $("tdSaveBtn").onclick = async () => {
      await OA.patch(`/tickets/${id}`, {
        status: $("tdStatus").value,
        resolution_notes: $("tdNotes").value,
      });
      OA.toast("Ticket updated");
      loadTickets();
    };
  }

  // ==========================================================================
  // AUDIT
  // ==========================================================================
  async function loadAudit() {
    const list = await OA.get("/admin/audit");
    const tb = $("auditTable").querySelector("tbody");
    tb.innerHTML = list.map(a => `
      <tr>
        <td>${new Date(a.created_at).toLocaleString()}</td>
        <td>${esc(a.user_email || "")}</td>
        <td>${esc(a.action)}</td>
        <td>${esc(a.resource_type || "")}${a.resource_id ? " · " + esc(a.resource_id) : ""}</td>
        <td><code>${esc(JSON.stringify(a.details || {}))}</code></td>
      </tr>`).join("");
  }

  // ==========================================================================
  // MODAL helpers
  // ==========================================================================
  // ==========================================================================
  // KB metadata editor (modal)
  //
  // Opens with the file's current `metadata` (server returns it as either
  // {key: "value"} or {key: ["v1","v2",...]}). Multi-value tags are
  // re-flattened to "v1, v2, v3" so the user edits a single line per key.
  // On save, the raw values are sent as-is — the backend re-normalises
  // them through normalize_metadata.
  // ==========================================================================
  let _kbMetaCtx = null;   // { docId, filename, rows: [{key, value}] }

  function openKbMetaModal(doc) {
    const root = $("kbMetaRoot");
    if (!root) return;
    const meta = (doc.metadata && typeof doc.metadata === "object") ? doc.metadata : {};
    const flatten = (v) => Array.isArray(v) ? v.join(", ") : (v == null ? "" : String(v));
    const rows = Object.keys(meta).map(k => ({ key: k, value: flatten(meta[k]) }));
    if (!rows.length) rows.push({ key: "", value: "" });

    _kbMetaCtx = { docId: doc.id, filename: doc.filename || "—", rows };
    $("kbmFile").textContent = doc.filename || "—";

    renderKbMetaRows();

    root.classList.remove("hidden");
    const close = () => {
      root.classList.add("hidden");
      _kbMetaCtx = null;
    };
    root.querySelectorAll('[data-act="kbm-cancel"]').forEach(el => el.onclick = close);

    $("kbmAddBtn").onclick = () => {
      if (!_kbMetaCtx) return;
      _kbMetaCtx.rows.push({ key: "", value: "" });
      renderKbMetaRows();
    };

    $("kbmSaveBtn").onclick = async () => {
      if (!_kbMetaCtx) return;
      const out = {};
      _kbMetaCtx.rows.forEach(r => {
        const k = (r.key || "").trim();
        const v = (r.value || "").trim();
        if (k && v) out[k] = v;
      });
      const btn = $("kbmSaveBtn");
      btn.disabled = true;
      const prev = btn.textContent;
      btn.textContent = "Saving…";
      try {
        await OA.patch(`/admin/kb/documents/${_kbMetaCtx.docId}/metadata`, { metadata: out });
        OA.toast("Metadata updated. Click Sync to push it to retrieval.");
        close();
        await loadKbDocuments();
      } catch (e) {
        OA.toast(e.message || "Couldn't update metadata", "error");
      } finally {
        btn.disabled = false;
        btn.textContent = prev;
      }
    };
  }

  function renderKbMetaRows() {
    const wrap = $("kbmRows");
    if (!wrap || !_kbMetaCtx) return;
    wrap.innerHTML = "";
    _kbMetaCtx.rows.forEach((m, idx) => {
      const row = document.createElement("div");
      row.className = "meta-row";
      row.innerHTML = `
        <input class="meta-key" placeholder="key (e.g. region)" value="${esc(m.key || "")}" />
        <span class="meta-eq">=</span>
        <input class="meta-val" placeholder="value (e.g. india, usa)" value="${esc(m.value || "")}" />
        <button type="button" class="ghost icon-btn meta-rm" title="Remove" aria-label="Remove">${window.Icons.svg("x")}</button>
      `;
      const keyEl = row.querySelector(".meta-key");
      const valEl = row.querySelector(".meta-val");
      keyEl.oninput = () => { m.key = keyEl.value; };
      valEl.oninput = () => { m.value = valEl.value; };
      row.querySelector(".meta-rm").onclick = () => {
        if (!_kbMetaCtx) return;
        _kbMetaCtx.rows.splice(idx, 1);
        if (!_kbMetaCtx.rows.length) _kbMetaCtx.rows.push({ key: "", value: "" });
        renderKbMetaRows();
      };
      wrap.appendChild(row);
    });
  }

  function confirmModal({ title, body, confirmLabel = "Confirm", danger = false, onConfirm }) {
    const root = $("modalRoot");
    const btn  = $("modalConfirm");
    $("modalTitle").textContent = title;
    $("modalBody").innerHTML = body;
    btn.textContent = confirmLabel;
    btn.classList.toggle("danger", !!danger);

    root.classList.remove("hidden");
    const close = () => { root.classList.add("hidden"); };
    root.querySelectorAll('[data-act="cancel"]').forEach(el => el.onclick = close);
    btn.onclick = async () => {
      close();
      try { await onConfirm(); } catch (_) {}
    };
  }

  // ==========================================================================
  // Misc helpers
  // ==========================================================================
  $("logoutBtn").onclick = () => { OA.clearToken(); location.href = "/index.html"; };
  $("goChat").onclick = () => location.href = "/chat.html";

  function esc(s) { return (s == null ? "" : String(s)).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c])); }

  function formatBytes(b) {
    if (!b && b !== 0) return "—";
    const u = ["B","KB","MB","GB"];
    let i = 0, n = b;
    while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
    return `${n.toFixed(i === 0 ? 0 : 1)} ${u[i]}`;
  }

  function debounce(fn, ms) {
    let t = null;
    return (...args) => {
      clearTimeout(t);
      t = setTimeout(() => fn(...args), ms);
    };
  }

  // Global keyboard: Escape closes any open modal or the ticket detail.
  // Single listener so we don't leak handlers per modal-open.
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    let closed = false;
    document.querySelectorAll(".modal-root:not(.hidden)").forEach(m => {
      m.classList.add("hidden");
      closed = true;
    });
    const dept = document.querySelector(".dept-picker-menu.open");
    if (dept) {
      dept.classList.remove("open");
      closed = true;
    }
    const td = $("ticketDetail");
    if (!closed && td && !td.classList.contains("hidden")) {
      td.classList.add("hidden");
    }
  });

  // Boot
  activate("overview");
})();
