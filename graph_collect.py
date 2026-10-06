"""
graph_collect.py
================
Fallback handover collector that builds a brief directly from **Microsoft Graph**
when the Work IQ resource is not available in the tenant.

It populates the sections Graph can answer factually:
  - employee : /me profile + /me/manager
  - contacts : /me/people (relevant people)
  - importantFiles : /me/insights/used (recently used documents)

The AI-inferred sections (projects, recurring processes, outstanding items,
access transfers) require Work IQ's reasoning and cannot be reproduced from Graph,
so they are preserved from the existing brief: the collector seeds its result from
the previously stored handover and only overlays the sections Graph can refresh,
rather than overwriting the whole document.

This mirrors collect.collect()'s contract: returns (upn, handover_dict).
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
import re
from typing import Optional
import urllib.parse

import config
import storage
from graph_files import (
    _graph_get,
    GraphError,
    sensitivity_label_catalog,
    file_sensitivity_label,
)


def _get(path: str, token: str):
    data, _ = _graph_get(path, token)
    return data


def _items(data: dict) -> list[dict]:
    return data.get("value", []) if isinstance(data, dict) else []


def _quote_path(value: str) -> str:
    return urllib.parse.quote(value, safe="")


def _clean_text(*parts) -> str:
    return " ".join(" ".join(str(part or "").split()) for part in parts).strip()


def _first_date(value: dict | None) -> str:
    if not isinstance(value, dict):
        return ""
    dt = value.get("dateTime") or value.get("date") or ""
    return str(dt)[:10] if dt else ""


def _email_name(person: dict | None) -> str:
    if not isinstance(person, dict):
        return ""
    addr = person.get("emailAddress") or person.get("email") or person
    if isinstance(addr, dict):
        return addr.get("name") or addr.get("address") or ""
    return str(addr or "")


def _normalize_key(value: str) -> str:
    value = value.lower()
    value = re.sub(r"\b(re|fw|fwd):\s*", "", value)
    value = re.sub(r"\[[^\]]+\]", " ", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    words = [w for w in value.split() if w not in _STOP_WORDS]
    return " ".join(words[:8])


def _candidate_name(value: str) -> str:
    value = _clean_text(value)
    value = re.sub(r"^(re|fw|fwd):\s*", "", value, flags=re.I)
    value = re.sub(r"\[[^\]]+\]", "", value).strip()
    value = re.split(r"\s[-|:]\s|\s/\s", value, maxsplit=1)[0].strip()
    value = re.sub(r"\.(docx?|pptx?|xlsx?|pdf|one|msg)$", "", value, flags=re.I)
    return value[:90]


def _is_generic_subject(value: str) -> bool:
    key = _normalize_key(value)
    return not key or key in _GENERIC_SUBJECTS or len(key) < 4


def _priority_from(importance: str = "", due_date: str = "") -> str:
    if str(importance or "").lower() == "high":
        return "high"
    if due_date:
        today = datetime.now(timezone.utc).date()
        try:
            due = datetime.fromisoformat(due_date).date()
            if due <= today + timedelta(days=7):
                return "high"
        except ValueError:
            pass
    return "medium"


def _merge_lists(existing: list, generated: list, key_fields: tuple[str, ...], limit: int) -> list:
    out = []
    seen = set()
    for item in generated + existing:
        if not isinstance(item, dict):
            continue
        key = tuple(_normalize_key(str(item.get(k, ""))) for k in key_fields)
        if not any(key) or key in seen:
            continue
        seen.add(key)
        out.append(item)
        if len(out) >= limit:
            break
    return out


def _safe_graph_list(path: str, token: str, emit, label: str) -> list[dict]:
    try:
        return _items(_get(path, token))
    except Exception as e:  # noqa: BLE001 — best-effort: one bad endpoint must not fail the whole refresh
        emit(f"    - {label} unavailable: {e}")
        return []


def _collect_events(token: str, emit) -> list[dict]:
    start = urllib.parse.quote((datetime.now(timezone.utc) - timedelta(days=60)).isoformat(), safe="")
    end = urllib.parse.quote((datetime.now(timezone.utc) + timedelta(days=90)).isoformat(), safe="")
    path = (
        f"/me/calendarView?startDateTime={start}&endDateTime={end}"
        "&$top=100&$orderby=start/dateTime"
        "&$select=subject,start,end,type,seriesMasterId,recurrence,attendees,organizer,webLink,isCancelled"
    )
    emit("  Graph: reading calendar signals...")
    events = [e for e in _safe_graph_list(path, token, emit, "calendar") if not e.get("isCancelled")]
    emit(f"    + calendar events: {len(events)}")
    return events


def _collect_messages(token: str, emit) -> list[dict]:
    path = (
        "/me/messages?$top=60&$orderby=receivedDateTime%20desc"
        "&$select=subject,receivedDateTime,importance,from,bodyPreview,webLink"
    )
    emit("  Graph: reading recent mail signals...")
    messages = _safe_graph_list(path, token, emit, "mail")
    emit(f"    + mail messages: {len(messages)}")
    return messages


def _collect_tasks(token: str, emit) -> list[dict]:
    emit("  Graph: reading To Do tasks...")
    # /me/todo/lists rejects both $top and $select (RequestBroker--ParseUri);
    # request the bare collection and read the fields off each list.
    lists = _safe_graph_list("/me/todo/lists", token, emit, "To Do lists")
    tasks = []
    for todo_list in lists:
        list_id = todo_list.get("id")
        if not list_id:
            continue
        path = (
            f"/me/todo/lists/{_quote_path(list_id)}/tasks?$top=30"
            "&$select=title,status,importance,dueDateTime,body,linkedResources,lastModifiedDateTime"
        )
        for task in _safe_graph_list(path, token, emit, f"tasks in {todo_list.get('displayName') or 'list'}"):
            task["_listName"] = todo_list.get("displayName") or todo_list.get("wellknownListName") or "To Do"
            tasks.append(task)
    emit(f"    + open task signals: {len(tasks)}")
    return tasks


def _collect_groups(token: str, emit) -> tuple[list[dict], list[dict]]:
    emit("  Graph: reading Teams and group memberships...")
    # /me/joinedTeams does not allow $top ("Query option 'Top' is not allowed").
    teams = _safe_graph_list("/me/joinedTeams?$select=id,displayName,description", token, emit, "joined teams")
    groups = _safe_graph_list(
        "/me/memberOf/microsoft.graph.group?$top=80&$select=id,displayName,description,mail,groupTypes,securityEnabled,mailEnabled",
        token,
        emit,
        "group memberships",
    )
    emit(f"    + teams: {len(teams)}, groups: {len(groups)}")
    return teams, groups


# Insights and "recent" often surface site chrome rather than real documents:
# template images, page assets, and site roots. These filters keep the list to
# documents a successor would actually open.
_ASSET_PATH_RE = re.compile(r"/(SiteAssets|_SiteTemplates|SitePages|Style Library|_catalogs|Forms)/", re.I)
_NONDOC_EXT_RE = re.compile(r"\.(jpe?g|png|gif|bmp|svg|ico|webp|tiff?|css|js|json|xml|master|aspx|html?|thmx)(?:\?|$)", re.I)
_NONDOC_VIZ_TYPES = {"image", "spsite", "folder", "web"}


def _is_document_file(name: str, url: str, viz_type: str = "") -> bool:
    """Keep documents a successor needs; drop images, template/page assets, folders,
    and site roots that insights/recent commonly return as noise."""
    u = url or ""
    if not u:
        return False
    if _ASSET_PATH_RE.search(u) or _NONDOC_EXT_RE.search(u):
        return False
    if (viz_type or "").strip().lower() in _NONDOC_VIZ_TYPES:
        return False
    return True


def _collect_onedrive_files(token: str, emit, max_scanned: int = 300, limit: int | None = None) -> list[dict]:
    """Return the user's OneDrive files newest-first, walking subfolders (bounded).
    Usage/insights signals miss freshly created or edited files, and the root often
    only holds folders, so we enumerate the drive directly."""
    limit = limit or config.MAX_IMPORTANT_FILES
    found: list[dict] = []
    queue = ["root"]
    scanned = 0
    while queue and scanned < max_scanned:
        node = queue.pop(0)
        base = "/me/drive/root/children" if node == "root" else f"/me/drive/items/{node}/children"
        path = base + "?$top=200&$select=id,name,webUrl,file,folder,lastModifiedDateTime"
        for it in _safe_graph_list(path, token, emit, "OneDrive"):
            scanned += 1
            if "folder" in it:
                if it.get("id"):
                    queue.append(it["id"])
                continue
            url = it.get("webUrl")
            if not url:
                continue
            found.append(
                {
                    "name": it.get("name") or "file",
                    "url": url,
                    "mod": it.get("lastModifiedDateTime") or "",
                }
            )
    found.sort(key=lambda f: f["mod"], reverse=True)
    if found:
        emit(f"    + OneDrive files found: {len(found)}")
    return found[:limit]


def _collect_important_files(token: str, emit, annotate: bool = True) -> list[dict]:
    emit("  Graph: gathering important files...")
    files = []
    skipped = 0

    # 1) The user's actual OneDrive files across the whole drive, newest first.
    #    Walks subfolders (bounded) so files in folders like "Documents" still show
    #    — usage/insights signals alone miss freshly created or edited files.
    for it in _collect_onedrive_files(token, emit):
        files.append({"name": it["name"], "why": "Recent OneDrive file", "url": it["url"]})

    # 2) Recently used across own + shared drives (kept, but filtered).
    for it in _safe_graph_list(
        "/me/drive/recent?$top=25&$select=name,webUrl,file,folder,lastModifiedDateTime",
        token, emit, "recent files",
    ):
        url = it.get("webUrl")
        name = it.get("name") or "file"
        if not url or "folder" in it:
            continue
        if not _is_document_file(name, url):
            skipped += 1
            continue
        files.append({"name": name, "why": "Recently used file", "url": url})

    # 3) Insights (SharePoint-heavy) — filtered to drop site chrome/assets.
    for label, path in (
        ("Recently used", "/me/insights/used?$top=25"),
        ("Trending", "/me/insights/trending?$top=25"),
    ):
        for it in _safe_graph_list(path, token, emit, label.lower()):
            viz = it.get("resourceVisualization") or {}
            ref = it.get("resourceReference") or {}
            url = ref.get("webUrl")
            name = viz.get("title") or "file"
            if not url:
                continue
            if not _is_document_file(name, url, viz.get("type")):
                skipped += 1
                continue
            files.append({"name": name, "why": label, "url": url})

    if skipped:
        emit(f"    (skipped {skipped} non-document/asset items)")

    out = _merge_lists([], files, ("url", "name"), config.MAX_IMPORTANT_FILES)
    if annotate:
        _annotate_sensitivity(out, token, emit)
    emit(f"    + files: {len(out)}")
    return out


def _annotate_sensitivity(files: list[dict], token: str, emit) -> None:
    """Tag each important file with its Microsoft Purview sensitivity label display
    name and a ``pii`` flag (mirroring the Work IQ path). Best-effort: resolving a
    label ID to its name needs the ``InformationProtectionPolicy.Read`` permission,
    and extracting a file's labels needs ``Files.Read.All``. When either is missing
    or a file can't be read, that file is simply left unlabelled."""
    from collect import _flag_pii  # single source of truth for PII detection

    if not files or not config.GRAPH_DETECT_PII:
        _flag_pii(files)
        return

    try:
        catalog = sensitivity_label_catalog(token, emit)
    except GraphError as e:
        emit(f"    - sensitivity labels unavailable: {e}")
        catalog = {}
    if not catalog:
        emit("    - no sensitivity-label catalog returned; skipping PII detection")
        _flag_pii(files)
        return

    labelled = 0
    for f in files:
        url = f.get("url")
        if not url:
            continue
        try:
            name = file_sensitivity_label(url, token, catalog)
        except GraphError:
            name = ""  # locked/unsupported file or transient error — leave unlabelled
        if name:
            f["sensitivityLabel"] = name
            labelled += 1

    _flag_pii(files)
    flagged = sum(1 for f in files if f.get("pii"))
    emit(f"    + sensitivity labels: {labelled} labelled, {flagged} flagged PII")


