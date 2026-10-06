"""
collect.py
==========
Refreshes data/handover.json by asking the **Work IQ REST API** a series of
questions, one per handover section, and parsing the structured JSON answers.
Factual/directory sections are routed to Microsoft Graph when a Graph token is
available (accurate and free), so Work IQ credits are spent only on synthesis.

Run:
    python collect.py                 # uses the Work IQ REST API

Because Work IQ is conversational, each section is requested with a strict
"respond with ONLY JSON matching this schema" prompt; the response is then
sanitized (citations / markdown fences stripped) and parsed. If a section fails
to parse, the existing snapshot value is preserved.
"""

from __future__ import annotations

import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import config
import storage
from workiq_client import get_client, WorkIQError, WorkIQRestClient

# --------------------------------------------------------------------------- #
# Prompts: each asks Work IQ for one section as strict JSON.
# --------------------------------------------------------------------------- #
COMMON = (
    "You are helping build an employee transition/handover brief. "
    "Answer ONLY with valid minified JSON, no prose, no markdown fences, no citations. "
)

PROMPTS = {
    "employee": COMMON + (
        "Return a JSON object describing me with keys: displayName, jobTitle, role "
        "(my job role/title exactly as listed in my Microsoft 365 profile), "
        "department, organization, email, officeLocation, manager (object with "
        "displayName and email). Use my Microsoft 365 profile and org data."
    ),
    "contacts": COMMON + (
        f"Return a JSON array of up to {config.MAX_CONTACTS} people I work with most closely, based on my "
        "email, meetings and Teams chats. Each item: {name, email, relationship "
        "(e.g. Manager, Peer, Customer stakeholder, Sales partner), org (their company "
        "or 'Microsoft'), scope (one short phrase on how we work together)}."
    ),
    "projects": COMMON + (
        "Return a JSON array of the active projects/engagements I am working on, inferred "
        "from my recurring meetings, recent email and files. Each item: {name, customer, "
        "role (my role), status, summary (one sentence), pending (array of short strings "
        "of what is still open)}."
    ),
    "importantFiles": COMMON + (
        f"Return a JSON array of up to {config.MAX_IMPORTANT_FILES} documents from my recent OneDrive/SharePoint files: "
        "Either important documents a successor would need or documents I've recently created, "
        "edited, or opened. Each item: {name, why (one phrase), url, "
        "sensitivityLabel (the display name of the file's Microsoft Purview sensitivity "
        "label exactly as shown — e.g. 'General', 'Confidential', 'Highly Confidential \\ PII' "
        "— or an empty string if the file has no label)}. "
        "The 'url' MUST be the actual SharePoint/OneDrive web URL of the file (webUrl) so it "
        "opens when clicked — never leave url empty."
    ),
    "recurringProcesses": COMMON + (
        "Return a JSON array of recurring processes or routines I run that others may not "
        "know how to do, inferred from my recurring meetings and responsibilities. Each "
        "item: {name, cadence, description (one sentence), knowledgeRisk (low|medium|high)}."
    ),
    "outstandingItems": COMMON + (
        "Return a JSON array of outstanding action items that need to be handed off, from "
        "my recent email and chats. Each item: {description, suggestedOwner, dueDate "
        "(YYYY-MM-DD or empty), priority (low|medium|high), source (where it came from)}."
    ),
    "accessTransfers": COMMON + (
        "Return a JSON array of systems, subscriptions, repos, sites or groups whose access "
        "must be transferred or revoked when I leave (e.g. Azure subscriptions, GitHub orgs, "
        "SharePoint sites, Teams groups), inferred from my files, email and chats. Each item: "
        "{system, type (Azure|Source control|SharePoint|Teams|Identity), detail, action, "
        "priority (low|medium|high)}."
    ),
}

