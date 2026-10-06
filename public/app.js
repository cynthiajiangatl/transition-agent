// ---------- utilities ----------
const esc = (s) =>
  String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const attr = (s) => esc(s).replace(/'/g, "&#39;");
const initials = (name) =>
  (name || "?").split(/\s+/).map((w) => w[0]).slice(0, 2).join("").toUpperCase();
const orgChipClass = (org) => (org === "Microsoft" ? "rel" : "ext");

let state = null;        // current handover data
let editing = false;     // edit mode flag
let dirty = false;       // unsaved changes
let workiqEnabled = true; // mirrors server WORKIQ_ENABLED (from /api/me)

// path helpers: "contacts.3.email", "employee.manager.email", "projects.2.pending.0"
function getByPath(obj, path) {
  return path.split(".").reduce((o, k) => (o == null ? o : o[k]), obj);
}
function setByPath(obj, path, value) {
  const keys = path.split(".");
  const last = keys.pop();
  const target = keys.reduce((o, k) => (o[k] ??= {}), obj);
  target[last] = value;
}

// Mark the list item (or the employee object) that an edit path belongs to as
// user-edited, so a later Work IQ refresh preserves it instead of overwriting it.
function markEdited(path) {
  const keys = String(path).split(".");
  if (keys[0] === "employee") {
    if (state.employee && typeof state.employee === "object") state.employee._edited = true;
    return;
  }
  if (keys.length >= 2 && /^\d+$/.test(keys[1])) {
    const arr = state[keys[0]];
    const item = Array.isArray(arr) ? arr[Number(keys[1])] : null;
    if (item && typeof item === "object") item._edited = true;
  }
}

// ---------- section schema (drives nav) ----------
const SECTIONS = [
  { id: "employee", label: "Role & Org", icon: "👤", count: () => null },
  { id: "contacts", label: "Key Contacts", icon: "👥", count: (d) => d.contacts.length },
  { id: "projects", label: "Active Projects", icon: "📦", count: (d) => d.projects.length },
  { id: "files", label: "Important Files", icon: "📁", count: (d) => d.importantFiles.length },
  { id: "processes", label: "Recurring Processes", icon: "🔁", count: (d) => d.recurringProcesses.length },
  { id: "outstanding", label: "Outstanding Items", icon: "✅", count: (d) => d.outstandingItems.length },
  { id: "access", label: "Access Transfers", icon: "🔑", count: (d) => d.accessTransfers.length },
];

const BLANKS = {
  contacts: { name: "", email: "", org: "", relationship: "", scope: "", relevance: "medium" },
  projects: { name: "", customer: "", role: "", status: "Active", summary: "", pending: [] },
  importantFiles: { name: "", why: "", url: "", sensitivityLabel: "", pii: false },
  recurringProcesses: { name: "", cadence: "", description: "", knowledgeRisk: "medium" },
  outstandingItems: { description: "", suggestedOwner: "", dueDate: "", priority: "medium", source: "" },
  accessTransfers: { system: "", type: "", detail: "", action: "", priority: "medium" },
};

// Ensure a loaded brief has every section present, so the UI (which reads
// e.g. d.contacts.length) never trips over a brief that only has meta/employee.
function normalizeState(d) {
  d = d && typeof d === "object" ? d : {};
  if (!d.employee || typeof d.employee !== "object") d.employee = {};
  if (!d.meta || typeof d.meta !== "object") d.meta = {};
  for (const k of Object.keys(BLANKS)) {
    if (!Array.isArray(d[k])) d[k] = [];
  }
  return d;
}

// ---------- load / save ----------
let currentUser = null; // slug of the loaded brief
let autoRefreshDone = false; // auto-generate a brief at most once per page load

// ---------- auth ----------
async function initAuth() {
  try {
    const me = await (await fetch("/api/me")).json();
    workiqEnabled = me.workiqEnabled !== false;
    if (me.authEnabled && !me.authenticated) {
      window.location.href = "/login";
      return false;
    }
    if (me.user) {
      const badge = document.getElementById("userBadge");
      const logout = document.getElementById("logoutLink");
      if (badge) {
        badge.textContent = me.user.name || me.user.upn || "Signed in";
        badge.style.display = "inline-block";
      }
      if (logout) logout.style.display = "inline-block";
    }
    if (me.isAdmin) {
      const adminLink = document.getElementById("adminLink");
      if (adminLink) adminLink.style.display = "inline-block";
    }
    // Reveal the "Shared with me" link only when something has been shared.
    fetch("/api/shared")
      .then((r) => (r.ok ? r.json() : { shared: [] }))
      .then((d) => {
        if (d.shared && d.shared.length) {
          const link = document.getElementById("sharedLink");
          if (link) link.style.display = "inline-block";
        }
      })
      .catch(() => {});
    return true;
  } catch {
    return true; // don't block the app if /api/me is unreachable
  }
}

async function loadUsers() {
  try {
    const { users } = await (await fetch("/api/users")).json();
    const sel = document.getElementById("userSelect");
    if (users && users.length > 1) {
      sel.innerHTML = users
        .map((u) => `<option value="${u.slug}">${esc(u.displayName)}</option>`)
        .join("");
      sel.style.display = "inline-block";
      sel.onchange = () => load(sel.value);
      if (!currentUser) currentUser = users[0].slug;
      sel.value = currentUser;
    } else {
      sel.style.display = "none";
      if (users && users.length === 1) currentUser = users[0].slug;
    }
  } catch {
    /* no users endpoint / none yet */
  }
}

async function load(userSlug) {
  if (userSlug) currentUser = userSlug;
  await loadUsers();
  const url = currentUser ? `/api/handover?user=${encodeURIComponent(currentUser)}` : "/api/handover";
  const res = await fetch(url);
  if (res.status === 401) {
    window.location.href = "/login";
    return;
  }
  if (!res.ok) {
    // No brief stored for this signed-in user yet. Auto-generate one from Work IQ
    // (once, only on the initial sign-in load) so the dashboard populates on first
    // sign-in without a manual click. Skipped for explicit user selections.
    if (res.status === 404 && !autoRefreshDone && !editing && !userSlug) {
      autoRefreshDone = true;
      document.getElementById("loading").textContent =
        "No handover yet \u2014 generating from Work IQ\u2026 this can take a few minutes.";
      await refreshFromWorkIQ();
      return;
    }
    document.getElementById("loading").textContent =
      "No handover data yet \u2014 click \u201CRefresh\u201D to generate one.";
    return;
  }
  state = normalizeState(await res.json());
  dirty = false;
  render();
}

async function save() {
  const res = await fetch("/api/handover", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(state),
  });
  const out = await res.json();
  if (out.ok) {
    dirty = false;
    editing = false;
    if (out.user) currentUser = out.user;
    if (out.savedAt) state.meta.savedAt = out.savedAt;
    flash("Saved \u2713");
    render();
  } else {
    flash("Save failed: " + (out.error || "unknown"), true);
  }
}