def _generate_projects(events: list[dict], messages: list[dict], files: list[dict], tasks: list[dict], employee: dict) -> list[dict]:
    buckets = defaultdict(lambda: {"meetings": 0, "messages": 0, "files": 0, "tasks": [], "name": ""})

    def add(source: str, title: str):
        if _is_generic_subject(title):
            return
        name = _candidate_name(title)
        key = _normalize_key(name)
        if not key:
            return
        buckets[key][source] += 1
        buckets[key]["name"] = buckets[key]["name"] or name

    for event in events:
        add("meetings", event.get("subject") or "")
    for message in messages:
        add("messages", message.get("subject") or "")
    for file in files:
        add("files", file.get("name") or "")

    for task in tasks:
        title = task.get("title") or ""
        task_key = _normalize_key(title)
        for key, bucket in buckets.items():
            if key and (key in task_key or task_key in key):
                bucket["tasks"].append(title)

    scored = sorted(
        buckets.values(),
        key=lambda b: (b["meetings"] * 3) + (b["messages"] * 2) + b["files"] + len(b["tasks"]),
        reverse=True,
    )
    projects = []
    for bucket in scored:
        score = (bucket["meetings"] * 3) + (bucket["messages"] * 2) + bucket["files"] + len(bucket["tasks"])
        if score < 3 or not bucket["name"]:
            continue
        signals = []
        if bucket["meetings"]:
            signals.append(f"{bucket['meetings']} calendar events")
        if bucket["messages"]:
            signals.append(f"{bucket['messages']} mail messages")
        if bucket["files"]:
            signals.append(f"{bucket['files']} files")
        projects.append(
            {
                "name": bucket["name"],
                "customer": employee.get("organization") or "",
                "role": employee.get("jobTitle") or "Contributor",
                "status": "Active",
                "summary": "Inferred from Microsoft Graph signals: " + ", ".join(signals) + ".",
                "pending": list(dict.fromkeys(bucket["tasks"]))[:5],
            }
        )
        if len(projects) >= config.MAX_PROJECTS:
            break
    return projects


