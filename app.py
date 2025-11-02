from __future__ import annotations
from zoneinfo import ZoneInfo

import os
import csv
import io
import re
import math
import unicodedata
from datetime import datetime, timedelta
from functools import wraps
from urllib.parse import urlparse
import uuid
import mimetypes
from pathlib import Path
from werkzeug.utils import secure_filename
from flask import send_from_directory


import requests
from flask import (
    Flask, render_template, request, redirect,
    url_for, flash, abort, jsonify
)
from flask_sqlalchemy import SQLAlchemy
from flask_login import (
    LoginManager, UserMixin, login_user,
    login_required, logout_user, current_user
)
from werkzeug.security import generate_password_hash, check_password_hash
from sqlalchemy import inspect, text

# ======================= App & DB config =======================
app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "dev-change-me")

BASE_DIR = os.path.abspath(os.path.dirname(__file__))

def _compute_db_uri() -> str:
    """Вернёт PostgreSQL URI из env, иначе — SQLite (Railway Volume / локально)."""
    raw = os.getenv("DATABASE_URL")
    if raw:
        # postgres:// → postgresql+psycopg2://
        if raw.startswith("postgres://"):
            raw = raw.replace("postgres://", "postgresql+psycopg2://", 1)
        # Добавим sslmode=require, если его нет (часто требуется на платформах)
        if "sslmode=" not in raw:
            sep = "&" if "?" in raw else "?"
            raw = f"{raw}{sep}sslmode=require"
        return raw

    # Fallback: SQLite (на Railway есть /data — это персистентный volume)
    if os.path.exists("/data"):
        return "sqlite:////data/site.db"
    return "sqlite:///" + os.path.join(BASE_DIR, "site.db")

app.config["SQLALCHEMY_DATABASE_URI"] = _compute_db_uri()
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db = SQLAlchemy(app)

login_manager = LoginManager()
login_manager.login_view = "login"
login_manager.login_message = "Войдите, чтобы продолжить."
login_manager.init_app(app)

# ===== Uploads =====
# Храним файлы в персистентной папке (/data на Railway), локально — ./uploads
if os.path.exists("/data"):
    UPLOAD_DIR = Path("/data/uploads")
else:
    UPLOAD_DIR = Path(BASE_DIR) / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

# Ограничение размера (16 МБ)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024

ALLOWED_EXTS = {
    "pdf", "doc", "docx", "ppt", "pptx", "xls", "xlsx",
    "png", "jpg", "jpeg", "txt", "zip", "rar", "7z"
}
def _allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTS

# ====== Глобальный гейт для гостей ======
PUBLIC_ENDPOINTS = {
    "login",
    "register",
    "static",     # статика
    "not_found",
    "forbidden",
}

@app.before_request
def force_auth_for_all():
    if current_user.is_authenticated:
        return
    endpoint = (request.endpoint or "")
    if endpoint in PUBLIC_ENDPOINTS or endpoint.startswith("static"):
        return
    return redirect(url_for("login", next=request.url))

# ======================= Sheets config =======================
SHEETS = [
    {
        "name": "ИСРПО",
        "url": "https://docs.google.com/spreadsheets/d/1AD1WFu__rigevORuF3n92_hcPZehrHRXSEXjnqP0htc/edit?gid=2031953721#gid=2031953721",
        "sum_until_total": True,
    },
    {
        "name": "Алгоритмы",
        "url": "https://docs.google.com/spreadsheets/d/1bGFiA8Eo_DvWwFljG4edc0qvXsTRar_i4uNBJmT5bsw/edit?gid=36129917#gid=36129917",
        "sum_until_total": True,
    },
    {
        "name": "ДМ",
        "url": "https://docs.google.com/spreadsheets/d/1sT0BfUpBHX-MozJxqwempxtWfdShPHnFTgwDI1q_078/edit?gid=0#gid=0",
        "take_last_total": True,
    },
    {
        "name": "ОП",
        "url": "https://docs.google.com/spreadsheets/d/1GTl2TBVT9YfGlgxQIdV7kKY6a2BRGWOhak8Hak2qGR4/edit?gid=1739698806#gid=1739698806",
        "prefer_total": True,
    },
]
DEADLINE_TYPES = ["кр", "лаба", "дз", "тр", "тест", "коллок"]