# Section source routing (accuracy + cost):
#  - GRAPH_SECTIONS: factual/directory sections sourced from Microsoft Graph
#    (authoritative AND free — no Copilot Credits) whenever a Graph token is
#    available; Work IQ is not asked for these.
#  - HYBRID_SECTIONS: asked from BOTH sources and unioned (like important files).
#    Outstanding hand-off items benefit from Work IQ's reasoning over mail/chats
#    AND Graph's open To Do tasks / action-oriented mail, so neither source alone
#    is complete.
# Work IQ credits are otherwise spent only on the synthesis sections (projects,
# recurring processes) and the file merge, where its reasoning adds value.
GRAPH_SECTIONS = {"employee", "contacts", "accessTransfers"}
HYBRID_SECTIONS = {"outstandingItems"}

# List sections and how to merge a fresh collection into the existing (Cosmos)
# brief so manual edits survive a refresh: the de-duplication key(s) and the
# per-section cap from config (MAX_CONTACTS, MAX_PROJECTS, ... — see .env.example).
# User-EDITED items (flagged ``_edited`` in the UI) always win.
_MERGE_SPEC = {
    "contacts": (("email", "name"), config.MAX_CONTACTS),
    "projects": (("name",), config.MAX_PROJECTS),
    "importantFiles": (("url", "name"), config.MAX_IMPORTANT_FILES),
    "recurringProcesses": (("name", "cadence"), config.MAX_RECURRING_PROCESSES),
    "outstandingItems": (("description", "source"), config.MAX_OUTSTANDING_ITEMS),
    "accessTransfers": (("system", "type"), config.MAX_ACCESS_TRANSFERS),
}


def extract_json(text: str):
    """Pull a JSON value out of a Work IQ answer (strip citations / fences)."""
    if not text:
        return None
    # remove markdown-style citation links: [1](http...)
    text = re.sub(r"\[\d+\]\(https?://[^)]+\)", "", text)
    # strip code fences
    text = re.sub(r"```(?:json)?", "", text)
    # find the first { or [ and match to the matching close
    start = None
    for i, ch in enumerate(text):
        if ch in "[{":
            start = i
            break
    if start is None:
        return None
    opening = text[start]
    closing = "]" if opening == "[" else "}"
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == opening:
            depth += 1
        elif ch == closing:
            depth -= 1
            if depth == 0:
                blob = text[start : i + 1]
                try:
                    return json.loads(blob)
                except json.JSONDecodeError:
                    return None
    return None


# PII detection on Work IQ sensitivity labels (searchSensitivityLabelInfo.displayName).
# A label whose display name carries the "PII" token marks the file as containing PII.
_PII_LABEL_RE = re.compile(r"\bpii\b", re.IGNORECASE)


def _label_display_name(value) -> str:
    """Normalize a Work IQ sensitivity label value to its display name string.
    Work IQ's searchSensitivityLabelInfo resource exposes ``displayName``; the
    conversational answer may return it as a plain string or a {displayName,...} object."""
    if isinstance(value, dict):
        return str(value.get("displayName") or value.get("name") or "").strip()
    return str(value or "").strip()


def _flag_pii(files):
    """Tag each important file with a ``pii`` boolean derived from its sensitivity
    label display name. Files labelled with a name containing the token 'PII'
    (e.g. 'Highly Confidential \\ PII') are flagged so the UI can mark them."""
    if not isinstance(files, list):
        return files
    for f in files:
        if not isinstance(f, dict):
            continue
        label = _label_display_name(f.get("sensitivityLabel"))
        f["sensitivityLabel"] = label
        f["pii"] = bool(label and _PII_LABEL_RE.search(label))
    return files


def load_base(upn: str | None) -> dict:
    if upn:
        existing = storage.load(upn)
        if existing:
            return existing
    return {}


