#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Агро-калькулятор посевных — сайт с личным кабинетом.

Только стандартная библиотека Python 3.10+: HTTP-сервер, SQLite, сессии
в cookie, пароли через PBKDF2. Никаких зависимостей и сборки.

Запуск:
    python app.py                 # http://127.0.0.1:8765
    python app.py --port 9000
    python app.py --host 0.0.0.0  # доступ из локальной сети

Демо-доступ: demo@agro.ru / demo12345
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import sqlite3
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

BASE = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("DB_PATH", str(BASE / "data.db")))
TEMPLATES = BASE / "templates"
STATIC = BASE / "static"

SESSION_COOKIE = "agro_sid"
SESSION_TTL = 7 * 24 * 3600  # неделя
PBKDF2_ITERATIONS = 200_000

# Справочник культур: масса 1000 семян (г) и рекомендуемая норма высева
# (млн всхожих семян на гектар) — типовые значения для средней полосы.
CROPS = {
    "Пшеница озимая": {"m1000": 40.0, "density": 5.0},
    "Пшеница яровая": {"m1000": 38.0, "density": 6.0},
    "Ячмень яровой": {"m1000": 42.0, "density": 4.5},
    "Овёс": {"m1000": 35.0, "density": 5.0},
    "Кукуруза на зерно": {"m1000": 300.0, "density": 0.07},
    "Подсолнечник": {"m1000": 60.0, "density": 0.06},
    "Рапс яровой": {"m1000": 4.0, "density": 1.2},
    "Соя": {"m1000": 150.0, "density": 0.6},
    "Горох": {"m1000": 200.0, "density": 1.2},
    "Свёкла сахарная": {"m1000": 20.0, "density": 0.12},
}


# --------------------------------------------------------------------------- #
#  База данных
# --------------------------------------------------------------------------- #

