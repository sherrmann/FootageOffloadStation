"""Flask application: single-page UI, REST API (/api/v1), SSE and downloads.

The API is described in docs/ARCHITECTURE.md. Responses carry message ids,
never English sentences. Works with Flask 2.2 (Bookworm) and 3.x.
"""
import datetime
import json
import logging
import mimetypes
import os
import queue
import secrets
import socket
import stat
import time
from urllib.parse import quote

from flask import Flask, Response, jsonify, request, send_file, send_from_directory, session
from werkzeug.exceptions import HTTPException
from werkzeug.routing import Map

from .. import __version__, auth, diagnostics, i18n, paths
from ..config import LANGUAGES, SECRET_KEYS, ConfigError, validate
from ..profiles import GoProProfile, preview_names

log = logging.getLogger(__name__)

API = "/api/v1"
SSE_KEEPALIVE_S = 15.0

VIDEO_EXT = {".MP4", ".MOV"}
PHOTO_EXT = {".JPG", ".JPEG", ".PNG", ".GPR", ".DNG", ".HEIC"}
BROWSER_PHOTO_EXT = {".JPG", ".JPEG", ".PNG"}
MEDIA_TYPES = {
    ".THM": "image/jpeg",
    ".LRV": "video/mp4",
    ".MP4": "video/mp4",
    ".MOV": "video/quicktime",
    ".JPG": "image/jpeg",
    ".JPEG": "image/jpeg",
    ".PNG": "image/png",
    ".WAV": "audio/wav",
}

CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; "
       "media-src 'self' blob:; connect-src 'self'; font-src 'self'; object-src 'none'; "
       "base-uri 'none'; form-action 'self'; frame-ancestors 'none'")

_RECORDING = GoProProfile()


# helpers ------------------------------------------------------------------------
def load_secret_key(path=None):
    """Secret for signing session cookies; created (mode 0600) if missing."""
    path = path or paths.SECRET_KEY_FILE
    try:
        with open(path, "rb") as f:
            key = f.read()
        if len(key) >= 32:
            return key
    except FileNotFoundError:
        pass
    os.makedirs(os.path.dirname(path), exist_ok=True)
    key = secrets.token_bytes(48)
    tmp = f"{path}.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(key)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return key


def media_type(name):
    ext = os.path.splitext(name)[1].upper()
    return MEDIA_TYPES.get(ext) or mimetypes.guess_type(name)[0] or "application/octet-stream"


def media_url(run, name, inline=False):
    url = f"/media/{quote(run, safe='')}/{quote(name, safe='')}"
    return url + "?inline=1" if inline else url


def error(message_id, code, **extra):
    body = {"error": message_id}
    body.update(extra)
    return jsonify(body), code


def _list_run_files(path):
    """Visible regular files in a run folder: {name: size}."""
    out = {}
    try:
        with os.scandir(path) as it:
            for entry in it:
                if entry.name.startswith("."):
                    continue
                try:
                    if entry.is_file(follow_symlinks=False):
                        out[entry.name] = entry.stat(follow_symlinks=False).st_size
                except OSError:
                    continue
    except OSError:
        pass
    return out


def gallery_items(run, files):
    """Gallery entries of one run folder. THM/LRV companions of a video are
    used as its thumbnail and preview and not listed on their own."""
    names = set(files)
    hidden = set()
    previews = {}
    for name in sorted(names):
        if os.path.splitext(name)[1].upper() not in VIDEO_EXT:
            continue
        thumbs, proxies = preview_names(name)
        thumb = next((t for t in thumbs if t in names and t != name), None)
        proxy = next((p for p in proxies if p in names and p != name), None)
        previews[name] = (thumb, proxy)
        hidden.update(x for x in (thumb, proxy) if x)
    items = []
    for name in sorted(names):
        if name in hidden:
            continue
        ext = os.path.splitext(name)[1].upper()
        thumb_url = preview_url = None
        if ext in VIDEO_EXT:
            kind = "video"
            thumb, proxy = previews.get(name, (None, None))
            thumb_url = media_url(run, thumb, inline=True) if thumb else None
            preview_url = media_url(run, proxy, inline=True) if proxy else None
        elif ext in PHOTO_EXT:
            kind = "photo"
            if ext in BROWSER_PHOTO_EXT:
                preview_url = media_url(run, name, inline=True)
        else:
            kind = "other"
        items.append({
            "name": name,
            "size": files[name],
            "kind": kind,
            "thumb": thumb_url,
            "preview": preview_url,
            "download": media_url(run, name),
            "recording": _RECORDING.recording_key(name),
        })
    return items


