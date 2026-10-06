"""
app.py — Flask application for the Transition Agent (enterprise build).

Serves the dashboard and a JSON API backed by per-employee handover files. Live
refresh calls the Work IQ REST API (see workiq_client.py / collect.py).

Run in production with a real WSGI server:
    python serve.py            # waitress
Or for local dev:
    python app.py
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import Flask, jsonify, request, send_from_directory, abort, redirect, session

import auth
import config
import grants
import refresh_jobs
import storage

logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
# The Azure SDKs (Cosmos, identity) log every HTTP request/response at INFO,
# which floods the console. Keep them at WARNING so the collector's own
# progress lines stay readable.
for _noisy in ("azure", "azure.cosmos", "azure.core.pipeline.policies.http_logging_policy",
               "azure.identity", "urllib3", "msal"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)
log = logging.getLogger("transition-agent")

APP_VERSION = "2.0.0"

app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = config.MAX_CONTENT_LENGTH
app.config["SECRET_KEY"] = config.SECRET_KEY
# Session cookie carries the signed-in identity: keep it out of JavaScript and
# cross-site requests. SameSite=Lax (not Strict) so the Entra auth-code redirect
# back to /auth/callback still presents the cookie.
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=config.SESSION_COOKIE_SECURE,
    PERMANENT_SESSION_LIFETIME=timedelta(minutes=config.SESSION_LIFETIME_MINUTES),
)

if config.TRUST_PROXY:
    from werkzeug.middleware.proxy_fix import ProxyFix

    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

# one-time migration of any legacy single-file data
storage.migrate_legacy()


# --------------------------------------------------------------------------- #
# security headers
# --------------------------------------------------------------------------- #
@app.after_request
def _security_headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
        "script-src 'self'; connect-src 'self'; frame-ancestors 'none'"
    )
    # TRUST_PROXY means we sit behind a TLS-terminating ingress (Container Apps /
    # App Service accept HTTPS only), which is more reliable here than is_secure:
    # the forwarded-proto header does not always survive to the WSGI environ.
    # No 'preload': the default *.azurecontainerapps.io / *.azurewebsites.net hosts
    # are shared domains, so preloading them would be inappropriate.
    if config.TRUST_PROXY or request.is_secure:
        resp.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    if request.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


# --------------------------------------------------------------------------- #
# Microsoft Entra ID sign-in
# --------------------------------------------------------------------------- #
# Paths that never require a signed-in user.
_PUBLIC_PATHS = {"/login", "/logout", "/auth/callback", "/healthz"}


@app.before_request
def _require_login():
    if not config.AUTH_ENABLED:
        return None
    path = request.path
    if path in _PUBLIC_PATHS:
        return None
    if auth.current_user():
        return None
    if path.startswith("/api/"):
        return jsonify({"error": "Authentication required", "login": "/login"}), 401
    return redirect("/login")


@app.get("/login")
def login():
    if not config.AUTH_ENABLED:
        return redirect("/")
    try:
        return auth.login()
    except (auth.AuthError, ValueError) as e:
        log.error("sign-in misconfigured: %s", e)
        return jsonify({"error": f"Sign-in is misconfigured: {e}"}), 503


@app.get("/auth/callback")
def auth_callback():
    try:
        return auth.callback()
    except (auth.AuthError, ValueError) as e:
        log.error("sign-in callback failed: %s", e)
        return jsonify({"error": f"Sign-in failed: {e}"}), 503


@app.get("/logout")
def logout():
    return auth.logout()


@app.get("/api/me")
def api_me():
    user = auth.current_user()
    return jsonify(
        {
            "authenticated": bool(user),
            "authEnabled": config.AUTH_ENABLED,
            "workiqEnabled": config.WORKIQ_ENABLED,
            "isAdmin": auth.is_admin(user) if user else False,
            "user": user,
        }
    )


# --------------------------------------------------------------------------- #
# delegated-access authorization
# --------------------------------------------------------------------------- #
def _can_access(user: dict | None, subject_slug: str) -> bool:
    """A signed-in user may read a subject's data if they are the subject, an
    administrator, or hold an active grant for that subject."""
    if not config.AUTH_ENABLED:
        return True
    if not user:
        return False
    if auth.is_admin(user):
        return True
    own = storage.slugify(user.get("upn") or "")
    if own and own == subject_slug:
        return True
    return grants.has_access(user, subject_slug)


def require_admin(fn):
    """Gate an endpoint behind the administrator role."""

    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not config.AUTH_ENABLED:
            return jsonify({"error": "Administration requires sign-in to be enabled."}), 403
        if not auth.is_admin():
            return jsonify({"error": "Administrator role required."}), 403
        return fn(*args, **kwargs)

    return wrapper


@app.get("/api/shared")
def api_shared():
    """Departed employees whose data has been shared with the signed-in user."""
    if not config.AUTH_ENABLED:
        return jsonify({"shared": []})
    user = auth.current_user()
    if not user:
        return jsonify({"shared": []}), 401
    subjects = {u["slug"]: u for u in storage.list_users()}
    shared = []
    for g in grants.for_grantee(user):
        slug = g.get("subjectSlug")
        subj = subjects.get(slug, {})
        shared.append(
            {
                "slug": slug,
                "displayName": subj.get("displayName") or slug,
                "upn": subj.get("upn"),
                "exists": slug in subjects,
                "grantedBy": g.get("grantedBy"),
                "grantedAt": g.get("grantedAt"),
                "expiresAt": g.get("expiresAt"),
            }
        )
    return jsonify({"shared": shared})



# --------------------------------------------------------------------------- #
# static UI
# --------------------------------------------------------------------------- #
@app.get("/")
def index():
    return send_from_directory(config.PUBLIC_DIR, "index.html")


@app.get("/admin")
def admin_page():
    # The APIs enforce the admin role; redirect non-admins away from the UI too.
    if config.AUTH_ENABLED and not auth.is_admin():
        return redirect("/")
    return send_from_directory(config.PUBLIC_DIR, "admin.html")


@app.get("/shared")
def shared_page():
    # Read-only view of departed employees whose data has been shared with the
    # signed-in user. before_request already requires sign-in.
    return send_from_directory(config.PUBLIC_DIR, "shared.html")


@app.get("/<path:path>")
def static_files(path: str):
    # prevent path traversal; send_from_directory already guards, but be explicit
    if path.startswith("api/"):
        abort(404)
    return send_from_directory(config.PUBLIC_DIR, path)


# --------------------------------------------------------------------------- #
# health / metadata
# --------------------------------------------------------------------------- #
@app.get("/healthz")
def healthz():
    # Liveness must not depend on the handover store: when Cosmos is unreachable a
    # failing probe would restart the container in a loop instead of serving.
    try:
        users = len(storage.list_users())
        store = "ok"
    except Exception:  # noqa: BLE001
        log.exception("health check: handover store unavailable")
        users, store = None, "unavailable"
    return jsonify(
        {
            "status": "ok",
            "store": store,
            "version": APP_VERSION,
            "users": users,
            "refreshEnabled": config.ENABLE_REFRESH,
            "time": datetime.now(timezone.utc).isoformat(),
        }
    )


@app.get("/api/users")
def api_users():
    # Deny-by-default: only administrators see every brief. Everyone else sees
    # their own plus any they've been granted delegated access to.
    if not config.AUTH_ENABLED:
        return jsonify({"users": storage.list_users()})
    user = auth.current_user()
    if auth.is_admin(user):
        return jsonify({"users": storage.list_users()})
    own = storage.slugify((user or {}).get("upn") or "")
    allowed = grants.accessible_subjects(user) | ({own} if own else set())
    return jsonify({"users": [u for u in storage.list_users() if u.get("slug") in allowed]})


def _default_user():
    users = storage.list_users()
    if not users:
        return None
    # most recently generated brief wins
    users.sort(key=lambda u: (u.get("savedAt") or u.get("generatedAt") or ""), reverse=True)
    return users[0]


# --------------------------------------------------------------------------- #
# handover read / write
# --------------------------------------------------------------------------- #
@app.get("/api/handover")
def get_handover():
    slug = request.args.get("user")
    user = auth.current_user() if config.AUTH_ENABLED else None

    # Default to the signed-in user's own brief when no subject is requested.
    if not slug and user and user.get("upn"):
        slug = storage.slugify(user["upn"])

    # With sign-in enforced, every read is authorized against the subject.
    if config.AUTH_ENABLED:
        if not slug:
            return jsonify({"error": "No handover for that user"}), 404
        if not _can_access(user, slug):
            return jsonify({"error": "You do not have access to this handover"}), 403
        data = storage.resolve_slug(slug)
        if data is None:
            return jsonify({"error": "No handover for that user"}), 404
        return jsonify(data)

    # Unauthenticated (local dev): preserve the original open behavior.
    if slug:
        data = storage.resolve_slug(slug)
        if data is None:
            return jsonify({"error": "No handover for that user"}), 404
        return jsonify(data)
    default = _default_user()
    if not default:
        return jsonify({"error": "No handover data yet. Run a refresh to generate one."}), 404
    data = storage.resolve_slug(default["slug"])
    return jsonify(data)


@app.put("/api/handover")
def put_handover():
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or "employee" not in body:
        return jsonify({"ok": False, "error": "Invalid handover payload"}), 400
    upn = (body.get("meta", {}) or {}).get("subjectUpn") or body["employee"].get("upn") or body["employee"].get("email")
    if not upn:
        return jsonify({"ok": False, "error": "Cannot determine subject user for this handover"}), 400
    body.setdefault("meta", {})
    body["meta"]["savedAt"] = datetime.now(timezone.utc).astimezone().isoformat()
    storage.save(upn, body)
    log.info("handover saved for %s", upn)
    return jsonify({"ok": True, "savedAt": body["meta"]["savedAt"], "user": storage.slugify(upn)})


# --------------------------------------------------------------------------- #
# live refresh from the Work IQ API
# --------------------------------------------------------------------------- #
# Execution is delegated to refresh_jobs: with Redis it enqueues onto a queue that
# a separate worker pool drains (stateless web tier); without Redis it runs in a
# background thread in this process. Either way the status shape is identical.
@app.post("/api/refresh")
def refresh():
    if not config.ENABLE_REFRESH:
        return jsonify({"ok": False, "error": "Live refresh is disabled on this deployment"}), 403

    identity = auth.current_user() if config.AUTH_ENABLED else None
    sid = session.get("sid")
    transport = (request.get_json(silent=True) or {}).get("transport")
    key = refresh_jobs.refresh_key(identity)
    ok, status, err = refresh_jobs.start_refresh(key, sid, identity, transport)
    if not ok:
        return jsonify({"ok": False, "error": err}), status
    return jsonify({"ok": True, "started": True})


@app.get("/api/refresh/status")
def refresh_status():
    identity = auth.current_user() if config.AUTH_ENABLED else None
    return jsonify(refresh_jobs.get_status(refresh_jobs.refresh_key(identity)))


# --------------------------------------------------------------------------- #
# back up selected files to Azure Blob Storage
# --------------------------------------------------------------------------- #
@app.get("/api/backup/config")
def backup_config():
    import blob_backup

    return jsonify({"configured": blob_backup.is_configured(), "container": config.AZURE_BACKUP_CONTAINER})


@app.post("/api/backup")
def backup():
    import blob_backup
    import graph_files

    if not blob_backup.is_configured():
        return jsonify({"ok": False, "error": "Azure Blob Storage is not configured on this deployment."}), 503

    body = request.get_json(silent=True) or {}
    files = body.get("files") or []
    subject = body.get("user") or "unknown"
    files = [f for f in files if isinstance(f, dict) and f.get("url")]
    if not files:
        return jsonify({"ok": False, "error": "No files with a URL were selected."}), 400

    # Backups are read with the signed-in user's delegated Graph token, so a user
    # may only back up their OWN files (a successor cannot read the departed
    # employee's source files). Reading the backups back is authorized separately.
    if config.AUTH_ENABLED:
        me = auth.current_user()
        own = storage.slugify((me or {}).get("upn") or "")
        if not own or storage.slugify(subject) != own:
            return jsonify({"ok": False, "error": "You can only back up your own files."}), 403

    # one Graph token for the whole batch — reuse the signed-in user's delegated
    # identity (same token source as Work IQ) when sign-in is enabled.
    try:
        if config.AUTH_ENABLED:
            token = auth.get_graph_token()
        else:
            token = graph_files.GraphAuth().get_token(allow_interactive=config.BACKUP_ALLOW_INTERACTIVE)
    except auth.AuthError as e:
        return jsonify({"ok": False, "error": str(e)}), 401
    except graph_files.GraphError as e:
        if "graph_consent_required" in str(e):
            return jsonify({"ok": False, "error": "Sign-in required to read files for backup. Run a backup from the desktop app to consent once."}), 401
        return jsonify({"ok": False, "error": f"Could not sign in to read files: {e}"}), 502

    prefix = blob_backup.backup_prefix(subject)
    results = []
    ok_count = 0
    for f in files:
        name = f.get("name") or "file"
        try:
            content, ctype, meta = graph_files.download_shared(
                f["url"], token, max_bytes=config.BACKUP_MAX_FILE_BYTES
            )
            safe_name = "".join(c if (c.isalnum() or c in " ._-()") else "_" for c in (meta.get("name") or name))
            blob_name = f"{prefix}/{safe_name}"
            up = blob_backup.upload_bytes(
                content, blob_name, content_type=ctype,
                metadata={"sourceUrl": meta.get("webUrl") or f["url"], "subject": subject},
            )
            ok_count += 1
            results.append({"name": meta.get("name") or name, "ok": True, "blobUrl": up["url"], "size": up["size"]})
        except Exception as e:  # noqa: BLE001
            log.warning("backup failed for %s: %s", name, e)
            results.append({"name": name, "ok": False, "error": str(e)})

    return jsonify(
        {
            "ok": ok_count > 0,
            "backedUp": ok_count,
            "total": len(files),
            "container": config.AZURE_BACKUP_CONTAINER,
            "prefix": prefix,
            "results": results,
        }
    )


# --------------------------------------------------------------------------- #
# read backed-up files (subject, admin, or a granted successor)
# --------------------------------------------------------------------------- #
@app.get("/api/backups")
def list_backups():
    import blob_backup

    if not blob_backup.is_configured():
        return jsonify({"configured": False, "files": []})
    slug = request.args.get("user")
    user = auth.current_user() if config.AUTH_ENABLED else None
    if not slug and user and user.get("upn"):
        slug = storage.slugify(user["upn"])
    if config.AUTH_ENABLED and (not slug or not _can_access(user, slug)):
        return jsonify({"ok": False, "error": "You do not have access to these files."}), 403
    try:
        files = blob_backup.list_backups(slug)
    except Exception as e:  # noqa: BLE001
        log.warning("listing backups for %s failed: %s", slug, e)
        return jsonify({"ok": False, "error": "Could not list backups."}), 502
    return jsonify({"ok": True, "configured": True, "user": slug, "files": files})


@app.get("/api/backups/download")
def download_backup():
    import blob_backup
    from flask import Response

    if not blob_backup.is_configured():
        abort(404)
    slug = request.args.get("user")
    blob_name = request.args.get("blob")
    if not slug or not blob_name:
        return jsonify({"ok": False, "error": "Missing user or blob."}), 400
    user = auth.current_user() if config.AUTH_ENABLED else None
    if config.AUTH_ENABLED and not _can_access(user, slug):
        return jsonify({"ok": False, "error": "You do not have access to this file."}), 403
    try:
        chunks, ctype, size, filename = blob_backup.open_backup_stream(slug, blob_name)
    except blob_backup.BlobBackupError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:  # noqa: BLE001
        log.warning("download of %s failed: %s", blob_name, e)
        return jsonify({"ok": False, "error": "Could not download the file."}), 502
    headers = {
        "Content-Disposition": f'attachment; filename="{filename}"',
        "Content-Length": str(size),
    }
    return Response(chunks, mimetype=ctype, headers=headers)


# --------------------------------------------------------------------------- #
# administration: grant / revoke delegated access
# --------------------------------------------------------------------------- #
@app.get("/api/admin/subjects")
@require_admin
def admin_subjects():
    return jsonify({"subjects": storage.list_users()})


@app.get("/api/admin/grants")
@require_admin
def admin_list_grants():
    slug = request.args.get("user")
    rows = grants.for_subject(slug) if slug else grants.list_all()
    return jsonify({"grants": rows})


@app.post("/api/admin/grants")
@require_admin
def admin_create_grant():
    body = request.get_json(silent=True) or {}
    subject = (body.get("subject") or "").strip()
    grantee_upn = (body.get("granteeUpn") or "").strip()
    days = body.get("days")
    if not subject or not grantee_upn:
        return jsonify({"ok": False, "error": "subject and granteeUpn are required."}), 400
    subject_slug = storage.slugify(subject)
    if storage.resolve_slug(subject_slug) is None:
        return jsonify({"ok": False, "error": "No handover exists for that employee."}), 404
    if "@" not in grantee_upn:
        return jsonify({"ok": False, "error": "granteeUpn must be a user principal name (email)."}), 400
    admin = auth.current_user() or {}
    grant = grants.create(
        subject_slug=subject_slug,
        grantee_upn=grantee_upn,
        granted_by=admin.get("upn"),
        granted_by_oid=admin.get("oid"),
        days=days,
    )
    log.info("grant created: %s -> %s by %s", grantee_upn, subject_slug, admin.get("upn"))
    return jsonify({"ok": True, "grant": grant})


@app.post("/api/admin/grants/revoke")
@require_admin
def admin_revoke_grant():
    body = request.get_json(silent=True) or {}
    grant_id = (body.get("id") or "").strip()
    subject_slug = (body.get("subject") or "").strip()
    if not grant_id or not subject_slug:
        return jsonify({"ok": False, "error": "id and subject are required."}), 400
    admin = auth.current_user() or {}
    ok = grants.revoke(grant_id, subject_slug, revoked_by=admin.get("upn"))
    if not ok:
        return jsonify({"ok": False, "error": "Grant not found."}), 404
    log.info("grant revoked: %s (%s) by %s", grant_id, subject_slug, admin.get("upn"))
    return jsonify({"ok": True})


if __name__ == "__main__":
    log.info("Transition Agent (dev server) on http://%s:%s", config.HOST, config.PORT)
    app.run(host=config.HOST, port=config.PORT, debug=False)