def collect(
    transport: str | None = None,
    progress=None,
    token: str | None = None,
    identity: dict | None = None,
    graph_token: str | None = None,
) -> tuple[str, dict]:
    """Run a Work IQ collection for the signed-in user.
    Returns (upn, handover_dict). The caller persists it via storage.save().

    ``token`` / ``identity`` are supplied by the web sign-in flow so the
    collection runs with the logged-in user's delegated identity. ``graph_token``
    (optional) lets us resolve Purview sensitivity labels for the file list via
    Microsoft Graph, since Work IQ's conversational answer omits them."""

    def emit(msg: str):
        print(msg, flush=True)
        if progress:
            try:
                progress(msg)
            except Exception:  # noqa: BLE001
                pass

    client = get_client(transport, token=token)
    started = hasattr(client, "start")
    if started:
        emit("Starting Work IQ session (acquiring token)...")
        client.start()

    # Identify the signed-in user. Prefer the identity supplied by the web
    # sign-in; otherwise decode it from the delegated token (REST transport).
    identity = dict(identity or {})
    if not identity.get("upn") and isinstance(client, WorkIQRestClient):
        try:
            identity = client.auth.get_identity()
        except Exception:  # noqa: BLE001
            identity = {}
    upn = identity.get("upn") or "me"

    base = load_base(upn)
    result = dict(base)
    result["meta"] = {
        "generatedAt": datetime.now(timezone.utc).astimezone().isoformat(),
        "source": f"Work IQ API ({(transport or config.WORKIQ_TRANSPORT or 'rest')})",
        "subjectUpn": upn,
    }
    # Seed identity so the brief is attributable even before the employee section returns.
    emp = dict(result.get("employee", {}))
    if identity.get("name"):
        emp.setdefault("displayName", identity["name"])
    if identity.get("upn"):
        emp.setdefault("upn", identity["upn"])
        emp.setdefault("email", identity["upn"])
    result["employee"] = emp

    parallel = hasattr(client, "ask_once")

    def fetch(section: str, prompt: str):
        ask = client.ask_once if parallel else client.ask
        try:
            answer = ask(prompt, timeout=config.SECTION_TIMEOUT)
            return section, extract_json(answer), None
        except Exception as e:  # noqa: BLE001 — never let one section kill the whole run
            return section, None, str(e)

    # Route the factual sections to Microsoft Graph when a Graph token is available
    # (accurate + free); otherwise fall back to asking Work IQ for everything.
    routed_to_graph = GRAPH_SECTIONS if graph_token else set()
    workiq_prompts = {s: p for s, p in PROMPTS.items() if s not in routed_to_graph}
    try:
        if parallel:
            workers = config.MAX_WORKERS
            emit(f"  asking Work IQ for {len(workiq_prompts)} sections in parallel (workers={workers})...")
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {pool.submit(fetch, sec, prompt): sec for sec, prompt in workiq_prompts.items()}
                for fut in as_completed(futures):
                    section, parsed, err = fut.result()
                    _apply(result, section, parsed, err, emit)
        else:
            for section, prompt in workiq_prompts.items():
                emit(f"  asking Work IQ for: {section} ...")
                section, parsed, err = fetch(section, prompt)
                _apply(result, section, parsed, err, emit)
    finally:
        if started:
            client.close()

    # Important Files: combine both sources, labeling each file exactly once.
    #  - Work IQ contributes importance-ranked picks useful for a transition
    #    (including SharePoint documents).
    #  - Graph adds ONLY files Work IQ didn't already return (de-dup by name), so
    #    a file returned by Work IQ is never re-fetched from the drive.
    # Graph files are gathered WITHOUT labeling; after the merge we resolve Purview
    # sensitivity labels once for the whole list so PII is flagged on every file
    # (OneDrive or SharePoint) without labeling any file twice.
    if graph_token:
        try:
            from graph_collect import _collect_important_files, _annotate_sensitivity, _merge_lists

            workiq_files = [
                f for f in (result.get("importantFiles") or [])
                if isinstance(f, dict) and (f.get("url") or "").strip()
            ]
            graph_files = _collect_important_files(graph_token, emit, annotate=False)
            merged = _merge_lists(graph_files, workiq_files, ("name",), config.MAX_IMPORTANT_FILES)  # Work IQ first, then new Graph files
            _annotate_sensitivity(merged, graph_token, emit)                 # label each file once
            if merged:
                result["importantFiles"] = merged
            emit(f"    + important files (Work IQ + Graph merged): {len(merged)}")
        except Exception as e:  # noqa: BLE001 — best-effort; keep Work IQ's list on failure
            emit(f"  file merge/enrichment skipped: {e}")

    # Factual/directory sections from Microsoft Graph (authoritative + free): the
    # employee profile & manager (Entra), key contacts, and access transfers.
    # These are more accurate than Work IQ's conversational answer and spend no
    # Copilot Credits, so they overlay the routed sections. Hybrid sections
    # (outstanding items) are instead UNIONED with Work IQ's synthesis.
    if graph_token:
        try:
            import graph_collect
            from graph_collect import _merge_lists

            _, gdata = graph_collect.collect(graph_token, identity, progress)
            # Factual sections: Graph is authoritative — replace.
            for sec in sorted(routed_to_graph):
                val = gdata.get(sec)
                if not val:
                    continue
                if sec == "employee" and isinstance(val, dict):
                    emp = dict(result.get("employee") or {})
                    emp.update({k: v for k, v in val.items() if v})
                    result["employee"] = emp
                else:
                    result[sec] = val
            # Hybrid sections: union Work IQ's synthesis with Graph's factual
            # signals — Work IQ wins on de-dup, Graph adds what it missed.
            for sec in sorted(HYBRID_SECTIONS):
                workiq_items = result.get(sec) or []
                graph_items = gdata.get(sec) or []
                if workiq_items or graph_items:
                    keys, limit = _MERGE_SPEC.get(sec, (("description",), 20))
                    result[sec] = _merge_lists(list(graph_items), list(workiq_items), keys, limit)
            labels = sorted(routed_to_graph) + [f"{s} (hybrid)" for s in sorted(HYBRID_SECTIONS)]
            emit(f"    + Graph-sourced sections: {', '.join(labels)}")
        except Exception as e:  # noqa: BLE001 — keep seeded/Work IQ data on failure
            emit(f"  Graph section routing skipped: {e}")

    # Merge the freshly-collected sections with the existing (Cosmos) brief so
    # manual edits and prior items survive a refresh. Priority (de-duplicated by
    # key, first wins): user-EDITED items are never replaced; then fresh data
    # (updates non-edited items and adds new ones); then any remaining prior items.
    from graph_collect import _merge_lists

    for section, (keys, limit) in _MERGE_SPEC.items():
        new_items = result.get(section) or []
        old_items = base.get(section) or []
        edited_old = [it for it in old_items if isinstance(it, dict) and it.get("_edited")]
        plain_old = [it for it in old_items if not (isinstance(it, dict) and it.get("_edited"))]
        ordered = edited_old + list(new_items) + plain_old
        if ordered:
            result[section] = _merge_lists([], ordered, keys, limit)

    # Preserve manually-edited employee fields over fresh data too.
    base_emp = base.get("employee") or {}
    if isinstance(base_emp, dict) and base_emp.get("_edited"):
        emp = dict(result.get("employee") or {})
        emp.update({k: v for k, v in base_emp.items() if v not in (None, "")})
        result["employee"] = emp

    return upn, result


def _apply(result: dict, section: str, parsed, err, emit) -> None:
    if err:
        emit(f"    ! {section} failed: {err}")
    if parsed is None:
        emit(f"    - kept existing snapshot for {section}")
        return
    # merge employee identity rather than clobbering token-seeded fields
    if section == "employee" and isinstance(parsed, dict):
        merged = dict(result.get("employee", {}))
        merged.update({k: v for k, v in parsed.items() if v})
        result["employee"] = merged
    else:
        if section == "importantFiles":
            parsed = _flag_pii(parsed)
        result[section] = parsed
    count = len(parsed) if isinstance(parsed, list) else 1
    emit(f"    + {section}: {count} item(s)")


def main() -> int:
    transport = sys.argv[1] if len(sys.argv) > 1 else None
    upn, data = collect(transport)
    path = storage.save(upn, data)
    print(f"\nWrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