def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    with connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                email         TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                created_at    REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS sessions (
                token      TEXT PRIMARY KEY,
                user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                csrf       TEXT NOT NULL,
                created_at REAL NOT NULL,
                expires_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS calculations (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                culture     TEXT NOT NULL,
                area        REAL NOT NULL,
                density     REAL NOT NULL,
                m1000       REAL NOT NULL,
                germination REAL NOT NULL,
                purity      REAL NOT NULL,
                price       REAL NOT NULL,
                rate_kg_ha  REAL NOT NULL,
                total_kg    REAL NOT NULL,
                cost        REAL NOT NULL,
                created_at  REAL NOT NULL
            );
            """
        )


def seed_demo_user() -> None:
    with connect() as conn:
        row = conn.execute("SELECT 1 FROM users WHERE email = ?", ("demo@agro.ru",)).fetchone()
        if row:
            return
        conn.execute(
            "INSERT INTO users (email, password_hash, created_at) VALUES (?, ?, ?)",
            ("demo@agro.ru", hash_password("demo12345"), time.time()),
        )


# --------------------------------------------------------------------------- #
#  Пароли и сессии
# --------------------------------------------------------------------------- #

def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt), PBKDF2_ITERATIONS
    ).hex()
    return f"pbkdf2${PBKDF2_ITERATIONS}${salt}${digest}"


def check_password(password: str, stored: str) -> bool:
    try:
        scheme, iterations, salt, digest = stored.split("$", 3)
        if scheme != "pbkdf2":
            return False
        candidate = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt), int(iterations)
        ).hex()
        return hmac.compare_digest(candidate, digest)
    except (ValueError, TypeError):
        return False


def create_session(user_id: int) -> tuple[str, str]:
    token = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(24)
    now = time.time()
    with connect() as conn:
        conn.execute(
            "INSERT INTO sessions (token, user_id, csrf, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (token, user_id, csrf, now, now + SESSION_TTL),
        )
        conn.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))
    return token, csrf


# --------------------------------------------------------------------------- #
#  Агрономический расчёт
# --------------------------------------------------------------------------- #

def calculate(data: dict) -> dict:
    """Норма высева в кг/га: НВ(млн/га) × М1000(г) × 100 / посевная годность.

    Посевная годность, % = всхожесть, % × чистота, % / 100.
    """
    area = max(0.0, float(data["area"]))
    density = max(0.0, float(data["density"]))
    m1000 = max(0.0, float(data["m1000"]))
    germination = min(100.0, max(0.1, float(data["germination"])))
    purity = min(100.0, max(0.1, float(data["purity"])))
    price = max(0.0, float(data["price"]))

    field_germination = germination * purity / 100
    rate_kg_ha = density * m1000 * 100 / field_germination
    total_kg = rate_kg_ha * area
    cost = total_kg * price

    return {
        "field_germination": round(field_germination, 2),
        "rate_kg_ha": round(rate_kg_ha, 1),
        "total_kg": round(total_kg, 1),
        "total_t": round(total_kg / 1000, 3),
        "cost": round(cost, 0),
        "cost_per_ha": round(cost / area, 0) if area else 0.0,
        "seeds_per_m2": round(density * 100, 1),
    }


# --------------------------------------------------------------------------- #
#  HTML-шаблоны
# --------------------------------------------------------------------------- #

def render(name: str, **context) -> str:
    text = (TEMPLATES / name).read_text(encoding="utf-8")
    for key, value in context.items():
        text = text.replace("{{" + key + "}}", str(value))
    return text


def page(title: str, content: str, *, user=None) -> str:
    if user:
        nav = (
            f'<span class="who">{user["email"]}</span>'
            '<form method="post" action="/logout" class="inline">'
            f'<input type="hidden" name="csrf" value="{user["csrf"]}">'
            '<button class="link" type="submit">Выйти</button></form>'
        )
    else:
        nav = '<a class="link" href="/login">Войти</a><a class="btn btn-sm" href="/register">Создать аккаунт</a>'
    return render("base.html", title=title, content=content, nav=nav)


def money(value: float) -> str:
    return f"{value:,.0f}".replace(",", "\u00A0")


def render_history(rows) -> str:
    if not rows:
        return '<p class="empty">Пока пусто. Рассчитайте норму высева и нажмите «Сохранить расчёт».</p>'
    items = []
    for row in rows:
        date = time.strftime("%d.%m.%Y %H:%M", time.localtime(row["created_at"]))
        items.append(
            f"""
            <tr>
              <td class="muted">{date}</td>
              <td>{html.escape(row["culture"])}</td>
              <td class="num">{row["area"]:g} га</td>
              <td class="num">{row["rate_kg_ha"]:g} кг/га</td>
              <td class="num">{row["total_kg"]:g} кг</td>
              <td class="num">{money(row["cost"])} ₽</td>
              <td class="right">
                <form method="post" action="/app/delete" class="inline">
                  <input type="hidden" name="csrf" value="{row["csrf"]}">
                  <input type="hidden" name="id" value="{row["id"]}">
                  <button class="icon-btn" type="submit" title="Удалить">✕</button>
                </form>
              </td>
            </tr>"""
        )
    return (
        '<div class="table-wrap"><table class="history">'
        "<thead><tr><th>Дата</th><th>Культура</th><th>Площадь</th>"
        "<th>Норма</th><th>Семян</th><th>Стоимость</th><th></th></tr></thead>"
        f"<tbody>{''.join(items)}</tbody></table></div>"
    )


# --------------------------------------------------------------------------- #
#  HTTP-обработчик
# --------------------------------------------------------------------------- #

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$")


class Handler(BaseHTTPRequestHandler):
    server_version = "AgroCalc/1.0"

    # -- низкоуровневые ответы ---------------------------------------------- #

    def _send(self, status: int, body: bytes, content_type: str, headers=None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _html(self, status: int, text: str, headers=None) -> None:
        self._send(status, text.encode("utf-8"), "text/html; charset=utf-8", headers)

    def _json(self, status: int, payload: dict) -> None:
        self._send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _redirect(self, location: str, status: int = HTTPStatus.SEE_OTHER) -> None:
        self._send(status, b"", "text/plain; charset=utf-8", {"Location": location})

    def _error(self, status: int, message: str) -> None:
        content = render("error.html", code=status, message=message)
        self._html(status, page("Ошибка", content))

    # -- разбор запроса ------------------------------------------------------ #

    def _cookies(self) -> dict:
        header = self.headers.get("Cookie", "")
        result = {}
        for chunk in header.split(";"):
            if "=" in chunk:
                key, _, value = chunk.strip().partition("=")
                result[key] = value
        return result

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def _form(self) -> dict:
        raw = self._read_body().decode("utf-8", "replace")
        return {k: v[0] for k, v in parse_qs(raw, keep_blank_values=True).items()}

    def _json_body(self) -> dict:
        raw = self._read_body().decode("utf-8", "replace")
        try:
            data = json.loads(raw or "{}")
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}

    def _session(self):
        token = self._cookies().get(SESSION_COOKIE)
        if not token:
            return None
        with connect() as conn:
            row = conn.execute(
                "SELECT s.token, s.csrf, s.expires_at, u.id AS user_id, u.email "
                "FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.token = ?",
                (token,),
            ).fetchone()
        if not row or row["expires_at"] < time.time():
            return None
        return {
            "token": row["token"],
            "csrf": row["csrf"],
            "user_id": row["user_id"],
            "email": row["email"],
        }

    def _check_csrf(self, session, token: str) -> bool:
        return bool(session) and bool(token) and hmac.compare_digest(session["csrf"], token)

    def _set_session_cookie(self, token: str) -> dict:
        # За прокси Render сайт работает по HTTPS — помечаем cookie Secure.
        secure = "; Secure" if self.headers.get("X-Forwarded-Proto") == "https" else ""
        return {
            "Set-Cookie":
                f"{SESSION_COOKIE}={token}; Path=/; HttpOnly; SameSite=Lax; "
                f"Max-Age={SESSION_TTL}{secure}"
        }

    # -- GET ---------------------------------------------------------------- #

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        user = self._session()

        if path == "/healthz":
            return self._send(HTTPStatus.OK, b"ok", "text/plain; charset=utf-8")

        if path == "/":
            content = render("landing.html", crops=len(CROPS))
            return self._html(HTTPStatus.OK, page("Агро-калькулятор посевных", content, user=user))

        if path in ("/login", "/register"):
            if user:
                return self._redirect("/app")
            name = "login.html" if path == "/login" else "register.html"
            content = render(name, error="", email="")
            title = "Вход" if path == "/login" else "Регистрация"
            return self._html(HTTPStatus.OK, page(title, content, user=user))

        if path == "/app":
            if not user:
                return self._redirect("/login")
            with connect() as conn:
                rows = conn.execute(
                    "SELECT * FROM calculations WHERE user_id = ? "
                    "ORDER BY created_at DESC LIMIT 20",
                    (user["user_id"],),
                ).fetchall()
            history = render_history([dict(r, csrf=user["csrf"]) for r in rows])
            content = render(
                "app.html",
                email=user["email"],
                csrf=user["csrf"],
                crops_json=json.dumps(CROPS, ensure_ascii=False),
                history=history,
            )
            return self._html(HTTPStatus.OK, page("Личный кабинет", content, user=user))

        if path.startswith("/static/"):
            return self._static(path)

        if path == "/favicon.ico":
            self._send(HTTPStatus.NO_CONTENT, b"", "image/x-icon")
            return

        self._error(HTTPStatus.NOT_FOUND, "Такой страницы нет.")

    def _static(self, path: str) -> None:
        rel = path[len("/static/"):]
        if "/" in rel or ".." in rel:
            return self._error(HTTPStatus.NOT_FOUND, "Файл не найден.")
        file = STATIC / rel
        if not file.is_file():
            return self._error(HTTPStatus.NOT_FOUND, "Файл не найден.")
        types = {".css": "text/css; charset=utf-8", ".js": "application/javascript; charset=utf-8",
                 ".svg": "image/svg+xml", ".png": "image/png"}
        self._send(HTTPStatus.OK, file.read_bytes(),
                   types.get(file.suffix, "application/octet-stream"),
                   {"Cache-Control": "public, max-age=3600"})

    # -- POST --------------------------------------------------------------- #

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        user = self._session()

        if path == "/register":
            return self._register()
        if path == "/login":
            return self._login()
        if path == "/logout":
            form = self._form()
            if user and self._check_csrf(user, form.get("csrf", "")):
                with connect() as conn:
                    conn.execute("DELETE FROM sessions WHERE token = ?", (user["token"],))
            headers = {"Set-Cookie": f"{SESSION_COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax"}
            self._send(HTTPStatus.SEE_OTHER, b"", "text/plain", {"Location": "/", **headers})
            return

        if path == "/app/delete":
            if not user:
                return self._redirect("/login")
            form = self._form()
            if not self._check_csrf(user, form.get("csrf", "")):
                return self._error(HTTPStatus.FORBIDDEN, "Сессия устарела, обновите страницу.")
            with connect() as conn:
                conn.execute("DELETE FROM calculations WHERE id = ? AND user_id = ?",
                             (form.get("id", "0"), user["user_id"]))
            return self._redirect("/app")

        if path == "/api/save":
            if not user:
                return self._json(HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "Нужен вход"})
            if not self._check_csrf(user, self.headers.get("X-CSRF-Token", "")):
                return self._json(HTTPStatus.FORBIDDEN, {"ok": False, "error": "CSRF"})
            return self._save_calculation(user)

        self._error(HTTPStatus.NOT_FOUND, "Такой страницы нет.")

    # -- обработчики --------------------------------------------------------- #

    def _register(self) -> None:
        form = self._form()
        email = (form.get("email") or "").strip().lower()
        password = form.get("password") or ""
        confirm = form.get("confirm") or ""

        error = ""
        if not EMAIL_RE.match(email):
            error = "Похоже, адрес почты указан с ошибкой."
        elif len(password) < 8:
            error = "Пароль должен быть не короче 8 символов."
        elif password != confirm:
            error = "Пароли не совпадают."
        if error:
            content = render("register.html",
                             error=f'<div class="alert">{html.escape(error)}</div>',
                             email=html.escape(email))
            return self._html(HTTPStatus.BAD_REQUEST, page("Регистрация", content))

        try:
            with connect() as conn:
                cur = conn.execute(
                    "INSERT INTO users (email, password_hash, created_at) VALUES (?, ?, ?)",
                    (email, hash_password(password), time.time()),
                )
                user_id = cur.lastrowid
        except sqlite3.IntegrityError:
            content = render("register.html",
                             error='<div class="alert">Аккаунт с такой почтой уже есть.</div>',
                             email=html.escape(email))
            return self._html(HTTPStatus.CONFLICT, page("Регистрация", content))

        token, _ = create_session(user_id)
        headers = self._set_session_cookie(token)
        headers["Location"] = "/app"
        self._send(HTTPStatus.SEE_OTHER, b"", "text/plain", headers)

    def _login(self) -> None:
        form = self._form()
        email = (form.get("email") or "").strip().lower()
        password = form.get("password") or ""

        with connect() as conn:
            row = conn.execute(
                "SELECT id, password_hash FROM users WHERE email = ?", (email,)
            ).fetchone()

        if not row or not check_password(password, row["password_hash"]):
            content = render("login.html",
                             error='<div class="alert">Неверная почта или пароль.</div>',
                             email=html.escape(email))
            return self._html(HTTPStatus.UNAUTHORIZED, page("Вход", content))

        token, _ = create_session(row["id"])
        headers = self._set_session_cookie(token)
        headers["Location"] = "/app"
        self._send(HTTPStatus.SEE_OTHER, b"", "text/plain", headers)

    def _save_calculation(self, user) -> None:
        data = self._json_body()
        try:
            payload = {
                "area": float(data["area"]),
                "density": float(data["density"]),
                "m1000": float(data["m1000"]),
                "germination": float(data["germination"]),
                "purity": float(data["purity"]),
                "price": float(data["price"]),
            }
            culture = str(data.get("culture") or "Без названия")[:120]
        except (KeyError, TypeError, ValueError):
            return self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "Неверные данные"})

        if payload["area"] <= 0 or payload["area"] > 1_000_000:
            return self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "Площадь вне диапазона"})
        if payload["m1000"] <= 0 or payload["density"] <= 0:
            return self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "Норма и масса семян должны быть больше нуля"})

        result = calculate(payload)
        with connect() as conn:
            conn.execute(
                "INSERT INTO calculations (user_id, culture, area, density, m1000, germination, "
                "purity, price, rate_kg_ha, total_kg, cost, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (user["user_id"], culture, payload["area"], payload["density"], payload["m1000"],
                 payload["germination"], payload["purity"], payload["price"],
                 result["rate_kg_ha"], result["total_kg"], result["cost"], time.time()),
            )
        self._json(HTTPStatus.OK, {"ok": True, **result})

    def log_message(self, fmt: str, *args) -> None:  # тише в консоли
        print(f"[{self.log_date_time_string()}] {fmt % args}")


# --------------------------------------------------------------------------- #
#  Запуск
# --------------------------------------------------------------------------- #

def main() -> int:
    parser = argparse.ArgumentParser(description="Агро-калькулятор посевных")
    # На Render переменная PORT задаётся платформой, хост должен быть 0.0.0.0.
    default_host = os.environ.get("HOST") or ("0.0.0.0" if os.environ.get("PORT") else "127.0.0.1")
    default_port = int(os.environ.get("PORT", "8765"))
    parser.add_argument("--host", default=default_host)
    parser.add_argument("--port", type=int, default=default_port)
    args = parser.parse_args()

    init_db()
    seed_demo_user()

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.daemon_threads = True
    print(f"Агро-калькулятор: http://{args.host}:{args.port}", flush=True)
    print(f"База данных: {DB_PATH}", flush=True)
    print("Демо-доступ: demo@agro.ru / demo12345", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nОстановлено")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