def _recurrence_cadence(event: dict) -> str:
    recurrence = event.get("recurrence") or {}
    pattern = recurrence.get("pattern") or {}
    rtype = str(pattern.get("type") or "").lower()
    interval = pattern.get("interval") or 1
    if "daily" in rtype:
        return "Daily" if interval == 1 else f"Every {interval} days"
    if "weekly" in rtype:
        return "Weekly" if interval == 1 else f"Every {interval} weeks"
    if "monthly" in rtype:
        return "Monthly" if interval == 1 else f"Every {interval} months"
    return "Recurring meeting"


def _generate_recurring_processes(events: list[dict]) -> list[dict]:
    buckets = defaultdict(lambda: {"count": 0, "name": "", "cadence": "", "attendees": 0})
    for event in events:
        subject = event.get("subject") or ""
        if _is_generic_subject(subject):
            continue
        recurring = event.get("recurrence") or event.get("seriesMasterId") or event.get("type") in {"seriesMaster", "occurrence"}
        key = _normalize_key(subject)
        buckets[key]["count"] += 1
        buckets[key]["name"] = buckets[key]["name"] or _candidate_name(subject)
        buckets[key]["attendees"] = max(buckets[key]["attendees"], len(event.get("attendees") or []))
        if recurring:
            buckets[key]["cadence"] = buckets[key]["cadence"] or _recurrence_cadence(event)

    processes = []
    for bucket in sorted(buckets.values(), key=lambda b: b["count"], reverse=True):
        if bucket["count"] < 2 and not bucket["cadence"]:
            continue
        risk = "high" if bucket["count"] >= 4 and bucket["attendees"] <= 6 else "medium"
        processes.append(
            {
                "name": bucket["name"],
                "cadence": bucket["cadence"] or "Repeated meetings",
                "description": f"Inferred from {bucket['count']} related calendar events in Microsoft Graph.",
                "knowledgeRisk": risk,
            }
        )
        if len(processes) >= config.MAX_RECURRING_PROCESSES:
            break
    return processes


