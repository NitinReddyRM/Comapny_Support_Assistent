/**
 * Login flow: email → OTP → department picker.
 *
 * Single- vs multi-department picker:
 *   - USER, ADMIN: single-select tile UI.
 *   - CROSSADMIN, SUPERADMIN(multi): checkbox tiles + "Continue" button,
 *     submits the full set to /auth/departments.
 *
 * Tokens are stored once department(s) are selected; non-admin users are
 * redirected to /chat.html, admins to /admin.html.
 */
(function () {
  const $ = (id) => document.getElementById(id);
  const err = (msg) => { $("authError").textContent = msg || ""; };

  const stepEmail = $("step-email");
  const stepOtp = $("step-otp");
  const stepDept = $("step-dept");

  // Roles that may select multiple departments at login.
  const MULTI_DEPT_ROLES = new Set(["CROSSADMIN", "SUPERADMIN"]);

  function show(step) {
    [stepEmail, stepOtp, stepDept].forEach(s => s.classList.add("hidden"));
    step.classList.remove("hidden");
    // Widen the shell on the department step so the tile grid has room
    // for ~4 columns; keep email/OTP at the narrow default.
    const shell = document.querySelector(".auth-shell");
    if (shell) shell.classList.toggle("dept-step", step === stepDept);
    err("");
  }

  $("btnSendOtp").addEventListener("click", async () => {
    const email = $("email").value.trim().toLowerCase();
    if (!email || !email.includes("@")) { err("Enter a valid email"); return; }
    try {
      await OA.post("/auth/otp/request", { email });
      $("emailEcho").textContent = email;
      show(stepOtp);
      $("otp").focus();
    } catch (e) {
      err(e.message);
    }
  });

  $("btnBackEmail").addEventListener("click", () => show(stepEmail));

  // Back button on the dept picker — drops the half-authed token, blanks
  // the form, and returns to the email step. Users land here after OTP
  // verify but before picking a dept; if they chose the wrong account,
  // they need a clean way out.
  const btnBackToLogin = $("btnBackToLogin");
  if (btnBackToLogin) {
    btnBackToLogin.addEventListener("click", () => {
      OA.clearToken();
      selected.clear();
      $("otp").value = "";
      show(stepEmail);
      $("email").focus();
    });
  }

  $("btnVerifyOtp").addEventListener("click", async () => {
    const email = $("email").value.trim().toLowerCase();
    const code = $("otp").value.trim();
    if (code.length < 4) { err("Enter the code from your email"); return; }
    try {
      const res = await OA.post("/auth/otp/verify", { email, code });
      OA.setToken(res.access_token);
      OA.setUser(res.user);
      await loadDepartments(res.user);
      show(stepDept);
    } catch (e) {
      err(e.message);
    }
  });

  // Map a department code to the matching key in our SVG icon set.
  const DEPT_ICON_KEY = {
    hr: "hr", finance: "finance", it: "it", legal: "legal",
    operations: "operations", security: "security", procurement: "procurement",
    marketing: "marketing", sales: "sales", engineering: "engineering",
    health: "health",
  };
  const deptIconSvg = (code) =>
    window.Icons.svg(DEPT_ICON_KEY[(code || "").toLowerCase()] || "building");

  // Selected codes (multi-mode only).
  const selected = new Set();

  // Pool of currently-rendered department codes — used by Select all.
  let renderedCodes = [];

  function refreshMultiFoot() {
    const n = selected.size;
    $("deptSelectedCount").textContent = String(n);
    $("btnConfirmDepts").disabled = n === 0;
    const btnAll = $("btnSelectAllDepts");
    if (btnAll) {
      const allChosen = renderedCodes.length > 0 && n === renderedCodes.length;
      btnAll.textContent = allChosen ? "Clear all" : "Select all";
    }
  }

  // Full dept list as returned by the server. We keep it around so the
  // search box can re-render the visible subset without re-fetching.
  let allDepartments = [];
  let isMultiMode = false;

  async function loadDepartments(user) {
    const role = String(user.role || "").toUpperCase();
    const multi = MULTI_DEPT_ROLES.has(role);
    isMultiMode = multi;
    selected.clear();

    if (multi) {
      $("deptStepTitle").textContent = "Choose your departments";
      $("deptStepHelp").textContent =
        "Pick one or more departments. The assistant will answer only from the bases you select.";
      $("deptMultiFoot").classList.remove("hidden");
    } else {
      $("deptStepTitle").textContent = "Choose your department";
      $("deptStepHelp").textContent =
        "The assistant will only answer from that department's knowledge base.";
      $("deptMultiFoot").classList.add("hidden");
    }

    let list = [];
    try {
      list = await OA.get("/auth/departments", { silent: true });
    } catch (_) {
      list = [];
    }
    allDepartments = list;
    const search = $("deptSearch");
    if (search) search.value = "";

    if (!list.length) {
      $("deptList").innerHTML = `<p class="muted">No departments are available for your account. Please contact your administrator.</p>`;
      renderedCodes = [];
      return;
    }
    renderDeptList("");
  }

  function renderDeptList(query) {
    const listEl = $("deptList");
    const empty = $("deptEmpty");
    listEl.innerHTML = "";
    const q = (query || "").trim().toLowerCase();
    const visible = q
      ? allDepartments.filter(d =>
          (d.name || "").toLowerCase().includes(q) ||
          (d.code || "").toLowerCase().includes(q) ||
          (d.description || "").toLowerCase().includes(q))
      : allDepartments;
    renderedCodes = visible.map(d => d.code);
    if (empty) empty.classList.toggle("hidden", visible.length > 0);

    // Standard web form: each row is a <label> wrapping a native
    // radio (single-select) or checkbox (multi-select). Clicking
    // anywhere on the row toggles its control — row-wise selection.
    const inputType = isMultiMode ? "checkbox" : "radio";

    visible.forEach(d => {
      const row = document.createElement("label");
      row.className = "dept-row";
      row.dataset.code = d.code;

      const input = document.createElement("input");
      input.type = inputType;
      input.className = "dept-row-input";
      input.name = "deptChoice";
      input.value = d.code;
      input.checked = isMultiMode && selected.has(d.code);
      row.classList.toggle("checked", input.checked);

      // Sub-line: code, plus the dept description when the server gives us one.
      const sub = d.description
        ? `${escapeHtml(d.code)} · ${escapeHtml(d.description)}`
        : escapeHtml(d.code);
      const body = document.createElement("span");
      body.className = "dept-row-main";
      body.innerHTML = `
        <span class="dept-row-icon">${deptIconSvg(d.code)}</span>
        <span class="dept-row-text">
          <span class="dept-row-name">${escapeHtml(d.name)}</span>
          <span class="dept-row-sub">${sub}</span>
        </span>`;

      row.appendChild(input);
      row.appendChild(body);

      input.addEventListener("change", () => {
        if (isMultiMode) {
          if (input.checked) selected.add(d.code);
          else selected.delete(d.code);
          row.classList.toggle("checked", input.checked);
          refreshMultiFoot();
        } else if (input.checked) {
          listEl.querySelectorAll(".dept-row.checked")
            .forEach(r => r.classList.remove("checked"));
          row.classList.add("checked");
          selectSingle(d.code);
        }
      });

      listEl.appendChild(row);
    });

    if (isMultiMode) refreshMultiFoot();
  }

  // Live filter — works for both single and multi modes.
  const deptSearchInput = $("deptSearch");
  if (deptSearchInput) {
    deptSearchInput.addEventListener("input", (e) => renderDeptList(e.target.value));
  }

  // Select all / Clear all — only present in multi mode; the button is
  // rendered statically in index.html so we can bind once.
  const btnSelectAll = $("btnSelectAllDepts");
  if (btnSelectAll) {
    btnSelectAll.addEventListener("click", () => {
      const allChosen =
        renderedCodes.length > 0 && selected.size === renderedCodes.length;
      if (allChosen) {
        selected.clear();
      } else {
        renderedCodes.forEach(c => selected.add(c));
      }
      // Re-render so row checks + aria reflect the new selection, keeping
      // any active search filter intact.
      renderDeptList($("deptSearch") ? $("deptSearch").value : "");
    });
  }

  $("btnConfirmDepts").addEventListener("click", async () => {
    if (selected.size === 0) return;
    try {
      const res = await OA.post("/auth/departments", {
        department_codes: Array.from(selected),
      });
      OA.setToken(res.access_token);
      OA.setUser(res.user);
      routeAfterLogin(res.user);
    } catch (e) {
      err(e.message);
    }
  });

  async function selectSingle(code) {
    try {
      const res = await OA.post("/auth/department", { department_code: code });
      OA.setToken(res.access_token);
      OA.setUser(res.user);
      routeAfterLogin(res.user);
    } catch (e) {
      err(e.message);
    }
  }

  function routeAfterLogin(user) {
    const role = String(user.role || "").toUpperCase();
    // Hard admin destinations stay on the admin page; other roles land on chat.
    // if (role === "ADMIN" || role === "SUPERADMIN" || role === "CROSSADMIN") {
    //   location.href = "/admin.html";
    // } else {
    location.href = "/chat.html";
    // }
  }

  // Enter-key submits.
  ["email", "otp"].forEach(id => {
    $(id).addEventListener("keydown", (e) => {
      if (e.key !== "Enter") return;
      e.preventDefault();
      (id === "email" ? $("btnSendOtp") : $("btnVerifyOtp")).click();
    });
  });

  // Resume if already authed (and a department has already been picked).
  if (OA.token()) {
    OA.get("/auth/me", { silent: true })
      .then(u => {
        if (u && u.department_code) {
          routeAfterLogin(u);
        }
      })
      .catch(() => OA.clearToken());
  }

  function escapeHtml(s) {
    return (s == null ? "" : String(s)).replace(/[&<>"']/g, c => (
      {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]
    ));
  }
})();