async function refreshFromWorkIQ() {
  if (editing) {
    flash("Finish editing before refreshing", true);
    return;
  }
  if (dirty && !confirm("Refreshing will overwrite unsaved edits. Continue?")) return;
  const btn = document.getElementById("refreshBtn");
  btn.disabled = true;
  const start = await fetch("/api/refresh", { method: "POST" });
  const startOut = await start.json();
  if (!startOut.ok) {
    flash("Refresh failed: " + (startOut.error || "unknown"), true);
    btn.disabled = false;
    return;
  }
  flash("Refreshing\u2026 this can take a few minutes");
  const poll = setInterval(async () => {
    const s = await (await fetch("/api/refresh/status")).json();
    const last = s.log && s.log.length ? s.log[s.log.length - 1] : "";
    btn.textContent = "\u27F3 " + (last || "working\u2026").slice(0, 28);
    if (!s.running) {
      clearInterval(poll);
      btn.disabled = false;
      btn.textContent = "\u27F3 Refresh";
      if (s.error) {
        flash("Refresh error: " + s.error, true);
      } else {
        flash("Refreshed \u2713");
        await load(s.upn || currentUser);
      }
    }
  }, 2000);
}

function exportJson() {
  const blob = new Blob([JSON.stringify(state, null, 2)], { type: "application/json" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `handover-${(state.employee.displayName || "employee").replace(/\s+/g, "-").toLowerCase()}.json`;
  a.click();
  URL.revokeObjectURL(a.href);
}

function flash(msg, isErr) {
  const el = document.getElementById("flash");
  el.textContent = msg;
  el.className = "flash show" + (isErr ? " err" : "");
  setTimeout(() => (el.className = "flash"), 2200);
}

// ---------- field builders ----------
function field(path, value, opts = {}) {
  if (!editing) return esc(value ?? opts.placeholder ?? "");
  const ph = opts.placeholder ? ` placeholder='${attr(opts.placeholder)}'` : "";
  if (opts.type === "textarea")
    return `<textarea class="edit" data-path="${attr(path)}"${ph}>${esc(value ?? "")}</textarea>`;
  if (opts.type === "select") {
    const options = opts.options
      .map((o) => `<option value="${attr(o)}"${o === value ? " selected" : ""}>${esc(o)}</option>`)
      .join("");
    return `<select class="edit" data-path="${attr(path)}">${options}</select>`;
  }
  const t = opts.type === "date" ? "date" : "text";
  return `<input class="edit" type="${t}" data-path="${attr(path)}" value="${attr(value ?? "")}"${ph} />`;
}
const delBtn = (target) =>
  editing ? `<button class="icon-btn del" data-action="del" data-target="${attr(target)}" title="Delete">\u2715</button>` : "";
const addBtn = (target, kind, label) =>
  editing ? `<button class="btn ghost add" data-action="add" data-target="${attr(target)}" data-kind="${kind}">\uFF0B ${label}</button>` : "";

// ---------- render ----------
function render() {
  document.getElementById("generated").textContent =
    "Generated " + new Date(state.meta.generatedAt).toLocaleString() +
    (state.meta.savedAt ? " \u00B7 saved " + new Date(state.meta.savedAt).toLocaleTimeString() : "");

  document.getElementById("sidenav").innerHTML = SECTIONS.map((s) => {
    const c = s.count(state);
    return `<a href="#${s.id}" data-id="${s.id}"><span>${s.icon}</span><span>${s.label}</span>${
      c != null ? `<span class="count">${c}</span>` : ""
    }</a>`;
  }).join("");

  document.getElementById("main").innerHTML = `
    ${heroHtml(state.employee)}
    ${contactsHtml(state.contacts)}
    ${projectsHtml(state.projects)}
    ${filesHtml(state.importantFiles)}
    ${processesHtml(state.recurringProcesses)}
    ${outstandingHtml(state.outstandingItems)}
    ${accessHtml(state.accessTransfers)}
  `;
  document.body.classList.toggle("editing", editing);
  setupScrollSpy();
  updateToolbar();
  updateBackupBar();
  applyBackupConfig();
  loadBackups();
}

function heroHtml(e) {
  const cell = (k, v) => `<div class="cell"><div class="k">${k}</div><div class="v">${v}</div></div>`;
  return `
  <section id="employee">
    <div class="hero">
      <div class="hero-top">
        <div class="avatar">${initials(e.displayName)}</div>
        <div style="flex:1">
          <h2>${field("employee.displayName", e.displayName, { placeholder: "Name" })}</h2>
          <div class="role">${field("employee.jobTitle", e.jobTitle, { placeholder: "Job title" })}</div>
        </div>
      </div>
      <div class="hero-grid">
        ${cell("Role", field("employee.role", e.role))}
        ${cell("Department", field("employee.department", e.department))}
        ${cell("Organization", field("employee.organization", e.organization))}
        ${cell("Manager", field("employee.manager.displayName", e.manager?.displayName))}
        ${cell("Manager email", field("employee.manager.email", e.manager?.email))}
        ${cell("Email", field("employee.email", e.email))}
        ${cell("Office", field("employee.officeLocation", e.officeLocation))}
      </div>
      <div class="depart">${field("employee.departureContext", e.departureContext, { type: "textarea", placeholder: "Departure context / transition notes" })}</div>
    </div>
  </section>`;
}

function sectionHead(icon, title, desc, addTarget, kind, addLabel) {
  return `<div class="sec-head"><div><h3>${icon} ${title}</h3><p class="desc">${desc}</p></div>${
    addTarget ? addBtn(addTarget, kind, addLabel) : ""
  }</div>`;
}

function contactsHtml(contacts) {
  const rows = contacts
    .map(
      (c, i) => `<tr>
      <td>${field(`contacts.${i}.name`, c.name, { placeholder: "Name" })}</td>
      <td>${editing ? field(`contacts.${i}.email`, c.email, { placeholder: "email" }) : `<a class="link" href="mailto:${attr(c.email)}">${esc(c.email)}</a>`}</td>
      <td>${editing ? field(`contacts.${i}.org`, c.org) : `<span class="chip ${orgChipClass(c.org)}">${esc(c.org)}</span>`}</td>
      <td>${field(`contacts.${i}.relationship`, c.relationship)}</td>
      <td class="meta">${field(`contacts.${i}.scope`, c.scope, { placeholder: "context" })}</td>
      <td class="rowtools">${delBtn(`contacts.${i}`)}</td>
    </tr>`
    )
    .join("");
  return `<section id="contacts">
    ${sectionHead("👥", "Key Contacts", "People the employee works with, and how to reach them.", "contacts", "contacts", "Add contact")}
    <div class="card tablecard">
      <table>
        <thead><tr><th>Name</th><th>Email</th><th>Org</th><th>Relationship</th><th>Context</th><th></th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </div>
  </section>`;
}

function projectsHtml(projects) {
  const cards = projects
    .map((p, i) => {
      const pending = (p.pending || [])
        .map(
          (x, j) => `<li>${
            editing
              ? `${field(`projects.${i}.pending.${j}`, x)} ${delBtn(`projects.${i}.pending.${j}`)}`
              : esc(x)
          }</li>`
        )
        .join("");
      return `<div class="card">
      ${delBtn(`projects.${i}`)}
      <h4>${field(`projects.${i}.name`, p.name, { placeholder: "Project name" })}</h4>
      <div class="meta">${field(`projects.${i}.customer`, p.customer, { placeholder: "Customer" })} \u00B7 ${
        editing ? field(`projects.${i}.status`, p.status) : `<span class="chip status">${esc(p.status)}</span>`
      }</div>
      <p><strong>Role:</strong> ${field(`projects.${i}.role`, p.role, { placeholder: "Role" })}</p>
      <p>${field(`projects.${i}.summary`, p.summary, { type: "textarea", placeholder: "Summary" })}</p>
      <div class="meta" style="margin-top:8px"><strong>Pending</strong></div>
      <ul class="pending">${pending}</ul>
      ${editing ? addBtn(`projects.${i}.pending`, "string", "Add pending item") : ""}
    </div>`;
    })
    .join("");
  return `<section id="projects">
    ${sectionHead("📦", "Active Projects", "What the employee is working on — role, status, what's open.", "projects", "projects", "Add project")}
    <div class="cards two">${cards}</div>
  </section>`;
}

function filesHtml(files) {
  const cards = files
    .map((f, i) => {
      // file name: a hyperlink to its stored location when not editing and a URL exists
      const nameHtml = editing
        ? field(`importantFiles.${i}.name`, f.name, { placeholder: "File name" })
        : f.url
        ? `<a class="filelink" href="${attr(f.url)}" target="_blank" rel="noopener" title="Open at its stored location">${esc(f.name)} \u2197</a>`
        : esc(f.name);
      const checkbox =
        !editing && f.url
          ? `<input type="checkbox" class="file-check" data-idx="${i}" title="Select for backup" />`
          : "";
      // PII marker: flagged during collection, or derived live from a "PII" sensitivity label.
      const isPii = f.pii === true || /\bpii\b/i.test(f.sensitivityLabel || "");
      const piiBadge = isPii
        ? `<span class="badge pii" title="Contains PII${
            f.sensitivityLabel ? ` \u2014 sensitivity label: ${attr(f.sensitivityLabel)}` : ""
          }">\u2713 PII</span>`
        : "";
      const labelChip =
        !editing && f.sensitivityLabel
          ? ` <span class="chip label" title="Sensitivity label">\uD83D\uDD12 ${esc(f.sensitivityLabel)}</span>`
          : "";
      return `<div class="card filecard">
      ${delBtn(`importantFiles.${i}`)}
      <h4>${checkbox}${nameHtml}${piiBadge}</h4>
      <p class="meta">${field(`importantFiles.${i}.why`, f.why, { placeholder: "Why it matters" })}${labelChip}</p>
      ${editing ? field(`importantFiles.${i}.url`, f.url, { placeholder: "https://\u2026" }) : ""}
      ${editing ? field(`importantFiles.${i}.sensitivityLabel`, f.sensitivityLabel, { placeholder: "Sensitivity label (e.g. Confidential \\ PII)" }) : ""}
    </div>`;
    })
    .join("");
  const backupBar = editing
    ? ""
    : `<div class="backup-bar">
        <label class="selall"><input type="checkbox" id="fileSelectAll" /> Select all</label>
        <button id="backupBtn" class="btn ghost" disabled>\u2601 Back up selected to Blob Storage</button>
        <span id="backupStatus" class="backup-status"></span>
      </div>`;
  return `<section id="files">
    ${sectionHead("📁", "Important Files", "Documents a successor needs — click a name to open it, or select files to back up.", "importantFiles", "importantFiles", "Add file")}
    ${backupBar}
    <div class="cards two">${cards}</div>
    <div id="backupsPanel" class="backups-panel"></div>
  </section>`;
}

function processesHtml(procs) {
  const risk = ["low", "medium", "high"];
  const cards = procs
    .map(
      (p, i) => `<div class="card">
      ${delBtn(`recurringProcesses.${i}`)}
      <h4>${field(`recurringProcesses.${i}.name`, p.name, { placeholder: "Process name" })} ${
        editing
          ? field(`recurringProcesses.${i}.knowledgeRisk`, p.knowledgeRisk, { type: "select", options: risk })
          : `<span class="badge ${esc(p.knowledgeRisk)}">${esc(p.knowledgeRisk)} risk</span>`
      }</h4>
      <div class="meta">${field(`recurringProcesses.${i}.cadence`, p.cadence, { placeholder: "Cadence" })}</div>
      <p>${field(`recurringProcesses.${i}.description`, p.description, { type: "textarea", placeholder: "Description" })}</p>
    </div>`
    )
    .join("");
  return `<section id="processes">
    ${sectionHead("🔁", "Recurring Processes", "Routines only the employee knows how to run.", "recurringProcesses", "recurringProcesses", "Add process")}
    <div class="cards two">${cards}</div>
  </section>`;
}

function outstandingHtml(items) {
  const pri = ["low", "medium", "high"];
  const rows = items
    .map(
      (it, i) => `<tr>
      <td>${editing ? field(`outstandingItems.${i}.priority`, it.priority, { type: "select", options: pri }) : `<span class="badge ${esc(it.priority || "low")}">${esc(it.priority || "")}</span>`}</td>
      <td>${field(`outstandingItems.${i}.description`, it.description, { placeholder: "Item" })}</td>
      <td>${field(`outstandingItems.${i}.suggestedOwner`, it.suggestedOwner, { placeholder: "Owner" })}</td>
      <td>${field(`outstandingItems.${i}.dueDate`, it.dueDate, { type: "date" })}</td>
      <td class="meta">${field(`outstandingItems.${i}.source`, it.source)}</td>
      <td class="rowtools">${delBtn(`outstandingItems.${i}`)}</td>
    </tr>`
    )
    .join("");
  return `<section id="outstanding">
    ${sectionHead("✅", "Outstanding Items", "Open items to hand off — owner and due date.", "outstandingItems", "outstandingItems", "Add item")}
    <div class="card tablecard">
      <table>
        <thead><tr><th>Priority</th><th>Item</th><th>Suggested owner</th><th>Due</th><th>Source</th><th></th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </div>
  </section>`;
}

function accessHtml(items) {
  const pri = ["low", "medium", "high"];
  const rows = items
    .map(
      (a, i) => `<tr>
      <td>${editing ? field(`accessTransfers.${i}.priority`, a.priority, { type: "select", options: pri }) : `<span class="badge ${esc(a.priority || "low")}">${esc(a.priority || "")}</span>`}</td>
      <td><strong>${field(`accessTransfers.${i}.system`, a.system, { placeholder: "System" })}</strong><div class="meta">${field(`accessTransfers.${i}.detail`, a.detail, { placeholder: "Detail" })}</div></td>
      <td>${editing ? field(`accessTransfers.${i}.type`, a.type) : `<span class="tag-type">${esc(a.type || "")}</span>`}</td>
      <td>${field(`accessTransfers.${i}.action`, a.action, { placeholder: "Action" })}</td>
      <td class="rowtools">${delBtn(`accessTransfers.${i}`)}</td>
    </tr>`
    )
    .join("");
  return `<section id="access">
    ${sectionHead("🔑", "Access & Systems to Transfer", "Subscriptions, repos, sites and groups to reassign or revoke.", "accessTransfers", "accessTransfers", "Add access item")}
    <div class="card tablecard">
      <table>
        <thead><tr><th>Priority</th><th>System</th><th>Type</th><th>Action</th><th></th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </div>
  </section>`;
}

// ---------- edit interactions ----------
function onInput(ev) {
  const el = ev.target;
  if (el.classList && el.classList.contains("file-check")) {
    updateBackupBar();
    return;
  }
  if (el.id === "fileSelectAll") {
    document.querySelectorAll(".file-check").forEach((c) => (c.checked = el.checked));
    updateBackupBar();
    return;
  }
  if (!el.classList || !el.classList.contains("edit")) return;
  setByPath(state, el.dataset.path, el.value);
  markEdited(el.dataset.path);
  dirty = true;
  updateToolbar();
}

function onClick(ev) {
  if (ev.target.id === "backupBtn") {
    doBackup();
    return;
  }
  const btn = ev.target.closest("[data-action]");
  if (!btn) return;
  const { action, target, kind } = btn.dataset;

  if (action === "del") {
    const keys = target.split(".");
    const idx = Number(keys.pop());
    const arr = getByPath(state, keys.join("."));
    if (Array.isArray(arr)) arr.splice(idx, 1);
    dirty = true;
    render();
  } else if (action === "add") {
    const arr = getByPath(state, target);
    if (Array.isArray(arr)) {
      const item = kind === "string" ? "" : JSON.parse(JSON.stringify(BLANKS[kind] || {}));
      if (item && typeof item === "object") item._edited = true;
      arr.push(item);
      markEdited(target);
      dirty = true;
      render();
    }
  }
}

// ---------- blob backup ----------
let backupConfigured = null; // null=unknown, true/false once checked

// Render the list of files already backed up for the loaded subject, with
// per-file download links. Anyone who can read the brief (subject, admin, or a
// granted successor) can download; the server re-checks access on each request.
async function loadBackups() {
  const panel = document.getElementById("backupsPanel");
  if (!panel) return;
  if (!currentUser) {
    panel.innerHTML = "";
    return;
  }
  let data;
  try {
    data = await (await fetch(`/api/backups?user=${encodeURIComponent(currentUser)}`)).json();
  } catch {
    panel.innerHTML = "";
    return;
  }
  if (!data || data.configured === false || data.ok === false || !data.files || !data.files.length) {
    panel.innerHTML = "";
    return;
  }
  const rows = data.files
    .map((f) => {
      const href = `/api/backups/download?user=${encodeURIComponent(currentUser)}&blob=${encodeURIComponent(f.blob)}`;
      const size = f.size != null ? ` <span class="meta">(${fmtBytes(f.size)})</span>` : "";
      return `<li><a class="filelink" href="${attr(href)}">⬇ ${esc(f.name)}</a>${size}</li>`;
    })
    .join("");
  panel.innerHTML = `
    <div class="sec-head" style="margin-top:18px">
      <div><h3>☁ Backed-up files</h3><p class="desc">Copies saved to Blob Storage — download a copy.</p></div>
    </div>
    <div class="card"><ul class="backups-list">${rows}</ul></div>`;
}

function fmtBytes(n) {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

async function applyBackupConfig() {
  const bar = document.querySelector(".backup-bar");
  if (!bar) return;
  if (backupConfigured === null) {
    try {
      const cfg = await (await fetch("/api/backup/config")).json();
      backupConfigured = !!cfg.configured;
    } catch {
      backupConfigured = false;
    }
  }
  const btn = document.getElementById("backupBtn");
  const status = document.getElementById("backupStatus");
  if (!backupConfigured) {
    if (btn) btn.disabled = true;
    if (status && !status.textContent)
      status.textContent = "Blob backup not configured on this deployment (set AZURE_STORAGE_* ).";
  }
}

function selectedFileChecks() {
  return [...document.querySelectorAll(".file-check:checked")];
}

function updateBackupBar() {
  const btn = document.getElementById("backupBtn");
  if (!btn) return;
  const n = selectedFileChecks().length;
  btn.disabled = n === 0 || backupConfigured === false;
  btn.textContent = n ? `\u2601 Back up ${n} file${n > 1 ? "s" : ""} to Blob Storage` : "\u2601 Back up selected to Blob Storage";
}

async function doBackup() {
  const checks = selectedFileChecks();
  if (!checks.length) return;
  const files = checks.map((c) => state.importantFiles[Number(c.dataset.idx)]).filter((f) => f && f.url);
  const btn = document.getElementById("backupBtn");
  const status = document.getElementById("backupStatus");
  btn.disabled = true;
  status.textContent = "Backing up\u2026 (downloads each file via Microsoft Graph, then uploads to Azure Blob)";
  status.className = "backup-status working";
  try {
    const res = await fetch("/api/backup", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ user: (state.meta && state.meta.subjectUpn) || currentUser, files }),
    });
    const out = await res.json();
    if (!res.ok || out.ok === false) {
      status.textContent = "Backup failed: " + (out.error || res.statusText);
      status.className = "backup-status err";
      btn.disabled = false;
      return;
    }
    const failed = (out.results || []).filter((r) => !r.ok);
    status.innerHTML =
      `\u2713 Backed up ${out.backedUp}/${out.total} to container “${esc(out.container)}” (${esc(out.prefix)})` +
      (failed.length ? ` \u2014 ${failed.length} failed: ${esc(failed.map((f) => f.name).join(", "))}` : "");
    status.className = "backup-status ok";
    flash(`Backed up ${out.backedUp} file(s) to Blob Storage \u2713`);
  } catch (e) {
    status.textContent = "Backup error: " + e.message;
    status.className = "backup-status err";
  } finally {
    updateBackupBar();
  }
}