def _is_open_task(task: dict) -> bool:
    return str(task.get("status") or "").lower() not in {"completed", "cancelled"}


def _message_needs_action(message: dict) -> bool:
    text = _clean_text(message.get("subject"), message.get("bodyPreview")).lower()
    if str(message.get("importance") or "").lower() == "high":
        return True
    return any(term in text for term in _ACTION_TERMS)


def _generate_outstanding_items(tasks: list[dict], messages: list[dict], identity: dict) -> list[dict]:
    items = []
    owner = identity.get("name") or identity.get("upn") or ""
    for task in tasks:
        if not _is_open_task(task):
            continue
        due = _first_date(task.get("dueDateTime"))
        title = _clean_text(task.get("title"))
        if title:
            items.append(
                {
                    "description": title,
                    "suggestedOwner": owner,
                    "dueDate": due,
                    "priority": _priority_from(task.get("importance"), due),
                    "source": f"Microsoft To Do: {task.get('_listName') or 'task list'}",
                }
            )
    for message in messages:
        if not _message_needs_action(message):
            continue
        subject = _candidate_name(message.get("subject") or "")
        if subject:
            sender = _email_name((message.get("from") or {}).get("emailAddress"))
            items.append(
                {
                    "description": subject,
                    "suggestedOwner": owner,
                    "dueDate": "",
                    "priority": _priority_from(message.get("importance")),
                    "source": f"Recent email{f' from {sender}' if sender else ''}",
                }
            )
    return _merge_lists([], items, ("description", "source"), config.MAX_OUTSTANDING_ITEMS)


