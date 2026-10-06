// shared.js — read-only viewer for departed-employee data shared with the
// signed-in user (Cosmos handover brief + backed-up files in Blob Storage).

const esc = (s) =>
  String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const attr = (s) => esc(s).replace(/'/g, "&#39;");
const initials = (name) =>
  (name || "?").split(/\s+/).map((w) => w[0]).slice(0, 2).join("").toUpperCase();
const orgChipClass = (org) => (org === "Microsoft" ? "rel" : "ext");

let shared = [];        // [{slug, displayName, upn, grantedBy, grantedAt, expiresAt}]
let current = null;     // selected subject slug
let state = null;       // loaded handover for the selected subject

function flash(msg, isErr) {
  const el = document.getElementById("flash");
  el.textContent = msg;
  el.className = "flash show" + (isErr ? " err" : "");
  setTimeout(() => (el.className = "flash"), 2400);
}

function fmtBytes(n) {
  if (n == null) return "";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

// ---------- auth ----------
async function initAuth() {
  let me;
  try {
    me = await (await fetch("/api/me")).json();
  } catch {
    return true;
  }
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
  return true;
}

// ---------- load ----------
async function loadShared() {
  const res = await fetch("/api/shared");
  if (res.status === 401) {
    window.location.href = "/login";
    return;
  }
  const data = await res.json();
  shared = (data.shared || []).filter((s) => s.exists !== false);
  const sel = document.getElementById("subjectSelect");
  if (!shared.length) {
    sel.style.display = "none";
    document.getElementById("sidenav").innerHTML = "";
    document.getElementById("main").innerHTML = emptyStateHtml();
    return;
  }
  sel.innerHTML = shared
    .map((s) => `<option value="${attr(s.slug)}">${esc(s.displayName)}</option>`)
    .join("");
  sel.style.display = shared.length > 1 ? "inline-block" : "none";
  sel.onchange = () => loadSubject(sel.value);
  if (!current || !shared.some((s) => s.slug === current)) current = shared[0].slug;
  sel.value = current;
  await loadSubject(current);
}

async function loadSubject(slug) {
  current = slug;
  document.getElementById("main").innerHTML = `<div class="loading">Loading…</div>`;
  const res = await fetch(`/api/handover?user=${encodeURIComponent(slug)}`);
  if (res.status === 401) {
    window.location.href = "/login";
    return;
  }
  if (res.status === 403) {
    document.getElementById("main").innerHTML =
      messageHtml("Access to this employee's data is no longer available. The grant may have expired or been revoked.");
    document.getElementById("sidenav").innerHTML = "";
    return;
  }
  if (!res.ok) {
    document.getElementById("main").innerHTML =
      messageHtml("No handover data is available for this employee.");
    document.getElementById("sidenav").innerHTML = "";
    return;
  }
  state = normalize(await res.json());
  render();
  loadBackups();
}

function normalize(d) {
  d = d && typeof d === "object" ? d : {};
  if (!d.employee || typeof d.employee !== "object") d.employee = {};
  if (!d.meta || typeof d.meta !== "object") d.meta = {};
  for (const k of ["contacts", "projects", "importantFiles", "recurringProcesses", "outstandingItems", "accessTransfers"]) {
    if (!Array.isArray(d[k])) d[k] = [];
  }
  return d;
}

// ---------- render ----------
const SECTIONS = [
  { id: "employee", label: "Role & Org", icon: "👤", count: () => null },
  { id: "contacts", label: "Key Contacts", icon: "👥", count: (d) => d.contacts.length },
  { id: "projects", label: "Active Projects", icon: "📦", count: (d) => d.projects.length },
  { id: "files", label: "Important Files", icon: "📁", count: (d) => d.importantFiles.length },
  { id: "processes", label: "Recurring Processes", icon: "🔁", count: (d) => d.recurringProcesses.length },
  { id: "outstanding", label: "Outstanding Items", icon: "✅", count: (d) => d.outstandingItems.length },
  { id: "access", label: "Access Transfers", icon: "🔑", count: (d) => d.accessTransfers.length },
  { id: "backups", label: "Backed-up Files", icon: "☁", count: () => null },
];

function grantNote() {
  const g = shared.find((s) => s.slug === current);
  if (!g) return "";
  const exp = g.expiresAt ? new Date(g.expiresAt).toLocaleDateString() : null;
  const by = g.grantedBy ? ` · shared by ${esc(g.grantedBy)}` : "";
  return `Read-only${by}${exp ? ` · access expires ${exp}` : ""}`;
}

function render() {
  document.getElementById("sidenav").innerHTML = SECTIONS.map((s) => {
    const c = s.count(state);
    return `<a href="#${s.id}" data-id="${s.id}"><span>${s.icon}</span><span>${s.label}</span>${
      c != null ? `<span class="count">${c}</span>` : ""
    }</a>`;
  }).join("");

  document.getElementById("main").innerHTML = `
    ${grantBannerHtml()}
    ${heroHtml(state.employee)}
    ${contactsHtml(state.contacts)}
    ${projectsHtml(state.projects)}
    ${filesHtml(state.importantFiles)}
    ${processesHtml(state.recurringProcesses)}
    ${outstandingHtml(state.outstandingItems)}
    ${accessHtml(state.accessTransfers)}
    <section id="backups">
      ${sectionHead("☁", "Backed-up Files", "Copies of the employee's files saved to Blob Storage — download a copy.")}
      <div id="backupsPanel"><div class="loading">Loading files…</div></div>
    </section>
  `;
  setupScrollSpy();
}

function grantBannerHtml() {
  const note = grantNote();
  return note
    ? `<div class="grant-banner">🔒 ${note}</div>`
    : "";
}

function emptyStateHtml() {
  return `<div class="card empty-state">
    <h2>Nothing shared with you yet</h2>
    <p class="desc">When an administrator grants you access to a departed colleague's handover
    brief and backed-up files, they'll appear here.</p>
  </div>`;
}

function messageHtml(msg) {
  return `<div class="card empty-state"><p class="desc">${esc(msg)}</p></div>`;
}

function sectionHead(icon, title, desc) {
  return `<div class="sec-head"><div><h3>${icon} ${title}</h3><p class="desc">${esc(desc)}</p></div></div>`;
}

function cell(k, v) {
  return `<div class="cell"><div class="k">${k}</div><div class="v">${esc(v || "—")}</div></div>`;
}

function heroHtml(e) {
  return `
  <section id="employee">
    <div class="hero">
      <div class="hero-top">
        <div class="avatar">${initials(e.displayName)}</div>
        <div style="flex:1">
          <h2>${esc(e.displayName || "—")}</h2>
          <div class="role">${esc(e.jobTitle || "")}</div>
        </div>
      </div>
      <div class="hero-grid">
        ${cell("Role", e.role)}
        ${cell("Department", e.department)}
        ${cell("Organization", e.organization)}
        ${cell("Manager", e.manager?.displayName)}
        ${cell("Manager email", e.manager?.email)}
        ${cell("Email", e.email)}
        ${cell("Office", e.officeLocation)}
      </div>
      ${e.departureContext ? `<div class="depart">${esc(e.departureContext)}</div>` : ""}
    </div>
  </section>`;
}

function contactsHtml(contacts) {
  const rows = contacts
    .map(
      (c) => `<tr>
      <td>${esc(c.name)}</td>
      <td>${c.email ? `<a class="link" href="mailto:${attr(c.email)}">${esc(c.email)}</a>` : ""}</td>
      <td>${c.org ? `<span class="chip ${orgChipClass(c.org)}">${esc(c.org)}</span>` : ""}</td>
      <td>${esc(c.relationship)}</td>
      <td class="meta">${esc(c.scope)}</td>
    </tr>`
    )
    .join("");
  return `<section id="contacts">
    ${sectionHead("👥", "Key Contacts", "People the employee works with, and how to reach them.")}
    <div class="card tablecard">
      <table>
        <thead><tr><th>Name</th><th>Email</th><th>Org</th><th>Relationship</th><th>Context</th></tr></thead>
        <tbody>${rows || `<tr><td colspan="5" class="meta">None recorded.</td></tr>`}</tbody>
      </table>
    </div>
  </section>`;
}

function projectsHtml(projects) {
  const cards = projects
    .map((p) => {
      const pending = (p.pending || []).map((x) => `<li>${esc(x)}</li>`).join("");
      return `<div class="card">
      <h4>${esc(p.name || "—")}</h4>
      <div class="meta">${esc(p.customer || "")}${p.status ? ` · <span class="chip status">${esc(p.status)}</span>` : ""}</div>
      <p><strong>Role:</strong> ${esc(p.role || "")}</p>
      <p>${esc(p.summary || "")}</p>
      ${pending ? `<div class="meta" style="margin-top:8px"><strong>Pending</strong></div><ul class="pending">${pending}</ul>` : ""}
    </div>`;
    })
    .join("");
  return `<section id="projects">
    ${sectionHead("📦", "Active Projects", "What the employee is working on — role, status, what's open.")}
    <div class="cards two">${cards || messageCard("No active projects recorded.")}</div>
  </section>`;
}

function filesHtml(files) {
  const cards = files
    .map((f) => {
      const nameHtml = f.url
        ? `<a class="filelink" href="${attr(f.url)}" target="_blank" rel="noopener" title="Open at its stored location">${esc(f.name)} ↗</a>`
        : esc(f.name);
      const isPii = f.pii === true || /\bpii\b/i.test(f.sensitivityLabel || "");
      const piiBadge = isPii ? `<span class="badge pii" title="Contains PII">✓ PII</span>` : "";
      const labelChip = f.sensitivityLabel
        ? ` <span class="chip label" title="Sensitivity label">🔒 ${esc(f.sensitivityLabel)}</span>`
        : "";
      return `<div class="card filecard">
      <h4>${nameHtml}${piiBadge}</h4>
      <p class="meta">${esc(f.why || "")}${labelChip}</p>
    </div>`;
    })
    .join("");
  return `<section id="files">
    ${sectionHead("📁", "Important Files", "Documents a successor needs — click a name to open it at its source.")}
    <div class="cards two">${cards || messageCard("No files recorded.")}</div>
  </section>`;
}

function processesHtml(procs) {
  const cards = procs
    .map(
      (p) => `<div class="card">
      <h4>${esc(p.name || "—")} ${p.knowledgeRisk ? `<span class="badge ${esc(p.knowledgeRisk)}">${esc(p.knowledgeRisk)} risk</span>` : ""}</h4>
      <div class="meta">${esc(p.cadence || "")}</div>
      <p>${esc(p.description || "")}</p>
    </div>`
    )
    .join("");
  return `<section id="processes">
    ${sectionHead("🔁", "Recurring Processes", "Routines only the employee knows how to run.")}
    <div class="cards two">${cards || messageCard("No recurring processes recorded.")}</div>
  </section>`;
}

function outstandingHtml(items) {
  const rows = items
    .map(
      (it) => `<tr>
      <td><span class="badge ${esc(it.priority || "low")}">${esc(it.priority || "")}</span></td>
      <td>${esc(it.description)}</td>
      <td>${esc(it.suggestedOwner)}</td>
      <td>${esc(it.dueDate)}</td>
      <td class="meta">${esc(it.source)}</td>
    </tr>`
    )
    .join("");
  return `<section id="outstanding">
    ${sectionHead("✅", "Outstanding Items", "Open items to hand off — owner and due date.")}
    <div class="card tablecard">
      <table>
        <thead><tr><th>Priority</th><th>Item</th><th>Suggested owner</th><th>Due</th><th>Source</th></tr></thead>
        <tbody>${rows || `<tr><td colspan="5" class="meta">None recorded.</td></tr>`}</tbody>
      </table>
    </div>
  </section>`;
}

function accessHtml(items) {
  const rows = items
    .map(
      (a) => `<tr>
      <td><span class="badge ${esc(a.priority || "low")}">${esc(a.priority || "")}</span></td>
      <td><strong>${esc(a.system)}</strong><div class="meta">${esc(a.detail)}</div></td>
      <td>${a.type ? `<span class="tag-type">${esc(a.type)}</span>` : ""}</td>
      <td>${esc(a.action)}</td>
    </tr>`
    )
    .join("");
  return `<section id="access">
    ${sectionHead("🔑", "Access & Systems to Transfer", "Subscriptions, repos, sites and groups to reassign or revoke.")}
    <div class="card tablecard">
      <table>
        <thead><tr><th>Priority</th><th>System</th><th>Type</th><th>Action</th></tr></thead>
        <tbody>${rows || `<tr><td colspan="4" class="meta">None recorded.</td></tr>`}</tbody>
      </table>
    </div>
  </section>`;
}

function messageCard(msg) {
  return `<div class="card"><p class="meta">${esc(msg)}</p></div>`;
}

// ---------- backed-up files ----------
async function loadBackups() {
  const panel = document.getElementById("backupsPanel");
  if (!panel) return;
  let data;
  try {
    data = await (await fetch(`/api/backups?user=${encodeURIComponent(current)}`)).json();
  } catch {
    panel.innerHTML = messageCard("Could not load backed-up files.");
    return;
  }
  if (!data || data.configured === false) {
    panel.innerHTML = messageCard("File backup is not configured on this deployment.");
    return;
  }
  if (data.ok === false || !data.files || !data.files.length) {
    panel.innerHTML = messageCard("No files have been backed up for this employee.");
    return;
  }
  const rows = data.files
    .map((f) => {
      const href = `/api/backups/download?user=${encodeURIComponent(current)}&blob=${encodeURIComponent(f.blob)}`;
      const size = f.size != null ? ` <span class="meta">(${fmtBytes(f.size)})</span>` : "";
      return `<li><a class="filelink" href="${attr(href)}">⬇ ${esc(f.name)}</a>${size}</li>`;
    })
    .join("");
  panel.innerHTML = `<div class="card"><ul class="backups-list">${rows}</ul></div>`;
}

// ---------- scrollspy ----------
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
window.addEventListener("DOMContentLoaded", async () => {
  document.getElementById("printBtn").onclick = () => window.print();
  if (!(await initAuth())) return;
  try {
    await loadShared();
  } catch (e) {
    document.getElementById("main").innerHTML = messageHtml("Failed to load: " + e.message);
  }
});