# ======================= Models =======================
class User(db.Model, UserMixin):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False, index=True)
    surname = db.Column(db.String(120), nullable=False, default="")
    password_hash = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    is_admin = db.Column(db.Boolean, nullable=False, default=False)

    def set_password(self, pwd: str) -> None:
        self.password_hash = generate_password_hash(pwd)

    def check_password(self, pwd: str) -> bool:
        return check_password_hash(self.password_hash, pwd)

class Deadline(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    due_at = db.Column(db.DateTime, nullable=False)
    all_day = db.Column(db.Boolean, default=False)
    subject = db.Column(db.String(120), nullable=True)
    kind = db.Column(db.String(30), nullable=False, default="дз")
    link = db.Column(db.String(500), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # --- attachment ---
    file_path = db.Column(db.String(500), nullable=True)  # относительный путь/имя на диске
    file_name = db.Column(db.String(255), nullable=True)  # «человеческое» имя
    file_size = db.Column(db.Integer, nullable=True)
    file_mime = db.Column(db.String(120), nullable=True)


@login_manager.user_loader
def load_user(user_id: str):
    return User.query.get(int(user_id))

# ======================= Helpers =======================
def _save_upload(fs) -> tuple[str, str, int, str] | None:
    """
    Сохраняет FileStorage fs в UPLOAD_DIR с уникальным именем.
    Возвращает (stored_name, original_name, size, mime) или None.
    """
    if not fs or fs.filename == "":
        return None
    if not _allowed_file(fs.filename):
        return None
    orig = secure_filename(fs.filename)
    ext = orig.rsplit(".", 1)[1].lower()
    stored = f"{uuid.uuid4().hex}.{ext}"
    target = UPLOAD_DIR / stored
    fs.save(target)
    size = target.stat().st_size
    mime = mimetypes.guess_type(orig)[0] or "application/octet-stream"
    return (stored, orig, size, mime)

def _remove_upload(stored_name: str | None):
    if not stored_name:
        return
    try:
        (UPLOAD_DIR / stored_name).unlink(missing_ok=True)
    except Exception:
        pass

def admin_required(view):
    @wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if not current_user.is_admin:
            abort(403)
        return view(*args, **kwargs)
    return wrapped

def gsheet_to_csv_url(edit_url: str) -> str:
    m = re.search(r"/spreadsheets/d/([^/]+)/", edit_url)
    if not m:
        raise ValueError("Некорректная ссылка на Google Sheets")
    doc_id = m.group(1)
    m2 = re.search(r"[?&]gid=(\d+)", edit_url)
    gid = m2.group(1) if m2 else "0"
    return f"https://docs.google.com/spreadsheets/d/{doc_id}/export?format=csv&gid={gid}"

def fetch_csv_rows(csv_url: str) -> list[list[str]]:
    r = requests.get(csv_url, timeout=20)
    r.raise_for_status()
    data = r.content.decode("utf-8", errors="ignore")
    reader = csv.reader(io.StringIO(data))
    return [row for row in reader]

def _norm(s: str) -> str:
    s = (s or "")
    s = unicodedata.normalize("NFKC", s)
    s = s.replace("\xa0", " ")
    return re.sub(r"\s+", " ", s.strip().lower())

def _norm_name(s: str) -> str:
    s = _norm(s).replace("ё", "е")
    keep = []
    for ch in s:
        if "а" <= ch <= "я" or ch == " ":
            keep.append(ch)
    return re.sub(r"\s+", " ", "".join(keep)).strip()

def _safe_number(cell: str):
    s = (cell or "").strip().replace(",", ".")
    try:
        v = float(s)
    except ValueError:
        return None
    if not math.isfinite(v):
        return None
    if abs(v) >= 100000:
        return None
    return v

def _find_header_row(rows: list[list[str]]) -> int:
    for i, row in enumerate(rows[:10]):
        n = [_norm(c) for c in row]
        if not row:
            continue
        if any(k in (n[0] if n else "") for k in ("фио", "студент", "фамилия")):
            return i
        if any("итог" in c or "total" in c for c in n):
            return i
        if any(re.search(r"\bлр\s*\d+", c) for c in n):
            return i
    for i, row in enumerate(rows):
        if any((c or "").strip() for c in row):
            return i
    return 0

def _find_preferred_total_index(headers: list[str]) -> int | None:
    if not headers:
        return None
    ranks: list[tuple[int,int]] = []
    for j, h in enumerate(headers):
        hj = _norm(h)
        if not hj:
            continue
        if hj == "итог" or re.fullmatch(r"итог\s*\([^)]*\)", hj) or hj == "total":
            ranks.append((1, j))
        elif hj.startswith("итог ") or hj.startswith("total "):
            ranks.append((2, j))
        elif "итог" in hj or "total" in hj:
            ranks.append((3, j))
    if not ranks:
        return None
    best = min(r for r, _ in ranks)
    candidates = [idx for r, idx in ranks if r == best]
    return max(candidates)

def find_score_by_surname(
    rows: list[list[str]],
    surname: str,
    prefer_total: bool = False,
    sum_until_total: bool = False,
    take_last_total: bool = False,
) -> dict | None:
    if not rows:
        return None

    hdr_idx = _find_header_row(rows)
    headers = rows[hdr_idx] if hdr_idx < len(rows) else []

    total_idx = _find_preferred_total_index(headers)
    stop_at = total_idx if total_idx is not None else (len(headers) if headers else 10**9)

    target = _norm_name(surname)
    row_idx = None
    for i, row in enumerate(rows[hdr_idx + 1:], start=hdr_idx + 1):
        if not row:
            continue
        row_text = _norm_name(" ".join((c or "") for c in row))
        if target and target in row_text:
            row_idx = i
            break
    if row_idx is None:
        return None

    row = rows[row_idx]

    if take_last_total:
        for j in range(len(row) - 1, -1, -1):
            v = _safe_number(row[j])
            if v is not None:
                return {"sum": v, "values": [v], "row": row}
        return None

    def _sum_left_until_total() -> dict:
        end = min(stop_at, len(row))
        vals = []
        for j in range(0, end):
            v = _safe_number(row[j])
            if v is not None:
                vals.append(v)
        return {"sum": round(sum(vals), 3), "values": vals, "row": row}

    if prefer_total and total_idx is not None and total_idx < len(row):
        v = _safe_number(row[total_idx])
        if v is not None:
            return {"sum": v, "values": [v], "row": row}
        return _sum_left_until_total()

    if sum_until_total:
        return _sum_left_until_total()

    return _sum_left_until_total()

# ======================= Pages =======================
@app.route("/")
@login_required
def home():
    # --- UTC для запросов в БД (как было) ---
    utc_now = datetime.utcnow()
    horizon = utc_now + timedelta(days=10)
    upcoming = (
        Deadline.query
        .filter(Deadline.due_at >= utc_now, Deadline.due_at <= horizon)
        .order_by(Deadline.due_at.asc())
        .all()
    )

    # --- Локальное время для Питера/Москвы (UTC+3) ---
    now = datetime.now(ZoneInfo("Europe/Moscow"))

    # Приветствие по локальному часу
    hour = now.hour
    if 5 <= hour < 12:
        greet = "Доброе утро"
    elif 12 <= hour < 18:
        greet = "Добрый день"
    elif 18 <= hour < 24:
        greet = "Добрый вечер"
    else:
        greet = "Доброй ночи"

    # Обращение по username (без фамилии)
    uname = current_user.username

    # В шаблон отдаём локальное now, чтобы дата отображалась по МСК
    return render_template("index.html", upcoming=upcoming, now=now, greet=greet, uname=uname)

@app.get("/subjects")
@login_required
def subjects_page():
    return render_template("subjects.html")

@app.get("/calendar")
@login_required
def calendar_page():
    return render_template("calendar.html")

def _format_deadline_title(d: Deadline) -> str:
    tag = f"[{d.kind}]" if d.kind else ""
    if d.subject:
        return f"{tag} {d.subject}: {d.title}".strip()
    return f"{tag} {d.title}".strip()

def _clean_url(u: str | None) -> str | None:
    if not u:
        return None
    u = u.strip()
    if not u:
        return None
    try:
        p = urlparse(u)
    except Exception:
        return None
    if p.scheme in ("http", "https") and p.netloc:
        return u[:500]
    return None

def _slug(s: str) -> str:
    return (s or "").strip().lower().replace("ё","е").replace(" ", "-")

@app.get("/events")
@login_required
def events_feed():
    items = Deadline.query.order_by(Deadline.due_at.asc()).all()

    def fmt_dt(dt): return dt.strftime("%Y-%m-%dT%H:%M:%S")
    def fmt_d(d):   return d.strftime("%Y-%m-%d")

    payload = []
    for d in items:
        if d.all_day:
            start = fmt_d(d.due_at)
            end   = fmt_d(d.due_at + timedelta(days=1))
        else:
            start = fmt_dt(d.due_at)
            end   = fmt_dt(d.due_at + timedelta(minutes=1))

        payload.append({
            "id": d.id,
            "title": _format_deadline_title(d),
            "start": start,
            "end": end,
            "allDay": bool(d.all_day),
            "classNames": [f"kind-{_slug(d.kind)}"],
            "extendedProps": {
                "subject": d.subject,
                "kind": d.kind,
                "rawTitle": d.title,
                "link": d.link,
            },
            **({"url": d.link} if d.link else {}),
        })
    return jsonify(payload)

# ======================= Admin =======================
@app.get("/admin")
@admin_required
def admin_panel():
    return render_template("admin.html")

@app.get("/admin/users")
@admin_required
def admin_users():
    users = User.query.order_by(User.created_at.desc()).all()
    return render_template("admin_users.html", users=users)

@app.post("/admin/users/<int:user_id>/promote")
@admin_required
def admin_user_promote(user_id):
    u = User.query.get_or_404(user_id)
    if not u.is_admin:
        u.is_admin = True
        db.session.commit()
        flash(f"{u.username} — теперь админ ✅", "success")
    else:
        flash("Пользователь уже админ", "error")
    return redirect(url_for("admin_users"))

@app.post("/admin/users/<int:user_id>/demote")
@admin_required
def admin_user_demote(user_id):
    u = User.query.get_or_404(user_id)
    if not u.is_admin:
        flash("Пользователь и так не админ", "error")
        return redirect(url_for("admin_users"))
    if u.id == current_user.id:
        flash("Нельзя снять админа с самого себя", "error")
        return redirect(url_for("admin_users"))
    if User.query.filter_by(is_admin=True).count() <= 1:
        flash("Нельзя снять статус с последнего админа", "error")
        return redirect(url_for("admin_users"))
    u.is_admin = False
    db.session.commit()
    flash(f"{u.username} больше не админ", "success")
    return redirect(url_for("admin_users"))

@app.post("/admin/users/<int:user_id>/delete")
@admin_required
def admin_user_delete(user_id):
    u = User.query.get_or_404(user_id)
    if u.id == current_user.id:
        flash("Нельзя удалить свой аккаунт", "error")
        return redirect(url_for("admin_users"))
    if u.is_admin and User.query.filter_by(is_admin=True).count() <= 1:
        flash("Нельзя удалить последнего админа", "error")
        return redirect(url_for("admin_users"))
    db.session.delete(u)
    db.session.commit()
    flash(f"Пользователь {u.username} удалён", "success")
    return redirect(url_for("admin_users"))

@app.route("/admin/deadlines/add", methods=["GET", "POST"])
@admin_required
def admin_add_deadline():
    if request.method == "POST":
        title = request.form["title"].strip()
        date = request.form["date"].strip()
        time_ = request.form.get("time", "").strip()
        all_day = bool(request.form.get("all_day"))
        subject = (request.form.get("subject") or "").strip() or None
        kind = (request.form.get("kind") or "дз").strip().lower()
        link = _clean_url(request.form.get("link"))
        if kind not in DEADLINE_TYPES:
            kind = "дз"

        if not title or not date:
            flash("Название и дата — обязательны", "error")
            return redirect(url_for("admin_add_deadline"))

        if all_day or not time_:
            due_at = datetime.strptime(date, "%Y-%m-%d")
        else:
            due_at = datetime.strptime(f"{date} {time_}", "%Y-%m-%d %H:%M")

        # Файл
        fs = request.files.get("attachment")
        stored = None
        orig = None
        size = None
        mime = None
        if fs and fs.filename:
            saved = _save_upload(fs)
            if saved:
                stored, orig, size, mime = saved
            else:
                flash("Недопустимый тип файла", "error")

        d = Deadline(
            title=title, due_at=due_at, all_day=all_day,
            subject=subject, kind=kind, link=link,
            file_path=stored, file_name=orig, file_size=size, file_mime=mime
        )
        db.session.add(d)
        db.session.commit()
        flash("Дедлайн добавлен ✅", "success")
        return redirect(url_for("admin_deadlines_list"))

    return render_template("admin_add_deadline.html", edit=False, DEADLINE_TYPES=DEADLINE_TYPES)

@app.get("/admin/deadlines")
@admin_required
def admin_deadlines_list():
    items = Deadline.query.order_by(Deadline.due_at.asc()).all()
    return render_template("admin_deadlines.html", items=items)

@app.route("/admin/deadlines/<int:deadline_id>/edit", methods=["GET", "POST"])
@admin_required
def admin_deadline_edit(deadline_id):
    d = Deadline.query.get_or_404(deadline_id)
    if request.method == "POST":
        title = request.form["title"].strip()
        date = request.form["date"].strip()
        time_ = request.form.get("time", "").strip()
        all_day = bool(request.form.get("all_day"))
        subject = (request.form.get("subject") or "").strip() or None
        kind = (request.form.get("kind") or d.kind or "дз").strip().lower()
        link = _clean_url(request.form.get("link"))
        if kind not in DEADLINE_TYPES:
            kind = d.kind or "дз"

        if not title or not date:
            flash("Название и дата — обязательны", "error")
            return redirect(url_for("admin_deadline_edit", deadline_id=deadline_id))

        if all_day or not time_:
            due_at = datetime.strptime(date, "%Y-%m-%d")
        else:
            due_at = datetime.strptime(f"{date} {time_}", "%Y-%m-%d %H:%M")

        # Удаление старого файла (если отмечено)
        if request.form.get("remove_attachment") == "1":
            _remove_upload(d.file_path)
            d.file_path = d.file_name = d.file_mime = None
            d.file_size = None

        # Загрузка нового файла (пришёл — значит заменить)
        fs = request.files.get("attachment")
        if fs and fs.filename:
            saved = _save_upload(fs)
            if saved:
                # удалить старый
                _remove_upload(d.file_path)
                stored, orig, size, mime = saved
                d.file_path, d.file_name, d.file_size, d.file_mime = stored, orig, size, mime
            else:
                flash("Недопустимый тип файла", "error")

        d.title = title
        d.due_at = due_at
        d.all_day = all_day
        d.subject = subject
        d.kind = kind
        d.link = link
        db.session.commit()
        flash("Дедлайн обновлён ✅", "success")
        return redirect(url_for("admin_deadlines_list"))

    date_val = d.due_at.strftime("%Y-%m-%d")
    time_val = ("" if d.all_day else d.due_at.strftime("%H:%M"))
    return render_template(
        "admin_add_deadline.html",
        edit=True, d=d, date_val=date_val, time_val=time_val,
        DEADLINE_TYPES=DEADLINE_TYPES
    )

@app.post("/admin/deadlines/<int:deadline_id>/delete")
@admin_required
def admin_deadline_delete(deadline_id):
    d = Deadline.query.get_or_404(deadline_id)
    db.session.delete(d)
    db.session.commit()
    flash("Дедлайн удалён 🗑️", "success")
    return redirect(url_for("admin_deadlines_list"))

# ======================= Auth =======================
@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form["username"].strip().lower()
        surname = request.form["surname"].strip()
        password = request.form["password"]
        password2 = request.form["password2"]

        if not username or not surname or not password:
            flash("Заполните все поля", "error")
            return redirect(url_for("register"))
        if password != password2:
            flash("Пароли не совпадают", "error")
            return redirect(url_for("register"))
        if User.query.filter_by(username=username).first():
            flash("Такой логин уже занят", "error")
            return redirect(url_for("register"))

        user = User(username=username, surname=surname)
        user.set_password(password)
        db.session.add(user)
        db.session.flush()  # получить user.id без полного commit

        # первый зарегистрированный = админ
        if User.query.filter_by(is_admin=True).count() == 0:
            user.is_admin = True

        db.session.commit()
        flash("Аккаунт создан. Войдите.", "success")
        return redirect(url_for("login"))

    return render_template("auth/register.html")

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form["username"].strip().lower()
        password = request.form["password"]
        user = User.query.filter_by(username=username).first()
        if user and user.check_password(password):
            login_user(user, remember=True)
            flash("Добро пожаловать!", "success")
            next_url = request.args.get("next")
            return redirect(next_url or url_for("home"))
        flash("Неверный логин или пароль", "error")
    return render_template("auth/login.html")

@app.route("/attach/<path:fname>")
def download_attachment(fname):
    return send_from_directory("uploads", fname, as_attachment=True)

@app.get("/uploads/<path:fname>")
@login_required
def download_attachment(fname):
    # Безопасная выдача только тех файлов, которые реально привязаны к дедлайнам
    dl = Deadline.query.filter_by(file_path=fname).first()
    if not dl:
        abort(404)
    return send_from_directory(
        directory=str(UPLOAD_DIR),
        path=fname,
        as_attachment=True,
        download_name=dl.file_name or fname
    )

@app.get("/logout")
@login_required
def logout():
    logout_user()
    flash("Вы вышли из аккаунта", "success")
    return redirect(url_for("login"))

# ======================= API =======================
@app.get("/api/scores")
@login_required
def api_scores():
    results = []
    surname = current_user.surname
    for sheet in SHEETS:
        try:
            csv_url = gsheet_to_csv_url(sheet["url"])
            rows = fetch_csv_rows(csv_url)
            found = find_score_by_surname(
                rows,
                surname,
                prefer_total=sheet.get("prefer_total", False),
                sum_until_total=sheet.get("sum_until_total", False),
                take_last_total=sheet.get("take_last_total", False),
            )
            if found:
                results.append({
                    "name": sheet["name"],
                    "score": round(found["sum"], 3),
                    "ok": True
                })
            else:
                results.append({"name": sheet["name"], "score": None, "ok": False})
        except Exception as e:
            results.append({"name": sheet["name"], "score": None, "ok": False, "error": str(e)})
    return jsonify({"surname": surname, "items": results, "ts": datetime.utcnow().isoformat(timespec="seconds")})

# ======================= Errors =======================
@app.errorhandler(403)
def forbidden(e):
    return render_template("errors/403.html"), 403

@app.errorhandler(404)
def not_found(e):
    return render_template("errors/404.html"), 404

# ======================= DB init / light migrations =======================
with app.app_context():
    db.create_all()
    insp = inspect(db.engine)

    cols = {c["name"] for c in insp.get_columns("user")}
    if "surname" not in cols:
        db.session.execute(text("ALTER TABLE user ADD COLUMN surname VARCHAR(120) NOT NULL DEFAULT ''"))
        db.session.commit()
    if "is_admin" not in cols:
        db.session.execute(text("ALTER TABLE user ADD COLUMN is_admin BOOLEAN NOT NULL DEFAULT 0"))
        db.session.commit()

    dcols = {c["name"] for c in insp.get_columns("deadline")}
    if "kind" not in dcols:
        db.session.execute(text("ALTER TABLE deadline ADD COLUMN kind VARCHAR(30) NOT NULL DEFAULT 'дз'"))
        db.session.commit()
    if "link" not in dcols:
        db.session.execute(text("ALTER TABLE deadline ADD COLUMN link VARCHAR(500)"))
        db.session.commit()
    # Новые поля для вложений:
    if "file_path" not in dcols:
        db.session.execute(text("ALTER TABLE deadline ADD COLUMN file_path VARCHAR(500)"))
        db.session.commit()
    if "file_name" not in dcols:
        db.session.execute(text("ALTER TABLE deadline ADD COLUMN file_name VARCHAR(255)"))
        db.session.commit()
    if "file_size" not in dcols:
        db.session.execute(text("ALTER TABLE deadline ADD COLUMN file_size INTEGER"))
        db.session.commit()
    if "file_mime" not in dcols:
        db.session.execute(text("ALTER TABLE deadline ADD COLUMN file_mime VARCHAR(120)"))
        db.session.commit()

# ======================= Entry =======================
if __name__ == "__main__":
    app.run(debug=True)