def _site_from_url(url: str) -> str:
    match = re.search(r"https://[^/]+/(?:sites|teams)/([^/?#]+)", url or "", flags=re.I)
    return urllib.parse.unquote(match.group(1)) if match else ""


def _access_type(group: dict) -> str:
    name = _clean_text(group.get("displayName"), group.get("description")).lower()
    if "azure" in name or "subscription" in name:
        return "Azure"
    if "github" in name or "repo" in name or "devops" in name:
        return "Source control"
    if "sharepoint" in name or "site" in name:
        return "SharePoint"
    if "Unified" in group.get("groupTypes", []) or group.get("mailEnabled"):
        return "Teams"
    return "Identity"


def _access_priority(text: str) -> str:
    text = text.lower()
    return "high" if any(term in text for term in _ACCESS_HIGH_TERMS) else "medium"


def _generate_access_transfers(teams: list[dict], groups: list[dict], files: list[dict], messages: list[dict]) -> list[dict]:
    items = []
    for team in teams:
        name = _clean_text(team.get("displayName"))
        if name:
            items.append(
                {
                    "system": name,
                    "type": "Teams",
                    "detail": _clean_text(team.get("description")) or "Direct member of Microsoft Team",
                    "action": "Confirm replacement owner/member and remove access if no longer needed.",
                    "priority": _access_priority(name),
                }
            )
    for group in groups:
        name = _clean_text(group.get("displayName"))
        if name:
            atype = _access_type(group)
            items.append(
                {
                    "system": name,
                    "type": atype,
                    "detail": group.get("mail") or _clean_text(group.get("description")) or "Microsoft 365 group membership",
                    "action": "Review membership and transfer ownership/responsibility before offboarding.",
                    "priority": _access_priority(name),
                }
            )

    for file in files:
        site = _site_from_url(file.get("url") or "")
        if site:
            items.append(
                {
                    "system": site,
                    "type": "SharePoint",
                    "detail": f"Referenced by file: {file.get('name') or 'document'}",
                    "action": "Confirm site access and document ownership handoff.",
                    "priority": "medium",
                }
            )

    for message in messages:
        text = _clean_text(message.get("subject"), message.get("bodyPreview"))
        lowered = text.lower()
        for system, atype in (("Azure", "Azure"), ("GitHub", "Source control"), ("Azure DevOps", "Source control")):
            if system.lower() in lowered:
                items.append(
                    {
                        "system": system,
                        "type": atype,
                        "detail": _candidate_name(message.get("subject") or system),
                        "action": "Review referenced access, ownership, and credentials before offboarding.",
                        "priority": _access_priority(text),
                    }
                )
    return _merge_lists([], items, ("system", "type"), config.MAX_ACCESS_TRANSFERS)


