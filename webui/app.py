import os
import time
from collections import defaultdict
from datetime import timedelta

from flask import (
    Flask, abort, flash, g, redirect, render_template, request, session, url_for,
)
from flask_login import (
    LoginManager, UserMixin, current_user, login_required, login_user, logout_user,
)
from flask_wtf import CSRFProtect
from werkzeug.security import check_password_hash, generate_password_hash

import models
from forms import ChangePasswordForm, LoginForm, RecordForm

_csrf = CSRFProtect()
_login_manager = LoginManager()

# In-memory brute-force guard: ip → (failure_count, first_failure_ts)
_login_attempts: dict = defaultdict(lambda: [0, 0.0])
_MAX_ATTEMPTS = 5
_LOCKOUT_SECS = 300


class _User(UserMixin):
    def __init__(self, row):
        self.id = row["id"]
        self.username = row["username"]
        self.is_admin = bool(row["is_admin"])


def create_app():
    app = Flask(__name__)

    secret = os.environ.get("SECRET_KEY", "")
    if not secret or len(secret) < 32:
        raise RuntimeError(
            "SECRET_KEY env var is missing or too short (need ≥ 32 chars). "
            "Run deploy.sh to generate one."
        )
    app.config["SECRET_KEY"] = secret
    app.config["WTF_CSRF_ENABLED"] = True
    app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(hours=8)
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

    _csrf.init_app(app)
    _login_manager.init_app(app)
    _login_manager.login_view = "login"
    _login_manager.login_message = "Please log in."
    _login_manager.login_message_category = "warning"

    models.init_db()

    # ── DB per-request ──────────────────────────────────────────────────────

    def _db():
        if "db" not in g:
            g.db = models.get_db()
        return g.db

    @app.teardown_appcontext
    def _close_db(exc):
        db = g.pop("db", None)
        if db:
            db.close()

    # ── Auth helpers ────────────────────────────────────────────────────────

    @_login_manager.user_loader
    def _load_user(user_id):
        conn = models.get_db()
        row = models.get_user_by_id(conn, int(user_id))
        conn.close()
        return _User(row) if row else None

    def _rate_ok(ip: str) -> bool:
        count, first_ts = _login_attempts[ip]
        if count >= _MAX_ATTEMPTS:
            if time.time() - first_ts < _LOCKOUT_SECS:
                return False
            _login_attempts[ip] = [0, 0.0]
        return True

    def _record_fail(ip: str):
        entry = _login_attempts[ip]
        if entry[0] == 0:
            entry[1] = time.time()
        entry[0] += 1

    # ── Routes ──────────────────────────────────────────────────────────────

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if current_user.is_authenticated:
            return redirect(url_for("records_list"))
        form = LoginForm()
        if form.validate_on_submit():
            ip = request.remote_addr
            if not _rate_ok(ip):
                flash("Too many failed attempts. Try again in 5 minutes.", "danger")
                return render_template("login.html", form=form)
            row = models.get_user_by_username(_db(), form.username.data)
            if row and check_password_hash(row["password"], form.password.data):
                _login_attempts.pop(ip, None)
                login_user(_User(row))
                session.permanent = True
                return redirect(url_for("records_list"))
            _record_fail(ip)
            flash("Invalid username or password.", "danger")
        return render_template("login.html", form=form)

    @app.route("/logout")
    @login_required
    def logout():
        logout_user()
        flash("Logged out.", "info")
        return redirect(url_for("login"))

    @app.route("/")
    @login_required
    def index():
        return redirect(url_for("records_list"))

    @app.route("/records")
    @login_required
    def records_list():
        page = max(1, request.args.get("page", 1, type=int))
        records, total = models.get_records_page(_db(), page)
        total_pages = max(1, (total + 49) // 50)
        return render_template(
            "records/list.html",
            records=records,
            page=page,
            total_pages=total_pages,
            total=total,
        )

    @app.route("/records/add", methods=["GET", "POST"])
    @login_required
    def records_add():
        form = RecordForm()
        if form.validate_on_submit():
            try:
                models.add_record(
                    _db(),
                    form.name.data,
                    form.type.data,
                    form.value.data.strip(),
                    form.ttl.data,
                    form.priority.data,
                    form.enabled.data,
                )
                flash(f"Record {form.name.data} ({form.type.data}) added.", "success")
                return redirect(url_for("records_list"))
            except Exception as exc:
                if "UNIQUE constraint" in str(exc):
                    flash("A record with this name/type/value already exists.", "danger")
                else:
                    flash(f"Error: {exc}", "danger")
        return render_template("records/add.html", form=form)

    @app.route("/records/<int:record_id>/edit", methods=["GET", "POST"])
    @login_required
    def records_edit(record_id):
        record = models.get_record(_db(), record_id)
        if record is None:
            abort(404)
        form = RecordForm()
        if request.method == "GET":
            form.name.data = record["name"]
            form.type.data = record["type"]
            form.value.data = record["value"]
            form.ttl.data = record["ttl"]
            form.priority.data = record["priority"]
            form.enabled.data = bool(record["enabled"])
        if form.validate_on_submit():
            try:
                models.update_record(
                    _db(),
                    record_id,
                    form.name.data,
                    form.type.data,
                    form.value.data.strip(),
                    form.ttl.data,
                    form.priority.data,
                    form.enabled.data,
                )
                flash("Record updated.", "success")
                return redirect(url_for("records_list"))
            except Exception as exc:
                if "UNIQUE constraint" in str(exc):
                    flash("A record with this name/type/value already exists.", "danger")
                else:
                    flash(f"Error: {exc}", "danger")
        return render_template("records/edit.html", form=form, record=record)

    @app.route("/records/<int:record_id>/delete", methods=["POST"])
    @login_required
    def records_delete(record_id):
        record = models.get_record(_db(), record_id)
        if record is None:
            abort(404)
        models.delete_record(_db(), record_id)
        flash("Record deleted.", "success")
        return redirect(url_for("records_list"))

    @app.route("/records/<int:record_id>/toggle", methods=["POST"])
    @login_required
    def records_toggle(record_id):
        record = models.get_record(_db(), record_id)
        if record is None:
            abort(404)
        models.toggle_record(_db(), record_id)
        flash("Record " + ("disabled." if record["enabled"] else "enabled."), "success")
        return redirect(url_for("records_list"))

    @app.route("/admin/password", methods=["GET", "POST"])
    @login_required
    def admin_password():
        form = ChangePasswordForm()
        if form.validate_on_submit():
            row = models.get_user_by_id(_db(), current_user.id)
            if not check_password_hash(row["password"], form.current_password.data):
                flash("Current password is incorrect.", "danger")
            else:
                models.update_password(
                    _db(), current_user.id,
                    generate_password_hash(form.new_password.data),
                )
                flash("Password changed successfully.", "success")
                return redirect(url_for("records_list"))
        return render_template("admin/password.html", form=form)

    return app


if __name__ == "__main__":
    create_app().run(host="0.0.0.0", port=5380, debug=False)
