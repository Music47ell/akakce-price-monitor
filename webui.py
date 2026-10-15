import json
import logging
import secrets
import threading
import time
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import app

SESSION_TTL = 12 * 60 * 60
MAX_LOGIN_FAILS = 5
LOGIN_LOCK_WINDOW = 5 * 60

_SESSIONS = {}
_LOGIN_FAILS = {}
_LOCK = threading.Lock()


def _prune():
    now = time.time()
    with _LOCK:
        for token in [t for t, s in _SESSIONS.items() if s["expires"] < now]:
            _SESSIONS.pop(token, None)
        for ip in [i for i, f in _LOGIN_FAILS.items() if now - f["first"] > LOGIN_LOCK_WINDOW]:
            _LOGIN_FAILS.pop(ip, None)


def _new_session():
    token = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(32)
    with _LOCK:
        _SESSIONS[token] = {"expires": time.time() + SESSION_TTL, "csrf": csrf}
    return token, csrf


def _get_session(token):
    if not token:
        return None
    with _LOCK:
        session = _SESSIONS.get(token)
    if session and session["expires"] > time.time():
        return session
    return None


def _drop_session(token):
    with _LOCK:
        _SESSIONS.pop(token, None)


def _too_many_fails(ip):
    now = time.time()
    fail = _LOGIN_FAILS.get(ip)
    return bool(fail and fail["count"] >= MAX_LOGIN_FAILS and now - fail["first"] <= LOGIN_LOCK_WINDOW)


def _record_fail(ip):
    now = time.time()
    fail = _LOGIN_FAILS.get(ip)
    if not fail or now - fail["first"] > LOGIN_LOCK_WINDOW:
        _LOGIN_FAILS[ip] = {"count": 1, "first": now}
    else:
        fail["count"] += 1


def _clear_fails(ip):
    _LOGIN_FAILS.pop(ip, None)


def _page(title, body, head_extra=""):
    return (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        f"<title>{escape(title)}</title>{head_extra}<style>"
        "body{font-family:system-ui,Segoe UI,Arial,sans-serif;max-width:820px;"
        "margin:2rem auto;padding:0 1rem;color:#222;background:#fafafa}"
        "h1{font-size:1.3rem}table{border-collapse:collapse;width:100%;background:#fff}"
        "th,td{padding:.5rem .6rem;border-bottom:1px solid #eee;text-align:left;"
        "vertical-align:top;font-size:.92rem}"
        "input[type=text],input[type=password]{width:100%;padding:.5rem;box-sizing:border-box}"
        "button{padding:.5rem .9rem;cursor:pointer}"
        ".row{display:flex;gap:.5rem;margin:.8rem 0}.muted{color:#888}"
        ".err{color:#c00;margin:.5rem 0}.ok{color:#080;margin:.5rem 0}"
        "form.inline{display:inline}</style></head><body>"
        f"{body}</body></html>"
    )


class Handler(BaseHTTPRequestHandler):
    server_version = "akakce-monitor"

    def log_message(self, fmt, *args):
        logging.debug("webui %s - %s", self.client_address[0], fmt % args)

    # -- helpers ---------------------------------------------------------
    def _ip(self):
        return self.client_address[0]

    def _cookie(self, name):
        raw = self.headers.get("Cookie", "")
        for part in raw.split(";"):
            if "=" in part:
                key, value = part.strip().split("=", 1)
                if key == name:
                    return value
        return None

    def _token(self):
        return self._cookie("session")

    def _session(self):
        return _get_session(self._token())

    def _secure(self):
        return self.headers.get("X-Forwarded-Proto", "").lower() == "https"

    def _send(self, body, status=200, headers=None, content_type="text/html; charset=utf-8"):
        payload = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'",
        )
        for key, value in (headers or {}):
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(payload)

    def _redirect(self, location, extra=None):
        headers = [("Location", location)] + list(extra or [])
        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        for key, value in extra or []:
            self.send_header(key, value)
        self.end_headers()

    def _form(self):
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0:
            return {}
        body = self.rfile.read(length).decode("utf-8", "replace")
        return {k: v[-1] for k, v in parse_qs(body, keep_blank_values=True).items()}

    def _login_cookie(self, token):
        parts = [f"session={token}", "HttpOnly", "SameSite=Lax", "Path=/"]
        if self._secure():
            parts.append("Secure")
        return ("Set-Cookie", "; ".join(parts))

    # -- routing ---------------------------------------------------------
    def do_GET(self):
        _prune()
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/favicon.ico":
            self._send("", status=204)
            return
        if path == "/logout":
            _drop_session(self._token())
            self._redirect("/login", [("Set-Cookie", "session=; HttpOnly; SameSite=Lax; Path=/; Max-Age=0")])
            return
        if path == "/login":
            if self._session():
                self._redirect("/")
            else:
                self._send(_login_page())
            return
        if path == "/":
            if not self._session():
                self._redirect("/login")
                return
            checking = "check" in parse_qs(parsed.query)
            self._send(_products_page(self._session(), checking=checking))
            return
        self._send(_page("Not found", "<h1>Not found</h1>"), status=404)

    def do_POST(self):
        _prune()
        path = urlparse(self.path).path
        if path == "/login":
            self._login()
            return
        session = self._session()
        if not session:
            self._redirect("/login")
            return
        form = self._form()
        if form.get("csrf") != session["csrf"]:
            self._send(_page("Error", "<p class='err'>Invalid session token.</p>"), status=403)
            return
        if path == "/add":
            self._add(form)
        elif path == "/remove":
            self._remove(form)
        elif path == "/check":
            app.request_check()
            self._redirect("/?check=1")
        else:
            self._send(_page("Not found", "<h1>Not found</h1>"), status=404)

    # -- actions ---------------------------------------------------------
    def _login(self):
        ip = self._ip()
        if _too_many_fails(ip):
            self._send(_login_page("Too many attempts. Try again later."), status=429)
            return
        form = self._form()
        password = form.get("password", "")
        if app.UI_PASSWORD_HASH and app.verify_password(password, app.UI_PASSWORD_HASH):
            _clear_fails(ip)
            token, _ = _new_session()
            self._redirect("/", [self._login_cookie(token)])
            return
        _record_fail(ip)
        self._send(_login_page("Wrong password."), status=401)

    def _add(self, form):
        url = (form.get("url") or "").strip()
        if not url.lower().startswith(("http://", "https://")):
            self._send(_products_page(self._session(), "Enter a valid http(s) URL."), status=400)
            return
        products = app.load_products()
        if any(p["url"] == url for p in products):
            self._send(_products_page(self._session(), "That URL is already monitored."), status=400)
            return
        products.append({"id": app.new_id(), "url": url})
        app.save_products(products)
        app.request_check()
        self._redirect("/")

    def _remove(self, form):
        product_id = (form.get("id") or "").strip()
        products = [p for p in app.load_products() if p["id"] != product_id]
        app.save_products(products)
        self._redirect("/")