_STOP_WORDS = {
    "a", "an", "and", "are", "as", "at", "by", "for", "from", "in", "is", "it", "of",
    "on", "or", "re", "the", "to", "with", "your", "you", "our", "my", "weekly", "monthly",
    "meeting", "meet", "sync", "call", "catch", "up", "review", "status", "update",
}

_GENERIC_SUBJECTS = {
    "one one", "standup", "daily standup", "weekly sync", "monthly sync", "team meeting",
    "office hours", "lunch", "ooo", "out office", "focus time",
}

_ACTION_TERMS = (
    "action required", "follow up", "todo", "to do", "please review", "please send",
    "can you", "could you", "need you", "needs", "due", "by eod", "before friday",
    "pending", "blocked", "approval", "approve", "handoff", "hand off",
)

_ACCESS_HIGH_TERMS = (
    "admin", "owner", "prod", "production", "subscription", "security", "secret", "key vault",
    "credential", "github", "devops", "repository", "repo", "azure",
)


def collect(graph_token: str, identity: Optional[dict] = None, progress=None) -> tuple[str, dict]:
    """Build a handover brief from Microsoft Graph for the signed-in user."""

    def emit(msg: str):
        print(msg, flush=True)
        if progress:
            try:
                progress(msg)
            except Exception:  # noqa: BLE001
                pass

    identity = dict(identity or {})
    upn = identity.get("upn") or "me"

    # Seed from any existing brief so sections Graph cannot produce (projects,
    # recurring processes, outstanding items, access transfers) and prior data
    # are preserved. Graph only overlays the sections it can refresh below.
    base = storage.load(upn) or {}
    result: dict = {
        "employee": dict(base.get("employee") or {}),
        "contacts": list(base.get("contacts") or []),
        "projects": list(base.get("projects") or []),
        "importantFiles": list(base.get("importantFiles") or []),
        "recurringProcesses": list(base.get("recurringProcesses") or []),
        "outstandingItems": list(base.get("outstandingItems") or []),
        "accessTransfers": list(base.get("accessTransfers") or []),
    }

    # --- employee profile + manager --------------------------------------- #
    try:
        emit("  Graph: reading your profile...")
        me = _get(
            "/me?$select=displayName,jobTitle,department,mail,userPrincipalName,"
            "officeLocation,companyName",
            graph_token,
        )
        upn = me.get("userPrincipalName") or me.get("mail") or upn
        emp = {
            "displayName": me.get("displayName"),
            "jobTitle": me.get("jobTitle"),
            "role": me.get("jobTitle"),
            "department": me.get("department"),
            "organization": me.get("companyName"),
            "email": me.get("mail") or me.get("userPrincipalName"),
            "officeLocation": me.get("officeLocation"),
            "upn": me.get("userPrincipalName"),
        }
        try:
            mgr = _get("/me/manager?$select=displayName,mail,userPrincipalName", graph_token)
            mgr_email = mgr.get("mail") or mgr.get("userPrincipalName")
            if mgr.get("displayName") or mgr_email:
                emp["manager"] = {"displayName": mgr.get("displayName"), "email": mgr_email}
        except GraphError as e:
            emit(f"    - manager unavailable: {e}")
        merged_emp = dict(result.get("employee") or {})
        merged_emp.update({k: v for k, v in emp.items() if v})
        result["employee"] = merged_emp
        emit("    + employee profile")
    except GraphError as e:
        emit(f"    ! profile failed: {e}")

    # --- key contacts via relevant people --------------------------------- #
    try:
        emit("  Graph: finding people you work with...")
        people = _get(
            f"/me/people?$top={config.MAX_CONTACTS}&$select=displayName,scoredEmailAddresses,jobTitle,"
            "companyName,personType",
            graph_token,
        )
        contacts = []
        for p in people.get("value", []):
            emails = p.get("scoredEmailAddresses") or []
            email = emails[0].get("address") if emails else ""
            if not (p.get("displayName") or email):
                continue
            ptype = p.get("personType") or {}
            contacts.append(
                {
                    "name": p.get("displayName") or email,
                    "email": email,
                    "org": p.get("companyName") or "",
                    "relationship": ptype.get("subclass") or ptype.get("class") or "",
                    "scope": p.get("jobTitle") or "",
                }
            )
        # Only overlay when Graph returned people, so a transient empty/failed
        # response doesn't wipe previously collected contacts.
        if contacts:
            result["contacts"] = contacts
        emit(f"    + contacts: {len(contacts)}")
    except GraphError as e:
        emit(f"    ! contacts failed: {e}")

    # --- richer Graph signals used to generate handover sections ---------- #
    files = _collect_important_files(graph_token, emit)
    if files:
        result["importantFiles"] = _merge_lists(
            result.get("importantFiles") or [], files, ("url", "name"), config.MAX_IMPORTANT_FILES
        )

    events = _collect_events(graph_token, emit)
    messages = _collect_messages(graph_token, emit)
    tasks = _collect_tasks(graph_token, emit)
    teams, groups = _collect_groups(graph_token, emit)

    projects = _generate_projects(events, messages, result.get("importantFiles") or [], tasks, result.get("employee") or {})
    if projects:
        result["projects"] = _merge_lists(result.get("projects") or [], projects, ("name",), config.MAX_PROJECTS)
    emit(f"    + generated projects: {len(projects)}")

    processes = _generate_recurring_processes(events)
    if processes:
        result["recurringProcesses"] = _merge_lists(
            result.get("recurringProcesses") or [], processes, ("name", "cadence"), config.MAX_RECURRING_PROCESSES
        )
    emit(f"    + generated recurring processes: {len(processes)}")

    outstanding = _generate_outstanding_items(tasks, messages, identity)
    if outstanding:
        result["outstandingItems"] = _merge_lists(
            result.get("outstandingItems") or [], outstanding, ("description", "source"), config.MAX_OUTSTANDING_ITEMS
        )
    emit(f"    + generated outstanding items: {len(outstanding)}")

    access = _generate_access_transfers(teams, groups, result.get("importantFiles") or [], messages)
    if access:
        result["accessTransfers"] = _merge_lists(
            result.get("accessTransfers") or [], access, ("system", "type"), config.MAX_ACCESS_TRANSFERS
        )
    emit(f"    + generated access transfers: {len(access)}")

    result["meta"] = {
        "generatedAt": datetime.now(timezone.utc).astimezone().isoformat(),
        "source": "Microsoft Graph (Work IQ unavailable)",
        "subjectUpn": upn,
        "notes": (
            "Generated from Microsoft Graph because the Work IQ resource is not "
            "available in this tenant. Project, process, outstanding item, and "
            "access-transfer sections are inferred from Graph calendar, mail, task, "
            "file, Team, and group signals when those permissions are available."
        ),
    }
    return upn, result