# application --------------------------------------------------------------------
class _Map(Map):
    """No redirects for paths with double slashes: odd media paths are 404."""

    def __init__(self, *args, **kwargs):
        kwargs["merge_slashes"] = False
        super().__init__(*args, **kwargs)


class _App(Flask):
    url_map_class = _Map


def create_app(service, config, state, stick_manager, helper, sysinfo=None):
    app = _App(__name__, static_folder=None)
    app.secret_key = load_secret_key()
    app.config.update(
        SESSION_COOKIE_NAME="fos_session",
        SESSION_COOKIE_SAMESITE="Strict",
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SECURE=False,          # plain HTTP on the local network
        PERMANENT_SESSION_LIFETIME=datetime.timedelta(days=30),
        MAX_CONTENT_LENGTH=64 * 1024,
    )
    limiter = auth.LoginLimiter()
    app.extensions["fos"] = {"service": service, "limiter": limiter}

    # session state --------------------------------------------------------------
    def csrf_token():
        token = session.get("csrf")
        if not token:
            token = auth.new_csrf_token()
            session["csrf"] = token
        return token

    def authenticated():
        return (bool(session.get("auth"))
                and session.get("pw") == auth.password_tag(config.get("password_hash")))

    def mark_logged_in():
        session["auth"] = True
        session["pw"] = auth.password_tag(config.get("password_hash"))
        session.permanent = True

    def has_access():
        return not config.get("login_enabled") or authenticated()

    # endpoints that never need a session (the UI must load to show the login)
    public_endpoints = {"index", "static_file", "api_session", "api_login", "api_logout", "api_i18n"}
    csrf_exempt = {"api_login"}

    @app.before_request
    def guard():
        if not config.get("login_enabled"):
            return None
        endpoint = request.endpoint
        if endpoint not in public_endpoints and not authenticated():
            return error("error.login_required", 401)
        if request.method in ("POST", "PUT", "DELETE", "PATCH") and endpoint not in csrf_exempt:
            if not auth.csrf_ok(session.get("csrf"), request.headers.get("X-CSRF-Token")):
                return error("error.csrf", 403)
        return None

    @app.after_request
    def headers(resp):
        h = resp.headers
        h.setdefault("X-Content-Type-Options", "nosniff")
        h.setdefault("X-Frame-Options", "DENY")
        h.setdefault("Referrer-Policy", "no-referrer")
        h.setdefault("Content-Security-Policy", CSP)
        h.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        h.setdefault("Cross-Origin-Resource-Policy", "same-origin")
        if request.path.startswith(API + "/"):
            h.setdefault("Cache-Control", "no-store")
        return resp

    @app.errorhandler(HTTPException)
    def http_error(e):
        if request.path.startswith(API + "/") or request.path.startswith("/media/"):
            return error("error.invalid", e.code or 400)
        return e

    @app.errorhandler(Exception)
    def internal_error(e):  # noqa: ARG001
        log.exception("unhandled error on %s %s", request.method, request.path)
        return error("error.internal", 500)

    # UI ---------------------------------------------------------------------------
    @app.get("/")
    def index():
        resp = send_from_directory(paths.WEB_DIR, "index.html", max_age=0)
        resp.headers["Cache-Control"] = "no-cache"
        return resp

    @app.get("/static/<path:filename>")
    def static_file(filename):
        resp = send_from_directory(paths.WEB_DIR, filename, max_age=0)
        resp.headers["Cache-Control"] = "no-cache"
        return resp

    # session ----------------------------------------------------------------------
    @app.get(API + "/session")
    def api_session():
        return jsonify({
            "login_enabled": bool(config.get("login_enabled")),
            "logged_in": has_access(),
            "csrf_token": csrf_token(),
            "language": i18n.negotiate(request.headers.get("Accept-Language"), config.get("language")),
            "version": __version__,
        })

    @app.post(API + "/login")
    def api_login():
        body = request.get_json(silent=True)
        password = body.get("password") if isinstance(body, dict) else None
        client = request.remote_addr or "?"
        wait = limiter.retry_after(client)
        if wait:
            resp, code = error("error.wrong_password", 401, retry_after=wait)
            resp.headers["Retry-After"] = str(wait)
            return resp, code
        if not config.get("login_enabled"):
            csrf_token()
            return jsonify({"ok": True})
        if not isinstance(password, str) or not auth.verify_password(password, config.get("password_hash")):
            limiter.failure(client)
            log.warning("failed login from %s", client)
            return error("error.wrong_password", 401)
        limiter.success(client)
        mark_logged_in()
        csrf_token()
        return jsonify({"ok": True})

    @app.post(API + "/logout")
    def api_logout():
        session.pop("auth", None)
        session.pop("pw", None)
        return jsonify({"ok": True})

    # status -----------------------------------------------------------------------
    @app.get(API + "/status")
    def api_status():
        return jsonify(service.status.snapshot())

    @app.get(API + "/events")
    def api_events():
        q, unsubscribe = service.status.queue_subscription()
        first = service.status.snapshot()

        def event(snap):
            return "event: status\ndata: " + json.dumps(snap, separators=(",", ":")) + "\n\n"

        def stream():
            try:
                yield "retry: 3000\n\n" + event(first)
                while True:
                    try:
                        snap = q.get(timeout=SSE_KEEPALIVE_S)
                    except queue.Empty:
                        yield ": keep-alive\n\n"
                        continue
                    yield event(snap)
            finally:
                unsubscribe()

        resp = Response(stream(), mimetype="text/event-stream")
        resp.headers["Cache-Control"] = "no-cache"
        resp.headers["X-Accel-Buffering"] = "no"
        return resp

    # stick ------------------------------------------------------------------------
    def current_stick():
        try:
            return stick_manager.stick()
        except Exception:  # noqa: BLE001
            log.exception("stick lookup failed")
            return None

    @app.get(API + "/stick")
    def api_stick():
        info = service.status.snapshot().get("stick")
        data = dict(info) if isinstance(info, dict) else {"present": False, "adopted": False}
        stick = current_stick()
        runs = []
        if stick is not None:
            runs = [{"name": r.name, "files": r.files, "size": r.size}
                    for r in reversed(stick.run_folders())]
        data["runs"] = runs
        return jsonify(data)

    @app.post(API + "/stick/eject")
    def api_eject():
        if service.busy():
            return error("error.busy", 409)
        try:
            stick_manager.eject()
        except Exception as e:  # noqa: BLE001
            log.warning("eject failed: %s", e)
            return error("error.helper", 500, detail=str(e))
        return jsonify({"ok": True})

    # history ----------------------------------------------------------------------
    @app.get(API + "/history")
    def api_history():
        try:
            limit = int(request.args.get("limit", 100))
            offset = int(request.args.get("offset", 0))
        except ValueError:
            return error("error.invalid", 400)
        limit = max(1, min(limit, 1000))
        offset = max(0, offset)
        return jsonify({"runs": state.runs(limit=limit, offset=offset), "devices": state.devices()})

    # gallery and media ------------------------------------------------------------
    @app.get(API + "/gallery")
    def api_gallery():
        stick = current_stick()
        runs = []
        if stick is not None:
            for run in reversed(stick.run_folders()):
                runs.append({"name": run.name, "items": gallery_items(run.name, _list_run_files(run.path))})
        return jsonify({"runs": runs})

    @app.get("/media/<run>/<name>")
    def media(run, name):
        stick = current_stick()
        if stick is None:
            return error("error.no_stick", 404)
        path = stick.resolve(run, name)
        if path is None:
            return error("error.invalid", 404)
        root = os.path.realpath(stick.root)
        real = os.path.realpath(path)
        if os.path.dirname(os.path.dirname(real)) != root:
            return error("error.invalid", 404)
        try:
            st = os.lstat(path)
        except OSError:
            return error("error.invalid", 404)
        if not stat.S_ISREG(st.st_mode):
            return error("error.invalid", 404)
        inline = request.args.get("inline") in ("1", "true", "yes")
        resp = send_file(path, mimetype=media_type(name), as_attachment=not inline,
                         download_name=name, conditional=True, max_age=0)
        resp.headers["Cache-Control"] = "private, no-cache"
        return resp

    # settings ---------------------------------------------------------------------
    @app.get(API + "/settings")
    def api_settings():
        return jsonify(config.public())

    @app.put(API + "/settings")
    def api_settings_put():
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify({"errors": {"_": "error.invalid"}}), 400
        changes, errors = {}, {}
        new_password = None
        for key, value in body.items():
            if key == "password":
                if value in ("", None):
                    continue
                problem = auth.password_problem(value)
                if problem:
                    errors["password"] = problem
                else:
                    new_password = value
            elif key == "password_hash":
                errors[key] = "error.invalid"      # only set through "password"
            elif key.endswith("_set") and key[:-4] in SECRET_KEYS:
                continue                           # echo of config.public(); ignore
            elif key in SECRET_KEYS and value == "":
                continue                           # empty secret keeps the old value
            else:
                changes[key] = value
        if new_password is not None:
            changes["password_hash"] = auth.hash_password(new_password)
        elif "password" in errors:
            # Validate the rest as if the password had been set, so a too
            # short password is not also reported as "not set".
            changes.setdefault("password_hash", config.get("password_hash") or "pending")
        old_samba = config.get("samba_enabled")
        if errors:
            # Report every problem at once: also validate the other fields.
            merged = config.snapshot()
            merged.update(changes)
            try:
                validate(merged)
            except ConfigError as e:
                for k, v in e.errors.items():
                    errors.setdefault(k, v)
            return jsonify({"errors": errors}), 400
        try:
            config.update(changes)
        except ConfigError as e:
            return jsonify({"errors": e.errors}), 400
        if "password_hash" in changes or (changes.get("login_enabled") and config.get("login_enabled")):
            # The person who set the password or switched login on stays logged in.
            mark_logged_in()
        new_samba = config.get("samba_enabled")
        if new_samba != old_samba:
            try:
                helper.call("samba", enabled=bool(new_samba))
            except Exception as e:  # noqa: BLE001
                log.warning("samba share change failed: %s", e)
                try:
                    config.update({"samba_enabled": old_samba})
                except ConfigError:
                    pass
                return error("error.helper", 500, detail=str(e), settings=config.public())
        return jsonify(config.public())

    # system -----------------------------------------------------------------------
    def power(action):
        body = request.get_json(silent=True)
        force = isinstance(body, dict) and body.get("force") is True
        if service.busy() and not force:
            return error("error.busy", 409)
        try:
            helper.call(action)
        except Exception as e:  # noqa: BLE001
            log.warning("%s failed: %s", action, e)
            return error("error.helper", 500, detail=str(e))
        return jsonify({"ok": True}), 202

    @app.post(API + "/system/shutdown")
    def api_shutdown():
        return power("shutdown")

    @app.post(API + "/system/reboot")
    def api_reboot():
        return power("reboot")

    def sysinfo_value(name, default=None):
        func = getattr(sysinfo, name, None) if sysinfo is not None else None
        if not callable(func):
            return default
        try:
            return func()
        except Exception:  # noqa: BLE001 - degrade, never fail the page
            log.exception("sysinfo.%s failed", name)
            return default

    @app.get(API + "/system")
    def api_system():
        return jsonify({
            "version": __version__,
            "latest_version": sysinfo_value("latest_version"),
            "update_available": bool(sysinfo_value("update_available", False)),
            "throttled": sysinfo_value("throttled"),
            "ntp_synchronised": sysinfo_value("ntp_synchronised"),
            "hostname": socket.gethostname(),
        })

    @app.get(API + "/diagnostics")
    def api_diagnostics():
        path = diagnostics.build(config, state, service=service)
        f = open(path, "rb")  # noqa: SIM115 - closed by the response
        try:
            os.unlink(path)       # the open file stays readable; nothing is left behind
        except OSError:
            pass
        return send_file(f, mimetype="application/zip", as_attachment=True,
                         download_name=diagnostics.download_name(time.time()),
                         conditional=False, etag=False, max_age=0)

    # i18n -------------------------------------------------------------------------
    @app.get(API + "/i18n/<lang>")
    def api_i18n(lang):
        if lang not in LANGUAGES:
            return error("error.invalid", 404)
        resp = jsonify(i18n.catalogue(lang))
        resp.headers["Cache-Control"] = "no-cache"
        return resp

    return app