// ---------- toolbar ----------
function updateToolbar() {
  const editBtn = document.getElementById("editBtn");
  editBtn.textContent = editing ? "\u2713 Save changes" : "\u270E Edit";
  editBtn.classList.toggle("active", editing);
  document.getElementById("dirtyDot").style.display = dirty ? "inline-block" : "none";
}

function setupScrollSpy() {
  const links = [...document.querySelectorAll(".sidenav a")];
  const sections = links.map((l) => document.getElementById(l.dataset.id)).filter(Boolean);
  const obs = new IntersectionObserver(
    (entries) => {
      entries.forEach((en) => {
        if (en.isIntersecting) links.forEach((l) => l.classList.toggle("active", l.dataset.id === en.target.id));
      });
    },
    { rootMargin: "-40% 0px -55% 0px" }
  );
  sections.forEach((s) => obs.observe(s));
}

// ---------- wire up ----------
window.addEventListener("DOMContentLoaded", () => {
  const main = document.getElementById("main");
  main.addEventListener("input", onInput);
  main.addEventListener("change", onInput);
  main.addEventListener("click", onClick);

  document.getElementById("editBtn").onclick = async () => {
    if (!editing) {
      editing = true;
      render();
      return;
    }
    // In edit mode the button is "Save changes": persist to Cosmos (which exits
    // edit mode on success) or just leave edit mode if nothing changed.
    if (dirty) {
      await save();
    } else {
      editing = false;
      render();
    }
  };
  document.getElementById("exportBtn").onclick = exportJson;
  document.getElementById("printBtn").onclick = () => window.print();
  document.getElementById("refreshBtn").onclick = refreshFromWorkIQ;

  window.addEventListener("beforeunload", (e) => {
    if (dirty) {
      e.preventDefault();
      e.returnValue = "";
    }
  });

  load().catch((err) => {
    document.getElementById("loading").textContent = "Failed to load: " + err.message;
  });
});

initAuth();