def _login_page(error=""):
    error_html = f"<p class='err'>{escape(error)}</p>" if error else ""
    return _page(
        "Login",
        "<h1>Akakçe price monitor</h1>"
        f"{error_html}"
        "<form method='post' action='/login'>"
        "<label>Password</label>"
        "<input type='password' name='password' autofocus required>"
        "<p><button type='submit'>Log in</button></p>"
        "</form>",
    )


def _products_page(session, error="", checking=False):
    state = app.load_state()
    products = app.load_products()

    error_html = f"<p class='err'>{escape(error)}</p>" if error else ""
    banner = (
        "<p class='ok'>Check requested. This page refreshes shortly — a "
        "notification is only sent if a price changed.</p>"
        if checking
        else ""
    )
    head_extra = '<meta http-equiv="refresh" content="15; url=/">' if checking else ""
    rows = []
    for product in products:
        entry = state.get(product["id"], {})
        name = entry.get("name") or "<span class='muted'>(pending check)</span>"
        price = "—"
        emoji = ""
        if entry.get("last_price") is not None:
            price = app.format_tl(entry["last_price"])
            emoji = app.DIRECTION_EMOJI.get(entry.get("direction"), "")
        updated = escape(entry.get("updated", "")[:19].replace("T", " "))
        if entry.get("name"):
            name = escape(entry["name"])
        rows.append(
            "<tr>"
            f"<td>{name}<br><a href='{escape(product['url'])}' target='_blank' rel='noopener'>{escape(product['url'])}</a></td>"
            f"<td>{emoji} {price}</td>"
            f"<td class='muted'>{updated}</td>"
            "<td><form class='inline' method='post' action='/remove'>"
            f"<input type='hidden' name='csrf' value='{escape(session['csrf'])}'>"
            f"<input type='hidden' name='id' value='{escape(product['id'])}'>"
            "<button type='submit'>Remove</button></form></td>"
            "</tr>"
        )

    body = (
        "<h1>Monitored products</h1>"
        f"{banner}{error_html}"
        "<form method='post' action='/add'>"
        f"<input type='hidden' name='csrf' value='{escape(session['csrf'])}'>"
        "<div class='row'>"
        "<input type='text' name='url' placeholder='https://www.akakce.com/...' required>"
        "<button type='submit'>Add</button></div></form>"
        "<table><thead><tr><th>Product</th><th>Price</th><th>Updated</th><th></th></tr></thead>"
        f"<tbody>{''.join(rows) or '<tr><td colspan=4>No products yet.</td></tr>'}</tbody></table>"
        "<form method='post' action='/check'>"
        f"<input type='hidden' name='csrf' value='{escape(session['csrf'])}'>"
        "<p><button type='submit'>Check now</button>"
        " <a href='/logout'>Log out</a></p></form>"
    )
    return _page("Products", body, head_extra)


def start_web_thread():
    if not app.UI_PASSWORD_HASH:
        logging.warning(
            "UI_PASSWORD_HASH is not set; the web UI is disabled. "
            "Generate one with: python app.py --hash-password"
        )
        return
    server = ThreadingHTTPServer(("0.0.0.0", app.UI_PORT), Handler)
    thread = threading.Thread(target=server.serve_forever, name="webui", daemon=True)
    thread.start()
    logging.info("Web UI listening on 0.0.0.0:%d", app.UI_PORT)
