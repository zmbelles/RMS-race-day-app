"""Single admin login.

There is one account. Its password lives only as a hash in the ADMIN_PASSWORD_HASH
environment variable, so nothing secret is stored in the code or the database. Make one with:

    python auth.py

Signed out, visitors see the home page, schedule, race results, standings and the driver list.
Everything else needs the login.
"""
import getpass
import hashlib
import hmac
import os
import secrets
import time

from flask import abort, flash, g, redirect, render_template, request, session, url_for
from markupsafe import Markup
from werkzeug.security import check_password_hash, generate_password_hash

def _password_hash():
    """ADMIN_PASSWORD_HASH, or the contents of the file ADMIN_PASSWORD_HASH_FILE names (simpler on a server,
    since the hash contains '$' characters that config files and shells like to expand)."""
    path = os.getenv("ADMIN_PASSWORD_HASH_FILE")
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    return os.getenv("ADMIN_PASSWORD_HASH", "")


PASSWORD_HASH = _password_hash()
PRODUCTION = os.getenv("RMS_ENV", "").lower() == "production"

# Pages anyone can open. Everything else redirects to the login page.
PUBLIC_ENDPOINTS = {"login", "logout", "static", "races", "races_season", "races_championship", "race_detail",
                    "schedule", "standings", "drivers", "dashboard"}

MAX_FAILURES = 5
LOCKOUT_SECONDS = 15 * 60
_failures = {}  # ip -> [count, first_failure_time]; one process is enough for one login


def is_admin():
    # Tie the session to the current hash, so changing the password signs everyone out.
    return session.get("admin") == _fingerprint() and bool(PASSWORD_HASH)


def _fingerprint():
    return hashlib.sha256(PASSWORD_HASH.encode()).hexdigest()[:16]


def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(32)
    return session["csrf"]


def csrf_field():
    return Markup(f'<input type="hidden" name="csrf_token" value="{csrf_token()}">')


def safe_next(target):
    """A redirect target on this site only: a path like '/races/3', never '//host' or '/\\host'."""
    target = (target or "").strip()
    if not target.startswith("/") or target.startswith("//") or "\\" in target:
        return None
    return target


def _client_ip():
    return request.remote_addr or "unknown"


def _locked_out(ip):
    count, first = _failures.get(ip, (0, 0))
    if time.time() - first > LOCKOUT_SECONDS:
        _failures.pop(ip, None)
        return False
    return count >= MAX_FAILURES


def _record_failure(ip):
    count, first = _failures.get(ip, (0, time.time()))
    if time.time() - first > LOCKOUT_SECONDS:
        count, first = 0, time.time()
    _failures[ip] = (count + 1, first)


def init_app(app):
    secret = os.getenv("SECRET_KEY")
    if PRODUCTION:
        missing = [name for name, value in (("SECRET_KEY", secret), ("ADMIN_PASSWORD_HASH", PASSWORD_HASH)) if not value]
        if missing:
            raise RuntimeError(f"Set {' and '.join(missing)} before running in production (see README).")
    app.secret_key = secret or "rms-dev-only"
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=PRODUCTION,  # cookie only travels over HTTPS in production
        PERMANENT_SESSION_LIFETIME=60 * 60 * 24 * 30,  # stay signed in for a race weekend and then some
    )
    if os.getenv("TRUST_PROXY") == "1":
        # Behind a reverse proxy / load balancer that sets X-Forwarded-For and X-Forwarded-Proto.
        from werkzeug.middleware.proxy_fix import ProxyFix
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)

    app.jinja_env.globals.update(csrf_field=csrf_field, is_admin=is_admin)

    @app.before_request
    def guard():
        g.admin = is_admin()
        if request.method == "POST":
            sent = request.form.get("csrf_token", "")
            if not (sent and "csrf" in session and hmac.compare_digest(sent, session["csrf"])):
                abort(400, "Form expired. Go back, refresh the page and try again.")
        if g.admin or request.endpoint in PUBLIC_ENDPOINTS:
            return None
        if request.method != "GET" or request.endpoint == "preview_command":
            abort(403)
        return redirect(url_for("login", next=request.full_path.rstrip("?")))

    @app.after_request
    def security_headers(resp):
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        if PRODUCTION:
            resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
        return resp

    @app.route("/login", methods=["GET", "POST"])
    def login():
        nxt = safe_next(request.values.get("next")) or url_for("dashboard")
        if g.admin:
            return redirect(nxt)
        error = None
        if request.method == "POST":
            ip = _client_ip()
            if _locked_out(ip):
                error = "Too many wrong passwords. Try again in 15 minutes."
            elif PASSWORD_HASH and check_password_hash(PASSWORD_HASH, request.form.get("password", "")):
                _failures.pop(ip, None)
                session.clear()
                session.permanent = True
                session["admin"] = _fingerprint()
                return redirect(nxt)
            else:
                _record_failure(ip)
                time.sleep(0.5)
                error = "Wrong password." if PASSWORD_HASH else "No admin password is set up yet (ADMIN_PASSWORD_HASH)."
        return render_template("login.html", error=error, next=nxt, configured=bool(PASSWORD_HASH))

    @app.post("/logout")
    def logout():
        session.clear()
        flash("Signed out", "secondary")
        return redirect(url_for("dashboard"))


if __name__ == "__main__":
    pw = getpass.getpass("New admin password: ")
    if len(pw) < 10:
        raise SystemExit("Use at least 10 characters.")
    if pw != getpass.getpass("Again: "):
        raise SystemExit("Passwords didn't match.")
    print("\nSet this environment variable (keep the quotes in a shell):\n")
    print(f"ADMIN_PASSWORD_HASH='{generate_password_hash(pw)}'")
