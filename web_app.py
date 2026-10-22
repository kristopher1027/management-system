"""A small browser interface for the ResourceHub resource manager."""
from copy import deepcopy
from urllib.parse import parse_qs, urlsplit
from html import escape
from asset_labels import make_qr_png
import csv
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import ipaddress
import json
import math
import os
import re
import secrets
import shutil
import sqlite3
import smtplib
import ssl
from email.message import EmailMessage
import tempfile
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from threading import RLock, Thread
import time
from urllib.parse import parse_qs, urlparse
import zipfile

import main as inventory
import audit


def load_local_environment():
    """Load optional key=value settings from the private data-directory .env file."""
    env_path = os.path.join(inventory.DATA_DIR, ".env")
    try:
        with open(env_path, encoding="utf-8") as env_file:
            for line in env_file:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key, value = key.strip(), value.strip().strip("\"'")
                if key and key.replace("_", "").isalnum():
                    os.environ.setdefault(key, value)
    except FileNotFoundError:
        pass


load_local_environment()

HOST = os.environ.get("RESOURCEHUB_HOST", "127.0.0.1")
LOCAL_DEVELOPMENT = HOST in ("127.0.0.1", "localhost")
PORT = int(os.environ.get("PORT", os.environ.get("RESOURCEHUB_PORT", "8000")))
COOKIE_SECURE = os.environ.get("RESOURCEHUB_COOKIE_SECURE", "0") == "1"
LOCK = RLock()
AUTH_FILE = os.path.join(inventory.DATA_DIR, "auth.json")
SESSION_COOKIE = "learn2earn_session"
SESSION_TTL = 8 * 60 * 60
PASSWORD_ROUNDS = 310_000
users = {}
shops = {}
sessions = {}
TENANT_DIR = os.path.join(inventory.DATA_DIR, "shops")
ROOT_BACKUP_DIR = inventory.BACKUP_DIR
email_challenges = {}
email_ip_sends = {}
INITIALIZED_SHOPS = set()

# In-memory login rate limiting: five failures, then a 15-minute cooldown.
LOGIN_ATTEMPTS = {}
LOGIN_FAILURE_WINDOW = 15 * 60
LOGIN_MAX_FAILURES = 5
LOGIN_LOCKOUT_SECONDS = 15 * 60


def login_retry_after(ip):
    """Return remaining cooldown seconds, or zero when login is allowed."""
    record = LOGIN_ATTEMPTS.get(ip)
    if not record:
        return 0

    now = time.time()
    if record["locked_until"] > now:
        return int(record["locked_until"] - now) + 1

    if now - record["window_start"] >= LOGIN_FAILURE_WINDOW:
        LOGIN_ATTEMPTS.pop(ip, None)

    return 0


def record_login_failure(ip):
    """Count failed logins from an IP and start cooldown at the limit."""
    now = time.time()
    record = LOGIN_ATTEMPTS.get(ip)

    if not record or now - record["window_start"] >= LOGIN_FAILURE_WINDOW:
        record = {
            "window_start": now,
            "failures": 0,
            "locked_until": 0,
        }
        LOGIN_ATTEMPTS[ip] = record

    record["failures"] += 1
    if record["failures"] >= LOGIN_MAX_FAILURES:
        record["locked_until"] = now + LOGIN_LOCKOUT_SECONDS
TURNSTILE_SITE_KEY = os.environ.get(
    "TURNSTILE_SITE_KEY", "1x00000000000000000000AA" if LOCAL_DEVELOPMENT else "")


AUDIT_ACTIONS = {
    "/api/resources": "item_added", "/api/resource-details": "item_updated",
    "/api/delete-resource": "item_deleted", "/api/restock": "stock_in",
    "/api/stocktake": "stocktake",
    "/api/sale": "sale", "/api/borrow": "loan", "/api/return": "return",
    "/api/loan-due-date": "loan_due_date",
}


def audit_log(handler, username, action, target="", detail=""):
    """Record an audit entry for a signed-in user. Call while holding LOCK."""
    audit.record(inventory, username, users[username]["role"], action,
                 target, detail, handler.client_ip())


def audit_detail(path, data, message, before_resources, stocktake_changes=None):
    parts = [message]
    if path == "/api/resource-details":
        parts.append(audit.describe_item_changes(before_resources, data) or "no field changed")
    if path == "/api/stocktake":
        parts.append(f"note={data.get('note', '').strip()}")
        changes = stocktake_changes or []
        parts.append("variances=" + ("; ".join(
            f"{item['resource_id']}:{item['expected']}→{item['counted']} ({item['delta']:+})"
            for item in changes) or "none"))
    for key in ("quantity", "fellow_id", "customer", "loan_id", "due_date"):
        if data.get(key) not in (None, ""):
            parts.append(f"{key}={data[key]}")
    return " | ".join(parts)


def activate_shop(shop_id, allow_create=False):
    """Select a shop database safely while holding LOCK."""
    if shop_id != "default" and not re.fullmatch(r"[a-f0-9]{16}", str(shop_id)):
        raise RuntimeError("Invalid shop account configuration.")

    if shop_id == "default":
        db_file = os.path.join(inventory.DATA_DIR, "campus_resources.db")
        backup_dir = ROOT_BACKUP_DIR
    else:
        shop_dir = os.path.join(TENANT_DIR, shop_id)
        db_file = os.path.join(shop_dir, "campus_resources.db")
        backup_dir = os.path.join(shop_dir, "backups")

    if not os.path.isfile(db_file) and not allow_create:
        raise RuntimeError("Shop database is missing. Access has been blocked.")

    if allow_create:
        os.makedirs(os.path.dirname(db_file), exist_ok=True)

    previous_db = inventory.DB_FILE
    previous_backup = inventory.BACKUP_DIR

    try:
        inventory.DB_FILE = db_file
        inventory.BACKUP_DIR = backup_dir
        if os.path.isfile(db_file):
            if shop_id not in INITIALIZED_SHOPS:
                # Apply schema updates to older shop databases once per server
                # run, without adding demo rows to an intentionally empty shop.
                inventory.init_db(seed_defaults=False)
                INITIALIZED_SHOPS.add(shop_id)
            else:
                inventory.refresh_state()
    except Exception:
        inventory.resources.clear()
        inventory.fellows.clear()
        inventory.borrow_records.clear()
        inventory.DB_FILE = previous_db
        inventory.BACKUP_DIR = previous_backup
        raise
def create_shop_database(shop_id, shop_name, phone, email):
    """Create an empty tenant database without the demo inventory."""
    previous_db, previous_backup = inventory.DB_FILE, inventory.BACKUP_DIR
    try:
        activate_shop(shop_id, allow_create=True)
        inventory.init_db()
        INITIALIZED_SHOPS.add(shop_id)
        with inventory.connect() as db:
            db.execute("DELETE FROM loans")
            db.execute("DELETE FROM sales")
            db.execute("DELETE FROM stock_movements")
            db.execute("DELETE FROM stock_adjustments")
            db.execute("DELETE FROM resources")
            db.execute("DELETE FROM fellows")
            db.execute("UPDATE shop_settings SET shop_name=? WHERE id=1", (shop_name,))
        inventory.refresh_state()
    finally:
        inventory.DB_FILE, inventory.BACKUP_DIR = previous_db, previous_backup
        inventory.refresh_state()


def smtp_settings():
    """Read and validate the SMTP settings from the environment."""
    host = os.environ.get("RESOURCEHUB_SMTP_HOST", "smtp.gmail.com").strip()
    sender = os.environ.get("RESOURCEHUB_SMTP_FROM", "")
    password = os.environ.get("RESOURCEHUB_SMTP_PASSWORD", "").replace(" ", "")
    username = os.environ.get("RESOURCEHUB_SMTP_USERNAME", sender)
    port = int(os.environ.get("RESOURCEHUB_SMTP_PORT", "587"))
    placeholders = {"your-google-app-password", "your-app-password", "replace-me", "changeme"}
    if (not host or not sender or not username or not password
            or sender.lower().endswith("@example.com")
            or sender.lower() == "your-sender@gmail.com"
            or password.lower() in placeholders):
        raise RuntimeError("Configure RESOURCEHUB_SMTP_HOST, RESOURCEHUB_SMTP_FROM, RESOURCEHUB_SMTP_USERNAME, and RESOURCEHUB_SMTP_PASSWORD in .env before using email features.")
    if host.lower() == "smtp.gmail.com" and len(password) != 16:
        raise RuntimeError("The Gmail app password should be 16 characters. Use a Google App Password, not your regular Gmail password.")
    return host, port, sender, username, password


def deliver_email(to_address, subject, body):
    """Send one plain-text email. EmailMessage builds correct headers and rejects injected line breaks."""
    host, port, sender, username, password = smtp_settings()
    message = EmailMessage()
    message["From"] = f"ResourceHub <{sender}>"
    message["To"] = to_address
    message["Subject"] = subject
    message.set_content(body)
    if port == 465:
        with smtplib.SMTP_SSL(host, port, timeout=15,
                              context=ssl.create_default_context()) as smtp:
            smtp.login(username, password)
            smtp.send_message(message)
    else:
        with smtplib.SMTP(host, port, timeout=15) as smtp:
            smtp.ehlo()
            smtp.starttls(context=ssl.create_default_context())
            smtp.ehlo()
            smtp.login(username, password)
            smtp.send_message(message)


def send_email_code(email):
    """Email a six-digit code and return it. The caller stores the code."""
    code = f"{secrets.randbelow(1_000_000):06d}"
    deliver_email(email, "Your ResourceHub verification code",
                  f"Your verification code is {code}. It expires in 10 minutes.")
    return code


def send_test_email(email):
    email = email.strip()
    if (len(email) > 254 or email.count("@") != 1
            or any(char.isspace() for char in email)
            or "." not in email.rsplit("@", 1)[-1]):
        raise ValueError("Enter a valid email address.")
    deliver_email(email, "ResourceHub email test",
                  "Your ResourceHub email configuration is working.")


def verify_turnstile(token, remote_ip):
    secret = os.environ.get(
        "TURNSTILE_SECRET_KEY", "1x0000000000000000000000000000000AA" if LOCAL_DEVELOPMENT else "")
    if not secret:
        return False
    if not token:
        return False
    data = urllib.parse.urlencode({"secret": secret, "response": token, "remoteip": remote_ip}).encode()
    request = urllib.request.Request("https://challenges.cloudflare.com/turnstile/v0/siteverify", data=data)
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            result = json.loads(response.read(4096))
        return bool(result.get("success"))
    except (OSError, ValueError, json.JSONDecodeError):
        return False

PROXY_HOPS = int(os.environ.get("RESOURCEHUB_PROXY_HOPS", "0") or 0)
DUMMY_SALT = secrets.token_bytes(16)
email_sending = set()


def reject_json_constant(name):
    raise ValueError("Numbers must be finite.")


def parse_finite_float(text):
    value = float(text)
    if not math.isfinite(value):
        raise ValueError("Numbers must be finite.")
    return value


def purge_expired():
    """Drop expired sessions, codes and rate-limit records. Call while holding LOCK."""
    now = time.time()
    for token in [t for t, s in sessions.items()
                  if not isinstance(s, dict) or s.get("expires", 0) <= now]:
        sessions.pop(token, None)
    for key in [k for k, c in email_challenges.items() if c.get("expires", 0) < now - 3600]:
        email_challenges.pop(key, None)
    for key in [k for k, sent in email_ip_sends.items() if sent < now - 3600]:
        email_ip_sends.pop(key, None)
    for key in [k for k, r in LOGIN_ATTEMPTS.items()
                if r["locked_until"] < now and now - r["window_start"] >= LOGIN_FAILURE_WINDOW]:
        LOGIN_ATTEMPTS.pop(key, None)


def prehash_password(path, data, ip):
    """Run PBKDF2 outside LOCK so one sign-in cannot freeze every shop.

    Unknown usernames are hashed with a dummy salt, so response time does not
    reveal whether an account exists. Returns None when no hashing is needed.
    """
    username, password = data.get("username"), data.get("password")
    if not isinstance(username, str) or not isinstance(password, str):
        return None
    if path == "/api/register":
        if len(password) < 10:
            return None
        salt = secrets.token_bytes(16)
    elif path == "/api/login":
        if login_retry_after(ip):
            return None
        user = users.get(username.strip().lower())
        try:
            salt = bytes.fromhex(user["salt"]) if user else DUMMY_SALT
        except (KeyError, TypeError, ValueError):
            return None
    else:
        return None
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PASSWORD_ROUNDS)
    return {"salt": salt, "digest": digest}


PASSWORD_MIN_LENGTH = 10
PASSWORD_MAX_LENGTH = 200
RESET_TTL = 15 * 60
reset_challenges = {}
reset_ip_sends = {}


def hash_new_password(password):
    """Return (salt, digest) for a new password. Call outside LOCK; hashing is slow."""
    salt = secrets.token_bytes(16)
    return salt, hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PASSWORD_ROUNDS)


def sign_out_user(username, keep_token=None):
    """End every session of a user, except one optional token. Call while holding LOCK."""
    for token, session in list(sessions.items()):
        if isinstance(session, dict) and session.get("username") == username and token != keep_token:
            sessions.pop(token, None)


def check_new_password(password):
    if not isinstance(password, str):
        raise ValueError("Enter a new password.")
    if len(password) < PASSWORD_MIN_LENGTH or len(password) > PASSWORD_MAX_LENGTH:
        raise ValueError(f"Use a password with {PASSWORD_MIN_LENGTH} to {PASSWORD_MAX_LENGTH} characters.")


def send_reset_email(username, target, code):
    """Runs in a background thread so response time never reveals whether an account exists."""
    try:
        deliver_email(target, "Your ResourceHub password reset code",
                      f"Your password reset code is {code}. It expires in 15 minutes. "
                      "If you did not ask for this, you can ignore this email.")
    except Exception as error:
        print(f"Could not send the password reset email: {error}")
        with LOCK:
            challenge = reset_challenges.get(username)
            if challenge and challenge["code"] == code:
                reset_challenges.pop(username, None)


PAGE = r'''<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="theme-color" content="#101b2d">
  <title>ResourceHub · Vendor stock desk</title>
  <style>
:root{color-scheme:light dark;
  --ink:#16212b;--muted:#5d6b78;--line:#dde2e7;--paper:#fff;--wash:#f4f6f7;
  --blue:#2b5f8f;--on-blue:#fff;
  --tint:color-mix(in srgb,var(--blue) 8%,#fff);--tint-strong:color-mix(in srgb,var(--blue) 15%,#fff);
  --accent-ink:color-mix(in srgb,var(--blue) 78%,#000);
  --green:#17785a;--green-bg:#e6f3ed;--amber:#955806;--amber-bg:#fcf0dc;--red:#b3261e;--red-bg:#fbeae8;
  --space-1:4px;--space-2:8px;--space-3:12px;--space-4:16px;--space-5:20px;--space-6:24px;--space-7:32px;--space-8:40px;
  --type-xs:11px;--type-sm:12px;--type-label:13px;--type-base:14px;--type-lg:16px;--type-xl:22px;--type-2xl:28px;
  --r-panel:12px;--r-ctl:8px;--r-chip:6px;--navy:#16212b;--shadow:0 2px 6px #16212b0d;--shadow-card:0 8px 24px #16212b0d;
  --motion-fast:160ms;--motion-standard:180ms;--ease-ui:cubic-bezier(.2,.7,.2,1)}
*{box-sizing:border-box}[hidden]{display:none!important}
body{margin:0;background:var(--wash);color:var(--ink);font:var(--type-base)/1.5 ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;font-variant-numeric:tabular-nums;-webkit-font-smoothing:antialiased}
button,input,select{font:inherit;color:inherit}button{cursor:pointer}
h1,h2,h3{letter-spacing:-.015em}
:focus-visible{outline:2px solid var(--blue)!important;outline-offset:2px}

/* Header */
.shell{min-height:100vh}
.sticky-header{position:sticky;top:0;z-index:20;background:var(--paper)}
.topbar{position:relative;z-index:1;min-height:76px;display:grid;grid-template-columns:minmax(0,1fr) auto;align-items:center;gap:16px;padding:10px max(28px,calc((100vw - 1320px)/2));background:var(--paper);border-bottom:1px solid var(--line);box-shadow:0 5px 20px #16212b0b;color:var(--ink)}
.app-menu{display:flex;align-items:center;justify-content:center;gap:5px;padding:7px max(24px,calc((100vw - 1320px)/2));overflow-x:auto;overflow-y:hidden;white-space:nowrap;overscroll-behavior-x:contain;scrollbar-width:thin;background:var(--paper);border-bottom:1px solid var(--line)}.app-menu a{flex:0 0 auto;padding:8px 12px;border-radius:8px;color:var(--muted);font-size:13px;font-weight:650;text-decoration:none;white-space:nowrap;transition:background var(--motion-fast) var(--ease-ui),color var(--motion-fast) var(--ease-ui),box-shadow var(--motion-fast) var(--ease-ui)}.app-menu a:hover,.app-menu a:focus-visible{background:var(--wash);color:var(--ink);box-shadow:0 1px 4px #16212b14}.app-menu a:focus-visible{outline:3px solid var(--blue);outline-offset:2px}html{scroll-behavior:smooth}main [id]{scroll-margin-top:140px}
.app-menu a[aria-current="page"]{background:var(--tint);color:var(--accent-ink);box-shadow:0 1px 4px #16212b14}
.brand{display:flex;align-items:center;gap:12px;font-weight:750;font-size:16px;letter-spacing:-.02em}
.brand small{display:block;margin-top:1px;color:var(--muted);font-size:11px;font-weight:500;letter-spacing:.01em}
.brand-mark{height:42px;width:42px;border-radius:12px;background:linear-gradient(145deg,var(--blue),color-mix(in srgb,var(--blue) 78%,#000));color:var(--on-blue);display:grid;place-items:center;overflow:hidden;flex:0 0 auto;box-shadow:0 4px 10px color-mix(in srgb,var(--blue) 24%,transparent)}
.brand-mark svg{width:20px;height:20px}.brand-mark .shop-logo{width:100%;height:100%;object-fit:contain;padding:3px}
.top-right{display:flex;align-items:center;justify-content:flex-end;gap:14px;color:var(--muted);font-size:12px}
.system-status{display:inline-flex;align-items:center;gap:9px;padding:8px 11px;border:1px solid var(--green-bg);border-radius:999px;background:color-mix(in srgb,var(--green-bg) 58%,#fff);color:var(--green);font-size:12px;font-weight:650;white-space:nowrap}
.live{height:8px;width:8px;border-radius:50%;background:#2fb386;box-shadow:0 0 0 3px color-mix(in srgb,#2fb386 15%,#fff)}
.account-menu{position:relative;flex:0 0 auto}
.account-name{display:flex;align-items:center;gap:10px;min-height:42px;padding:8px 13px;border:1px solid var(--line);border-radius:12px;background:var(--paper);color:var(--ink);font-size:12px;font-weight:700;white-space:nowrap;cursor:pointer;list-style:none;transition:background .16s ease,border-color .16s ease,box-shadow .16s ease}
.account-name::-webkit-details-marker{display:none}.account-name::after{content:"";width:7px;height:7px;border-right:1.7px solid currentColor;border-bottom:1.7px solid currentColor;transform:translateY(-2px) rotate(45deg);transition:transform .16s ease}.account-menu[open] .account-name::after{transform:translateY(2px) rotate(225deg)}
.account-name:hover,.account-menu[open] .account-name{border-color:color-mix(in srgb,var(--line) 60%,var(--blue));background:var(--wash)}
.account-name:focus-visible{outline:3px solid var(--blue);outline-offset:3px}
.account-menu-panel{position:absolute;top:calc(100% + 10px);right:0;z-index:10;width:min(250px,calc(100vw - 28px));display:grid;gap:5px;padding:9px;border:1px solid var(--line);border-radius:14px;background:var(--paper);box-shadow:0 16px 40px #16212b24}
.account-menu-panel::before{content:"Account";padding:8px 10px 7px;color:var(--muted);font-size:11px;font-weight:750;letter-spacing:.1em;text-transform:uppercase}
.logout{width:100%;min-height:42px;display:flex;align-items:center;border:0;border-radius:9px;background:transparent;color:var(--ink);padding:9px 11px;font-size:13px;font-weight:600;text-align:left;transition:background .16s ease,color .16s ease}
.logout:hover{background:var(--wash);color:var(--blue)}
.account-menu-panel #logoutButton{margin-top:4px;border-top:1px solid var(--line);border-radius:0 0 9px 9px;color:var(--red)}
.account-menu-panel #logoutButton:hover{background:var(--red-bg);color:var(--red)}

/* Page structure */
main{max-width:1280px;margin:auto;padding:32px 24px 56px}
.heading{display:flex;justify-content:space-between;align-items:center;gap:var(--space-5);margin-bottom:var(--space-5);padding:var(--space-5);border:1px solid var(--line);border-radius:var(--r-panel);background:var(--paper);box-shadow:var(--shadow)}
.heading-actions{display:grid;justify-items:end;gap:var(--space-3);flex:0 0 auto}
.heading-actions .actions{justify-content:flex-end}
.eyebrow{color:var(--accent-ink);font-weight:600;font-size:13px}
.heading h1{font-size:26px;line-height:1.2;margin:4px 0 6px;font-weight:650}
.heading p{color:var(--muted);margin:0;max-width:60ch}
.today{color:var(--muted);font-size:13px;white-space:nowrap}
.layout{display:grid;grid-template-columns:minmax(0,1fr) minmax(300px,340px);gap:var(--space-5);align-items:start}
.side{display:grid;gap:var(--space-5);align-content:start}

/* Summary cards */
.stats{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:var(--space-3);margin-bottom:var(--space-5)}
.stat{min-width:0;background:var(--paper);border:1px solid var(--line);border-radius:var(--r-panel);box-shadow:var(--shadow);padding:var(--space-5);transition:transform var(--motion-fast) var(--ease-ui),box-shadow var(--motion-fast) var(--ease-ui),border-color var(--motion-fast) var(--ease-ui)}
.stat:hover{transform:translateY(-2px);border-color:color-mix(in srgb,var(--line) 72%,var(--blue));box-shadow:var(--shadow-card)}
.stat-head{display:flex;align-items:center;gap:var(--space-3);margin-bottom:var(--space-3)}
.stat-icon{width:36px;height:36px;display:grid;place-items:center;flex:0 0 auto;border-radius:50%;background:var(--tint);color:var(--accent-ink)}
.stat-icon svg{display:block;width:18px;height:18px}
.stat-label{font-size:var(--type-label);line-height:1.35;color:var(--muted);font-weight:550}
.stat-value{font-size:var(--type-2xl);font-weight:650;letter-spacing:-.02em;margin-top:0;line-height:1.15}
.stat-foot{font-size:var(--type-sm);color:var(--muted);margin-top:var(--space-1)}

/* Panels */
.panel{background:var(--paper);border:1px solid var(--line);border-radius:var(--r-panel);box-shadow:var(--shadow-card);padding:var(--space-5)}
.panel-head{display:flex;justify-content:space-between;align-items:center;gap:var(--space-3);margin-bottom:var(--space-4)}
.panel-head h2{font-size:var(--type-lg);margin:0;font-weight:650}
.subtle{color:var(--muted);font-size:13px;margin-top:2px}
.actions{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.history-date-label{display:flex;align-items:center;gap:6px;color:var(--muted);font-size:12px;white-space:nowrap}.history-date-label .category-filter{width:145px;min-width:0;padding:0 7px}

/* Controls */
.search,.category-filter,.field input,.field select{height:40px;border:1px solid var(--line);border-radius:var(--r-ctl);padding:0 var(--space-3);background:var(--paper);outline:none;transition:border-color .15s,box-shadow .15s}
.search{width:220px}.category-filter{min-width:150px}
.search:focus,.category-filter:focus,.field input:focus,.field select:focus{border-color:var(--blue);box-shadow:0 0 0 3px var(--tint-strong)}
.field input:disabled,.field select:disabled{background:var(--wash);color:#8795a1;cursor:not-allowed}
.btn{height:40px;border:1px solid var(--blue);border-radius:var(--r-ctl);background:var(--blue);color:var(--on-blue);font-weight:600;padding:0 var(--space-4);transition:filter var(--motion-fast) var(--ease-ui),background-color var(--motion-fast) var(--ease-ui),transform var(--motion-fast) var(--ease-ui),border-color var(--motion-fast) var(--ease-ui)}
.btn:hover:not(:disabled){filter:brightness(.93);transform:translateY(-1px)}.btn:active:not(:disabled){filter:brightness(.86);transform:translateY(0)}
.btn.secondary{background:var(--paper);color:var(--ink);border-color:var(--line)}
.btn.secondary:hover:not(:disabled){background:var(--wash);filter:none}
.btn.full{width:100%;margin-top:4px}
.btn:disabled{cursor:not-allowed;opacity:.5;filter:none}
td .btn{height:30px;padding:0 10px;font-size:13px}
#inventory td .btn{min-width:70px}
#inventory td .delete-resource{color:var(--red);border-color:var(--red-bg)}
#inventory td .delete-resource:hover{background:var(--red-bg);color:var(--red);filter:none}

/* Tables */
.table-wrap{overflow:auto}
#inventory .table-wrap{max-height:min(68vh,680px);overscroll-behavior:contain}
table{width:100%;border-collapse:collapse;white-space:nowrap}
#inventory table{border-collapse:separate;border-spacing:0}
th{text-align:left;color:var(--muted);font-size:12px;font-weight:600;padding:10px 12px;border-bottom:1px solid var(--line);background:var(--wash)}
#inventory thead th{position:sticky;top:0;z-index:2}
.table-sort{border:0;padding:0;background:transparent;color:inherit;font:inherit;cursor:pointer}.table-sort:hover{color:var(--ink);text-decoration:underline}.table-sort:focus-visible{outline:3px solid var(--blue);outline-offset:3px;border-radius:3px}.sort-indicator{display:inline-block;min-width:1em;color:var(--blue);text-align:center}
th:first-child{border-top-left-radius:var(--r-ctl)}th:last-child{border-top-right-radius:var(--r-ctl)}
td{padding:12px;border-bottom:1px solid #edf0f3;font-size:14px}
#inventory th:nth-child(n+3):nth-child(-n+6),#inventory td:nth-child(n+3):nth-child(-n+6){text-align:right}
tbody tr:last-child td{border-bottom:0}tbody tr:hover{background:#fafbfc}
#inventory tbody tr:nth-child(even){background:var(--wash)}#inventory tbody tr:hover{background:var(--tint)}
.resource-name{font-weight:600}.resource-id{color:var(--muted);font-size:12px;margin-top:1px}
.badge{display:inline-flex;align-items:center;gap:6px;border-radius:var(--r-chip);padding:3px 9px;font-size:12px;font-weight:600;background:var(--green-bg);color:var(--green)}
.badge:before{content:"";width:6px;height:6px;border-radius:50%;background:currentColor}
.badge.low{background:var(--amber-bg);color:var(--amber)}.badge.out{background:var(--red-bg);color:var(--red)}
.empty{text-align:center;padding:28px;color:var(--muted)}
.inventory-empty{display:grid;justify-items:center;gap:var(--space-3);padding:var(--space-6) var(--space-4);white-space:normal}
.inventory-empty-icon{width:56px;height:56px;display:grid;place-items:center;border-radius:50%;background:var(--tint);color:var(--accent-ink)}
.inventory-empty-icon svg{width:28px;height:28px}
.inventory-empty p{max-width:36ch;margin:0;color:var(--muted);line-height:1.5}
.inventory-empty .btn{margin-top:var(--space-1)}

/* Side panels */
.form-panel h2{font-size:16px;margin:0;font-weight:650}.form-panel .subtle{margin-bottom:14px}
.tabs{display:flex;background:var(--wash);border:1px solid var(--line);padding:3px;border-radius:var(--r-ctl);margin-bottom:16px}
.tab{border:0;background:transparent;border-radius:6px;flex:1;padding:7px;color:var(--muted);font-weight:600;font-size:13px}
.tab.active{color:var(--ink);background:var(--paper);box-shadow:0 1px 2px #16212b1f}
.field{margin-bottom:var(--space-3)}.field label{display:block;font-size:var(--type-label);line-height:1.35;color:var(--ink);font-weight:600;margin-bottom:5px}.field input,.field select{width:100%}
.form-message{min-height:18px;margin-top:10px;font-size:13px;color:var(--green)}.form-message.error{color:var(--red)}
.loan-list{display:grid}
.loan-row{display:flex;justify-content:space-between;gap:10px;align-items:center;border-bottom:1px solid #edf0f3;padding:10px 0}
.loan-row:first-child{padding-top:0}.loan-row:last-child{border:0;padding-bottom:0}
.loan-who{font-size:13px;font-weight:600}.loan-what{font-size:12px;color:var(--muted)}.loan-count{font-weight:600;font-size:13px;white-space:nowrap}
.loan-due{font-size:11px;color:var(--muted);margin-top:3px}.loan-due.soon{color:var(--amber);font-weight:600}.loan-due.overdue{color:var(--red);font-weight:650}
.loan-row.overdue{background:var(--red-bg);border-radius:var(--r-ctl);padding:10px;margin:4px 0}.loan-row.overdue:first-child{padding-top:10px}.loan-row.overdue:last-child{padding-bottom:10px}
.loan-actions{display:grid;justify-items:end;gap:6px;flex-shrink:0}.loan-return,.loan-date-update{font-size:11px;padding:5px 8px;white-space:nowrap}
.loan-controls{display:grid;grid-template-columns:minmax(0,1fr) minmax(110px,42%);gap:8px;margin-bottom:10px}.loan-controls .search,.loan-controls .category-filter{width:100%;min-width:0}
.loan-reminder{padding:10px 12px;margin:0 0 10px;border:1px solid var(--amber-bg);border-radius:var(--r-ctl);background:var(--amber-bg);color:var(--amber);font-size:12px;font-weight:600;line-height:1.45}.loan-reminder.overdue{border-color:var(--red-bg);background:var(--red-bg);color:var(--red)}.loan-reminder[hidden]{display:none}
.watch-list{display:grid;gap:8px}
.watch-row{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:9px 11px;border:1px solid var(--line);border-radius:var(--r-ctl)}
.watch-name{font-size:13px;font-weight:600}.watch-detail{font-size:12px;color:var(--muted)}
.watch-count{font-size:12px;font-weight:600;color:var(--amber);white-space:nowrap}.watch-count.out{color:var(--red)}
.toast{position:fixed;right:22px;bottom:22px;z-index:30;display:flex;align-items:flex-start;gap:12px;width:max-content;max-width:min(420px,calc(100vw - 28px));padding:14px 16px;background:var(--ink);color:var(--paper);border:1px solid color-mix(in srgb,var(--paper) 18%,var(--ink));border-left:4px solid var(--blue);border-radius:var(--r-panel);box-shadow:0 12px 32px color-mix(in srgb,var(--ink) 28%,transparent);font-weight:550;line-height:1.45;overflow-wrap:anywhere;opacity:0;transform:translateY(8px);transition:opacity var(--motion-standard) var(--ease-ui),transform var(--motion-standard) var(--ease-ui);pointer-events:none}
.toast::before{content:"";width:8px;height:8px;flex:0 0 8px;margin-top:5px;border-radius:50%;background:var(--blue);box-shadow:0 0 0 3px var(--tint)}
.toast.show{opacity:1;transform:none}
@media(max-width:520px){.toast{right:14px;bottom:14px}}
.footnote{margin-top:24px;color:var(--muted);font-size:12px;text-align:center}
dialog{width:min(440px,calc(100% - 28px));max-width:calc(100% - 28px);max-height:calc(100vh - 32px);max-height:calc(100dvh - 32px);overflow:auto;margin:auto;padding:var(--space-6);border:1px solid var(--line);border-radius:var(--r-panel);background:var(--paper);color:var(--ink);box-shadow:0 24px 64px color-mix(in srgb,var(--ink) 24%,transparent)}
#stocktakeDialog{width:min(760px,calc(100% - 28px))}
#staffDialog,#shopSettingsDialog{width:min(500px,calc(100% - 28px))}
#confirmDialog{width:min(420px,calc(100% - 28px))}
dialog::backdrop{background:color-mix(in srgb,var(--ink) 52%,transparent);backdrop-filter:blur(2px)}
@media(max-width:520px){dialog{padding:var(--space-5)}}

/* Sign-in */
.auth-screen{max-width:1280px;min-height:100vh;margin:0 auto;display:grid;grid-template-columns:minmax(0,1.05fr) minmax(0,.95fr);place-content:center;align-items:center;gap:clamp(24px,5vw,64px);padding:clamp(24px,5vw,72px);background:var(--wash)}
.auth-brand-panel{position:relative;isolation:isolate;overflow:hidden;width:min(100%,520px);min-height:500px;justify-self:center;display:flex;flex-direction:column;justify-content:space-between;gap:28px;padding:clamp(28px,4vw,44px);border:1px solid color-mix(in srgb,var(--blue) 34%,transparent);border-radius:24px;background:linear-gradient(145deg,color-mix(in srgb,var(--blue) 40%,#101820),color-mix(in srgb,var(--blue) 28%,#101820));color:#fff;box-shadow:0 24px 64px color-mix(in srgb,var(--blue) 18%,transparent)}
.auth-brand-panel::before{content:"";position:absolute;z-index:-1;right:-92px;top:105px;width:260px;height:260px;border:1px solid color-mix(in srgb,#fff 20%,transparent);border-radius:50%;box-shadow:0 0 0 28px color-mix(in srgb,#fff 6%,transparent),0 0 0 56px color-mix(in srgb,#fff 4%,transparent);pointer-events:none}
.auth-panel-brand{display:flex;align-items:center;gap:12px;font-weight:700;font-size:16px}.auth-panel-brand span:last-child{min-width:0;overflow-wrap:anywhere}
.auth-panel-mark{width:42px;height:42px;display:grid;place-items:center;flex:0 0 auto;border:1px solid color-mix(in srgb,#fff 24%,transparent);border-radius:13px;background:color-mix(in srgb,#fff 12%,transparent);color:#fff}.auth-panel-mark svg{width:22px;height:22px}
.auth-panel-copy{max-width:440px}.auth-panel-kicker{margin:0 0 12px;color:color-mix(in srgb,#fff 76%,transparent);font-size:11px;font-weight:750;letter-spacing:.13em;text-transform:uppercase}
.auth-panel-copy h2{max-width:12ch;margin:0;font-size:clamp(30px,3.4vw,40px);line-height:1.1;letter-spacing:-.035em}.auth-panel-tagline{max-width:34ch;margin:17px 0 0;color:color-mix(in srgb,#fff 86%,transparent);font-size:15px;line-height:1.65}
.auth-benefits{display:grid;gap:14px;margin:0;padding:0;list-style:none}.auth-benefits li{display:flex;align-items:center;gap:12px;color:color-mix(in srgb,#fff 92%,transparent);font-size:13px;font-weight:600}.auth-benefits svg{width:20px;height:20px;flex:0 0 auto;color:#fff}
@media(max-width:820px){.auth-screen{grid-template-columns:minmax(0,420px);gap:0;padding:24px}.auth-brand-panel{display:none}}
.auth-card{width:min(420px,100%);justify-self:center;background:var(--paper);border:1px solid var(--line);border-radius:16px;padding:32px;box-shadow:0 24px 64px color-mix(in srgb,var(--navy) 14%,transparent),0 4px 14px color-mix(in srgb,var(--navy) 7%,transparent)}
@keyframes auth-card-enter{from{opacity:0;transform:translateY(10px)}to{opacity:1;transform:translateY(0)}}
@media(prefers-reduced-motion:no-preference){.auth-card{animation:auth-card-enter 200ms var(--ease-ui) both}}
.auth-brand{display:flex;align-items:center;gap:11px;font-weight:650;margin-bottom:24px}
.auth-card h1{font-size:26px;line-height:1.2;letter-spacing:-.025em;margin:0;font-weight:750;color:var(--ink)}
.auth-card>p{color:var(--muted);margin:8px 0 22px;font-size:14px;line-height:1.55}
.auth-tabs{display:flex;gap:4px;padding:4px;margin-bottom:20px;background:var(--wash);border:1px solid var(--line);border-radius:12px}
.auth-tabs .tab{min-width:0;min-height:38px;padding:8px 6px;border:1px solid transparent;border-radius:9px;background:transparent;color:var(--muted);font-size:12px;font-weight:650;line-height:1.3;transition:background-color var(--motion-fast) var(--ease-ui),color var(--motion-fast) var(--ease-ui),box-shadow var(--motion-fast) var(--ease-ui)}
.auth-tabs .tab:hover:not(.active){background:color-mix(in srgb,var(--paper) 60%,var(--wash));color:var(--ink)}
.auth-tabs .tab.active{background:var(--paper);color:var(--ink);box-shadow:0 2px 5px color-mix(in srgb,var(--navy) 14%,transparent)}
.auth-card .field{margin-bottom:14px}
.auth-card .field label{margin-bottom:7px;color:var(--ink);font-size:13px;font-weight:650}
.auth-card .field input,.auth-card .field select{height:44px;border-color:var(--line);border-radius:var(--r-ctl);color:var(--ink);font-size:14px}
.auth-card .field input::placeholder{color:var(--muted);opacity:.85}
.auth-card .field input:focus,.auth-card .field select:focus{border-color:var(--blue);box-shadow:0 0 0 3px var(--tint-strong)}
.auth-card .btn.secondary{min-height:44px;border-radius:var(--r-ctl);font-weight:650}
.email-code-controls{display:flex;align-items:center;gap:8px}
.email-code-controls #emailCode{width:auto;min-width:0;flex:1}
.email-code-controls #sendEmailCode{height:44px;flex:0 0 auto;padding:0 13px;white-space:nowrap}
#turnstileContainer:empty{display:none}
#turnstileContainer:not(:empty){display:flex;justify-content:center;overflow-x:auto;margin:4px 0 14px;padding:12px;border:1px solid var(--line);border-radius:var(--r-ctl);background:var(--wash)}
#resetForm h2{color:var(--ink);font-size:18px!important;line-height:1.3}
#resetForm>p{color:var(--muted);font-size:13px;line-height:1.55}
.auth-input-wrap{position:relative}.auth-input-wrap input{height:44px;padding-left:43px}
.auth-field-icon{position:absolute;left:14px;top:50%;width:18px;height:18px;transform:translateY(-50%);color:var(--muted);pointer-events:none}
.auth-password-wrap input{padding-right:48px}
.auth-password-toggle{position:absolute;right:6px;top:50%;width:36px;height:36px;display:grid;place-items:center;padding:0;border:0;border-radius:var(--r-chip);background:transparent;color:var(--muted);transform:translateY(-50%);transition:background-color var(--motion-fast) var(--ease-ui),color var(--motion-fast) var(--ease-ui)}
.auth-password-toggle:hover{background:var(--wash);color:var(--ink)}.auth-password-toggle svg{display:block;width:18px;height:18px}
.auth-password-toggle .eye-slash{transition:opacity var(--motion-fast) var(--ease-ui)}.auth-password-toggle.is-visible .eye-slash{opacity:0}
.auth-card #authSubmit{height:46px;margin-top:6px;border-color:var(--blue);background:linear-gradient(135deg,color-mix(in srgb,var(--blue) 86%,var(--paper)),var(--blue));box-shadow:0 6px 15px color-mix(in srgb,var(--blue) 24%,transparent);font-size:14px;font-weight:700}
.auth-card #authSubmit:disabled{opacity:.62;box-shadow:none;transform:none;filter:none}
.auth-error{color:var(--red);font-size:13px;min-height:18px;margin:10px 0}
.auth-card .auth-error:empty{display:none}
.auth-card .auth-error:not(:empty){display:flex;align-items:flex-start;gap:9px;min-height:0;margin:12px 0 0;padding:11px 12px;border:1px solid color-mix(in srgb,var(--red) 28%,var(--line));border-radius:var(--r-ctl);background:color-mix(in srgb,var(--red) 8%,var(--paper));color:var(--red);font-size:13px;line-height:1.45}
.auth-card .auth-error:not(:empty)::before{content:"!";width:19px;height:19px;display:grid;place-items:center;flex:0 0 auto;border-radius:50%;background:color-mix(in srgb,var(--red) 14%,var(--paper));color:var(--red);font-size:12px;font-weight:800;line-height:1}
.auth-card .auth-error:not(:empty)[style*="--green"]{border-color:color-mix(in srgb,var(--green) 28%,var(--line));background:color-mix(in srgb,var(--green) 9%,var(--paper));color:var(--green)}
.auth-card .auth-error:not(:empty)[style*="--green"]::before{content:"✓";background:color-mix(in srgb,var(--green) 14%,var(--paper));color:var(--green)}
.auth-back{display:block;margin:0 0 16px;background:none;border:0;padding:0;color:var(--accent-ink);font-size:13px;font-weight:600}
.auth-back:hover{text-decoration:underline}
.auth-card #backLanding{display:inline-flex;align-items:center;gap:8px;margin:0 0 18px;color:var(--muted);font-size:13px}
.auth-card #backLanding svg{width:16px;height:16px;flex:0 0 auto}
.auth-card #backLanding:hover{color:var(--blue);text-decoration:none}
.auth-card #forgotLink{color:var(--muted);font-size:13px;font-weight:600;text-decoration:none;transition:color var(--motion-fast) var(--ease-ui)}
.auth-card #forgotLink:hover{color:var(--blue);text-decoration:underline}
.auth-card>.subtle:last-child{margin-top:22px;padding-top:14px;border-top:1px solid var(--line);color:var(--muted);font-size:11px;line-height:1.5}

/* Landing */
.landing-screen{min-height:100vh;background:var(--paper)}
.landing-nav{position:sticky;top:0;z-index:30;height:72px;padding:0 max(28px,calc((100vw - 1200px)/2));display:flex;align-items:center;justify-content:space-between;gap:20px;background:color-mix(in srgb,var(--paper) 88%,transparent);-webkit-backdrop-filter:blur(12px);backdrop-filter:blur(12px);border-bottom:1px solid transparent;transition:border-color var(--motion-fast) var(--ease-ui),box-shadow var(--motion-fast) var(--ease-ui)}
.landing-nav.is-scrolled{border-bottom-color:var(--line);box-shadow:0 4px 18px color-mix(in srgb,var(--ink) 7%,transparent)}
.landing-brand{min-width:0;flex:1 1 auto;display:flex;align-items:center;gap:12px;font-size:17px;font-weight:650}
.landing-brand #landingBrandName{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.landing-nav-links{flex:0 0 auto;display:flex;align-items:center;gap:clamp(12px,2vw,24px)}
.landing-nav-links a{color:var(--muted);font-size:14px;font-weight:600;text-decoration:none;white-space:nowrap;transition:color var(--motion-fast) var(--ease-ui)}
.landing-nav-links a:hover{color:var(--blue)}
.landing-nav-links a:focus-visible{outline:3px solid var(--blue);outline-offset:4px;border-radius:3px}
.landing-nav-links .btn{height:40px;padding:0 16px;white-space:nowrap}
.landing-content{max-width:1200px;margin:auto;padding:56px 28px 32px;display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1.05fr);align-items:center;gap:56px;background-image:radial-gradient(ellipse 60% 72% at 22% 42%,color-mix(in srgb,var(--blue) 11%,transparent),transparent 78%),radial-gradient(circle,color-mix(in srgb,var(--blue) 7%,transparent) 1px,transparent 1.5px);background-size:100% 100%,24px 24px;background-repeat:no-repeat,repeat}
.landing-copy h1{max-width:15em;margin:0 0 16px;font-size:clamp(36px,5.2vw,64px);line-height:1.04;letter-spacing:-.04em;font-weight:800}
.landing-highlight{background:linear-gradient(105deg,color-mix(in srgb,var(--blue) 82%,var(--ink)),var(--blue));background-clip:text;-webkit-background-clip:text;color:transparent}
.landing-copy>p{max-width:34em;margin:0;color:var(--muted);font-size:17px;line-height:1.65}
.landing-actions{display:flex;align-items:center;flex-wrap:wrap;gap:12px 14px;margin-top:28px}
.landing-actions .btn{height:46px;padding:0 20px;font-size:15px;display:inline-flex;align-items:center;justify-content:center;white-space:nowrap;text-decoration:none}
.landing-actions .subtle{flex-basis:100%;color:var(--muted);font-size:13px;line-height:1.5}
.landing-cta-short{display:none}
.landing-note{font-size:13px;color:var(--muted);margin-top:16px}
.inventory-preview{background:var(--paper);border:1px solid var(--line);border-radius:16px;padding:20px;box-shadow:0 28px 72px color-mix(in srgb,var(--ink) 12%,transparent),0 6px 18px color-mix(in srgb,var(--ink) 7%,transparent)}
.preview-top{display:flex;justify-content:space-between;align-items:center;margin-bottom:16px}
.preview-title{font-weight:650}
.preview-status{font-size:12px;font-weight:600;color:var(--muted);background:var(--wash);padding:4px 9px;border-radius:var(--r-chip)}
.preview-stats{display:grid;grid-template-columns:repeat(3,1fr);gap:1px;background:var(--line);border:1px solid var(--line);border-radius:var(--r-ctl);overflow:hidden}
.preview-stat{padding:12px 14px;background:var(--paper)}
.preview-stat strong{display:block;font-size:28px;line-height:1.1;letter-spacing:-.03em;font-weight:750}.preview-stat small{font-size:12px;color:var(--muted)}
.preview-stock-progress{margin:14px 2px 2px}
.preview-stock-label{display:flex;justify-content:space-between;align-items:center;gap:12px;margin-bottom:7px;color:var(--muted);font-size:12px}
.preview-stock-label strong{color:var(--ink);font-size:12px;font-weight:650;white-space:nowrap}
.preview-progress-track{height:7px;overflow:hidden;border-radius:var(--r-chip);background:var(--wash)}
.preview-progress-track span{display:block;width:67%;height:100%;border-radius:inherit;background:linear-gradient(90deg,color-mix(in srgb,var(--blue) 72%,var(--paper)),var(--blue))}
.preview-list{margin-top:14px;border:1px solid var(--line);border-radius:var(--r-ctl);overflow:hidden}
.preview-row{display:grid;grid-template-columns:1fr auto auto;gap:18px;padding:11px 14px;align-items:center;border-bottom:1px solid var(--line);font-size:13px}
.preview-row:last-child{border-bottom:0}.preview-row span:first-child{font-weight:600}.preview-row small{color:var(--muted)}
.preview-pill{background:color-mix(in srgb,var(--green) 12%,var(--paper));color:var(--green);padding:3px 8px;border-radius:var(--r-chip);font-size:12px;font-weight:600;white-space:nowrap}
.preview-pill.low{background:color-mix(in srgb,var(--amber) 14%,var(--paper));color:var(--amber)}
@keyframes landing-preview-float{0%,100%{transform:translateY(0) rotateY(-1deg)}50%{transform:translateY(-6px) rotateY(-1deg)}}
@media(prefers-reduced-motion:no-preference){.inventory-preview{animation:landing-preview-float 6s ease-in-out infinite;transform-origin:center}}
.landing-trust{max-width:1200px;margin:0 auto 28px;padding:16px 28px;list-style:none;display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:20px;border-top:1px solid var(--line)}
.landing-trust-item{min-width:0;display:flex;align-items:center;gap:10px;color:var(--muted);font-size:13px;font-weight:600;line-height:1.45}
.landing-trust-item svg{width:20px;height:20px;flex:0 0 auto;color:var(--blue)}
.landing-features{max-width:1200px;margin:0 auto;padding:0 28px 72px;display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:16px;scroll-margin-top:84px}
.landing-feature{min-width:0;padding:24px;border:1px solid var(--line);border-radius:16px;background:var(--paper);box-shadow:0 10px 28px color-mix(in srgb,var(--ink) 5%,transparent)}
.landing-feature-icon{width:44px;height:44px;display:grid;place-items:center;margin-bottom:16px;border-radius:14px;background:color-mix(in srgb,var(--blue) 11%,var(--paper));color:var(--blue)}
.landing-feature-icon svg{width:22px;height:22px}
.landing-feature h2{font-size:17px;margin:0 0 8px;font-weight:700;line-height:1.3}
.landing-feature p{margin:0;color:var(--muted);font-size:14px;line-height:1.65;max-width:30em}
@media(prefers-reduced-motion:no-preference){.landing-feature{transition:transform var(--motion-fast) var(--ease-ui),box-shadow var(--motion-fast) var(--ease-ui),border-color var(--motion-fast) var(--ease-ui)}.landing-feature:hover{transform:translateY(-3px);border-color:color-mix(in srgb,var(--blue) 30%,var(--line));box-shadow:0 16px 34px color-mix(in srgb,var(--ink) 9%,transparent)}}
.landing-steps{max-width:1200px;margin:0 auto;padding:12px 28px 72px;scroll-margin-top:84px}
.landing-steps-heading{max-width:600px;margin:0 0 32px}
.landing-steps-heading h2{margin:0 0 8px;color:var(--ink);font-size:clamp(25px,3vw,34px);line-height:1.2;letter-spacing:-.025em}
.landing-steps-heading p{margin:0;color:var(--muted);font-size:15px;line-height:1.6}
.landing-step-list{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));margin:0;padding:0;list-style:none}
.landing-step{position:relative;min-width:0;display:flex;flex-direction:column;align-items:center;padding:0 22px;text-align:center}
.landing-step-number{position:relative;z-index:1;width:42px;height:42px;display:grid;place-items:center;margin-bottom:16px;border:1px solid color-mix(in srgb,var(--blue) 32%,var(--line));border-radius:50%;background:var(--paper);box-shadow:0 0 0 6px var(--paper);color:var(--blue);font-size:15px;font-weight:750}
.landing-step:not(:last-child)::after{content:"";position:absolute;z-index:0;left:calc(50% + 21px);top:20px;width:calc(100% - 42px);height:2px;background:var(--line)}
.landing-step h3{margin:0 0 7px;color:var(--ink);font-size:16px;font-weight:700;line-height:1.35}
.landing-step p{max-width:31ch;margin:0;color:var(--muted);font-size:14px;line-height:1.6}

/* Responsive */
@media(max-width:940px){.layout{grid-template-columns:1fr}.side{grid-template-columns:repeat(2,minmax(0,1fr))}.stats{grid-template-columns:repeat(2,1fr)}}
@media(max-width:850px){.landing-content{grid-template-columns:1fr;gap:36px;padding-top:36px}.inventory-preview{max-width:620px}}
@media(max-width:700px){.landing-features{grid-template-columns:1fr;gap:12px;padding:16px 20px 48px}.landing-feature{padding:20px}.landing-trust{grid-template-columns:1fr;gap:12px;padding:15px 20px;margin-bottom:24px}}
@media(max-width:700px){.landing-steps{padding:8px 20px 56px}.landing-steps-heading{margin-bottom:24px}.landing-step-list{grid-template-columns:1fr}.landing-step{align-items:flex-start;min-height:100px;padding:0 0 22px 58px;text-align:left}.landing-step-number{position:absolute;top:0;left:0;margin:0}.landing-step:not(:last-child)::after{left:20px;top:42px;right:auto;bottom:-21px;width:2px;height:auto}}
@media(max-width:1100px){.topbar{grid-template-columns:minmax(0,1fr) auto;gap:12px;padding:12px 20px}.top-right{grid-column:2;grid-row:1;justify-self:end}.system-status{display:none}}
@media(max-width:600px){.topbar{grid-template-columns:minmax(0,1fr) auto;gap:8px;padding:10px 14px}.brand{gap:9px;font-size:15px}.brand-mark{height:38px;width:38px;border-radius:11px}.brand small{font-size:10px}.top-right{gap:0}.account-name{min-height:40px;padding:8px 10px}.app-menu{justify-content:flex-start;gap:4px;padding:7px 14px}.app-menu a{padding:8px 11px}main{padding:22px 14px 40px}main [id]{scroll-margin-top:140px}.heading{align-items:stretch;flex-direction:column}.heading-actions{width:100%}.heading-actions .actions{width:auto}.heading h1{font-size:23px}.stat{padding:14px}.stat-value{font-size:24px}.layout,.side{gap:14px}.panel{padding:16px}.panel-head{align-items:flex-start;flex-direction:column}.actions{width:100%}.search,.category-filter{width:100%}.history-date-label{width:calc(50% - 4px);min-width:0}.history-date-label .category-filter{width:auto;min-width:0;flex:1}.side{grid-template-columns:1fr}th,td{padding-left:8px;padding-right:8px}.auth-card{padding:24px}}
@media(max-width:420px){.topbar{grid-template-columns:minmax(0,1fr) auto;row-gap:0}.top-right{grid-column:2;grid-row:1;justify-content:flex-end}.brand small{display:none}.account-menu-panel{top:calc(100% + 8px)}}
@media(max-width:520px){.landing-nav{height:64px;padding:0 18px}.landing-content{padding:28px 20px 24px}.landing-actions{align-items:flex-start;flex-direction:column}.preview-row{gap:8px;padding:10px}}
@media(max-width:520px){.landing-nav{gap:10px}.landing-brand{gap:8px;font-size:15px}.landing-nav-links{gap:9px}.landing-nav-links a{font-size:12px}.landing-nav-links .btn{height:38px;padding:0 11px;font-size:13px}}
@media(max-width:520px){.landing-actions .landing-cta-full{display:none}.landing-actions .landing-cta-short{display:inline}.landing-actions .btn{min-width:116px}}
@media(prefers-color-scheme:dark){
  :root{color-scheme:dark;
    --ink:#e7edf5;--muted:#afbdcc;--line:#344252;--paper:#17212b;--wash:#101820;
    --blue:#83b9f7;--on-blue:#101820;
    --tint:color-mix(in srgb,var(--blue) 18%,var(--paper));--tint-strong:color-mix(in srgb,var(--blue) 30%,var(--paper));
    --accent-ink:color-mix(in srgb,var(--blue) 38%,#fff);
    --green:#6bd5a8;--green-bg:#173b30;--amber:#f3c66b;--amber-bg:#443719;--red:#ff918b;--red-bg:#472725;
    --navy:#101820;--shadow:0 2px 6px #0005;--shadow-card:0 8px 24px #0005
  }
  .system-status{background:var(--green-bg)}
  .field input:disabled,.field select:disabled{color:var(--muted)}
  td,.loan-row,.preview-row{border-bottom-color:var(--line)}
  tbody tr:hover{background:var(--wash)}
  dialog::backdrop{background:color-mix(in srgb,#000 68%,transparent)}
  .auth-brand small{color:var(--muted)!important}
}
@media(prefers-reduced-motion:reduce){*,*::before,*::after{scroll-behavior:auto!important;transition:none!important;animation:none!important}.btn:hover:not(:disabled),.btn:active:not(:disabled),.stat:hover{transform:none!important}}
#printStocktakeSheet{display:none}
@page{size:A4 landscape;margin:12mm}
@media print{body *{visibility:hidden!important}#printStocktakeSheet.print-ready,#printStocktakeSheet.print-ready *{visibility:visible!important}#printStocktakeSheet.print-ready{display:block!important;position:absolute;left:0;top:0;width:100%;color:#111;background:#fff;font:12px Arial,sans-serif}#printStocktakeSheet h1{margin:0 0 4px;font-size:22px}#printStocktakeSheet p{margin:0 0 14px;color:#444}#printStocktakeSheet table{width:100%;border-collapse:collapse;white-space:normal}#printStocktakeSheet th,#printStocktakeSheet td{border:1px solid #68737d;padding:8px;text-align:left}#printStocktakeSheet th{background:#eef1f3}#printStocktakeSheet tr{break-inside:avoid}}
  </style>
</head>
<body><div class="shell">
  <section class="landing-screen" id="landingScreen">
    <nav class="landing-nav" aria-label="Primary navigation"><div class="landing-brand"><div class="brand-mark" data-shop-logo><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 8.5 12 4l8 4.5v9L12 22l-8-4.5zM4 8.5l8 4.5 8-4.5M12 13v9m-8-13 8 4.5 8-4.5" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/></svg><img class="shop-logo" alt="Shop logo" hidden></div><span id="landingBrandName">ResourceHub</span></div><div class="landing-nav-links"><a href="#features">Features</a><a href="#how-it-works">How it works</a><button class="btn" type="button" data-open-auth>Sign in</button></div></nav>
    <div class="landing-content">
      <div class="landing-copy"><h1>See <span class="landing-highlight">what’s in stock</span>, what’s out, and what to reorder.</h1><p>Track stock, sales, loans and returns in one clear workspace for you and your team.</p><div class="landing-actions"><button class="btn" type="button" data-open-auth><span class="landing-cta-full">Sign in to <span id="landingCtaBrand">ResourceHub</span></span><span class="landing-cta-short">Sign in</span></button><a class="btn secondary" href="#features">Explore features</a><span class="subtle">New company? Create an owner account on the next screen.</span></div></div>
      <div class="inventory-preview" aria-label="Example of the stock desk"><div class="preview-top"><div class="preview-title">Shop inventory</div><span class="preview-status">Example data</span></div><div class="preview-stats"><div class="preview-stat"><strong>18</strong><small>Total units</small></div><div class="preview-stat"><strong>12</strong><small>Remaining in stock</small></div><div class="preview-stat"><strong>6</strong><small>On loan</small></div></div><div class="preview-stock-progress"><div class="preview-stock-label"><span>Available stock</span><strong>12 of 18 units</strong></div><div class="preview-progress-track" role="progressbar" aria-label="Available stock" aria-valuemin="0" aria-valuemax="18" aria-valuenow="12"><span aria-hidden="true"></span></div></div><div class="preview-list"><div class="preview-row"><span>Laptop</span><small>Electronics</small><span class="preview-pill">7 in stock</span></div><div class="preview-row"><span>Keyboard</span><small>Accessories</small><span class="preview-pill">3 in stock</span></div><div class="preview-row"><span>Headset</span><small>Accessories</small><span class="preview-pill low">2 left, reorder</span></div></div></div>
    </div>
    <ul class="landing-trust" aria-label="Stock desk facts"><li class="landing-trust-item"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="6.5" y="2.5" width="11" height="19" rx="2.3"/><path d="M10 18.5h4"/></svg><span>Designed for phone screens</span></li><li class="landing-trust-item"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><ellipse cx="12" cy="5" rx="8.5" ry="3"/><path d="M3.5 5v7c0 1.7 3.8 3 8.5 3s8.5-1.3 8.5-3V5M3.5 12v7c0 1.7 3.8 3 8.5 3s8.5-1.3 8.5-3v-7"/></svg><span>Data stored in your configured data directory</span></li><li class="landing-trust-item"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 3v11m0 0 4-4m-4 4-4-4"/><path d="M4 17v3h16v-3"/></svg><span>Export reports and download backups</span></li></ul>
    <section class="landing-features" id="features" aria-label="What ResourceHub does"><article class="landing-feature"><span class="landing-feature-icon" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M4 7.5 12 4l8 3.5v9L12 20l-8-3.5zM4 7.5 12 11m0 9v-9m8-3.5L12 11"/></svg></span><h2>Track every item</h2><p>See how many units you stocked, how many went out, and how many remain. Get a restock alert when an item runs low.</p></article><article class="landing-feature"><span class="landing-feature-icon" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M4 18V6m0 12h16M7 14l3-3 3 2 5-6"/><path d="M15 7h3v3"/></svg></span><h2>Record sales, loans and returns</h2><p>Staff log each transaction in seconds. Owners control products, prices and staff accounts.</p></article><article class="landing-feature"><span class="landing-feature-icon" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M6 3.5h8l4 4v13H6zM14 3.5v4h4M9 12h6M9 16h6"/></svg></span><h2>Report and back up</h2><p>Owners can review sales and stock value, export reports to CSV, and download a backup of the shop’s data.</p></article></section>
    <section class="landing-steps" id="how-it-works" aria-labelledby="landingStepsTitle"><div class="landing-steps-heading"><h2 id="landingStepsTitle">How it works</h2><p>A simple routine to keep your shop’s stock and activity up to date.</p></div><ol class="landing-step-list"><li class="landing-step"><span class="landing-step-number" aria-hidden="true">1</span><h3>Add your items</h3><p>Add products, prices, quantities and low-stock thresholds.</p></li><li class="landing-step"><span class="landing-step-number" aria-hidden="true">2</span><h3>Record sales and loans</h3><p>Log sales, loans and returns so available stock stays current.</p></li><li class="landing-step"><span class="landing-step-number" aria-hidden="true">3</span><h3>Review reports</h3><p>Check sales and stock levels to plan what to reorder.</p></li></ol></section>
  </section>
  <section class="auth-screen" id="authScreen" hidden><aside class="auth-brand-panel" aria-label="ResourceHub shop overview"><div class="auth-panel-brand"><span class="auth-panel-mark" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M4 8.5 12 4l8 4.5v9L12 22l-8-4.5zM4 8.5l8 4.5 8-4.5M12 13v9"/></svg></span><span id="authPanelBrandName">ResourceHub</span></div><div class="auth-panel-copy"><p class="auth-panel-kicker">Vendor stock desk</p><h2>Know your shop, at a glance.</h2><p class="auth-panel-tagline">Know what's in stock, what's sold and what to reorder.</p></div><ul class="auth-benefits" aria-label="Stock desk benefits"><li><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 7.5 12 4l8 3.5v9L12 20l-8-3.5zM4 7.5 12 11m0 9v-9m8-3.5L12 11"/></svg><span>See available stock at a glance</span></li><li><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 18V6m0 12h16M7 14l3-3 3 2 5-6"/><path d="M15 7h3v3"/></svg><span>Keep sales and loans organized</span></li><li><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M5 7h14M5 12h9M5 17h6"/><path d="m16 16 2 2 4-5"/></svg><span>Know what needs reordering</span></li></ul></aside><div class="auth-card"><button class="auth-back" type="button" id="backLanding"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M19 12H5m7 7-7-7 7-7"/></svg><span>Back to ResourceHub</span></button><div class="auth-brand"><div class="brand-mark" data-shop-logo><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 8.5 12 4l8 4.5v9L12 22l-8-4.5zM4 8.5l8 4.5 8-4.5M12 13v9m-8-13 8 4.5 8-4.5" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/></svg><img class="shop-logo" alt="Shop logo" hidden></div><div><span id="authBrandName">ResourceHub</span><small style="display:block;color:#718096;font-size:10px;font-weight:600;letter-spacing:.08em;text-transform:uppercase">Vendor stock desk</small></div></div><h1 id="authTitle">Welcome back</h1><p id="authSubtitle">Sign in to manage shop items and stock.</p><div class="tabs auth-tabs"><button class="tab active" type="button" data-auth-mode="login">Sign in</button><button class="tab" type="button" id="registerTab" data-auth-mode="register">Create owner account</button></div><form id="authForm"><div class="field" id="nameField" hidden><label for="authName">Your name</label><input id="authName" autocomplete="name" maxlength="80"></div><div class="field"><label for="authUsername">Username</label><div class="auth-input-wrap"><svg class="auth-field-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="8" r="3.5"/><path d="M5 20c.8-3.2 3.2-5 7-5s6.2 1.8 7 5"/></svg><input id="authUsername" autocomplete="username" minlength="3" maxlength="40" required></div></div><div class="field"><label for="authPassword">Password</label><div class="auth-input-wrap auth-password-wrap"><svg class="auth-field-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="4.5" y="10" width="15" height="10" rx="2"/><path d="M8 10V7a4 4 0 0 1 8 0v3M12 14v2"/></svg><input id="authPassword" type="password" autocomplete="current-password" minlength="10" required><button class="auth-password-toggle" type="button" aria-label="Show password" aria-pressed="false"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M2.5 12s3.5-6 9.5-6 9.5 6 9.5 6-3.5 6-9.5 6-9.5-6-9.5-6Z"/><circle cx="12" cy="12" r="2.5"/><path class="eye-slash" d="m4 4 16 16"/></svg></button></div></div><button class="btn full" type="submit" id="authSubmit">Sign in</button><div class="auth-error" id="authError" role="alert"></div></form><button class="auth-back" type="button" id="forgotLink" style="margin:14px 0 0">Forgot your password?</button><form id="resetForm" hidden><h2 style="font-size:17px;margin:0 0 4px">Reset your password</h2><p class="subtle" style="margin:0 0 14px">Owners get a code at the email address used to register. Staff: ask your shop owner to reset your password.</p><div class="field"><label for="resetUsername">Username</label><input id="resetUsername" autocomplete="username" maxlength="40" required></div><button class="btn secondary full" type="button" id="resetSend">Email me a code</button><div class="field" style="margin-top:14px"><label for="resetCode">Code from the email</label><input id="resetCode" inputmode="numeric" maxlength="6" autocomplete="one-time-code" required></div><div class="field"><label for="resetPassword">New password (at least 10 characters)</label><input id="resetPassword" type="password" minlength="10" autocomplete="new-password" required></div><button class="btn full" type="submit">Set new password</button><div class="auth-error" id="resetMessage" role="status"></div><button class="auth-back" type="button" id="resetBack" style="margin:0">Back to sign in</button></form><div class="subtle">Your accounts and inventory are stored in the configured data directory.</div></div></section>
  <div id="appScreen" hidden>
  <div class="sticky-header"><header class="topbar"><div class="brand"><div class="brand-mark" data-shop-logo><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 8.5 12 4l8 4.5v9L12 22l-8-4.5zM4 8.5l8 4.5 8-4.5M12 13v9m-8-13 8 4.5 8-4.5" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/></svg><img class="shop-logo" alt="Shop logo" hidden></div><div><span id="appBrandName">ResourceHub</span><small>Digital vendor stock desk</small></div></div><div class="top-right" role="group" aria-label="Shop status and account menu"><div class="system-status" role="status"><span class="live" aria-hidden="true"></span><span class="status-text">Inventory online</span></div><details class="account-menu" id="accountMenu"><summary class="account-name" id="accountName" aria-label="Open account menu"></summary><div class="account-menu-panel" aria-label="Account actions"><button class="logout" id="shopSettingsButton" type="button" hidden>Shop settings</button><button class="logout" id="staffButton" type="button" hidden>Manage staff</button><button class="logout" id="passwordButton" type="button">Change password</button><button class="logout" id="logoutButton" type="button">Sign out</button></div></details></div></header><nav class="app-menu" id="appMenu" aria-label="Main navigation"><a href="#dashboard">Dashboard</a><a href="#inventory">Inventory</a><a href="#assetPassports">Asset passports</a><a href="#transactions">Transactions</a><a href="#loans">Loans</a><a id="reportsNavLink" href="#reportsPanel">Reports</a><a id="auditNavLink" href="#auditPanel">Audit Log</a></nav></div>
  <main>
    <section class="heading" id="dashboard"><div><div class="eyebrow">Operations overview</div><h1>Vendor stock desk</h1><p>Track items in stock, disbursements, and what remains to help plan restocking.</p></div><div class="heading-actions"><div class="today" id="today"></div><div class="actions"><button class="btn" id="addButton">＋ Add item</button><button class="btn secondary" id="restockButton">＋ Stock in</button></div></div></section>
    <section class="stats" aria-label="Inventory summary"><article class="stat"><div class="stat-head"><span class="stat-icon" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="m3 7 9-4 9 4-9 5-9-5Zm0 0v10l9 4 9-4V7M12 12v9"/></svg></span><div class="stat-label">Total items</div></div><div class="stat-value" id="totalUnits">—</div><div class="stat-foot">units across all resources</div></article><article class="stat"><div class="stat-head"><span class="stat-icon" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4 5h16v14H4zM8 9h7M8 13h3m3 2 2 2 4-4"/></svg></span><div class="stat-label">Remaining in stock</div></div><div class="stat-value" id="availableUnits">—</div><div class="stat-foot">units currently in stock</div></article><article class="stat"><div class="stat-head"><span class="stat-icon" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M20 7h-5V2M4 17h5v5"/><path d="M5 7a8 8 0 0 1 13.4-2L20 7M4 17l1.6 1.9A8 8 0 0 0 19 17"/></svg></span><div class="stat-label">Disbursed / on loan</div></div><div class="stat-value" id="borrowedUnits">—</div><div class="stat-foot">units currently disbursed or on loan</div></article><article class="stat"><div class="stat-head"><span class="stat-icon" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="4" y="4" width="6" height="6" rx="1"/><rect x="14" y="4" width="6" height="6" rx="1"/><rect x="4" y="14" width="6" height="6" rx="1"/><rect x="14" y="14" width="6" height="6" rx="1"/></svg></span><div class="stat-label">Item types</div></div><div class="stat-value" id="resourceCount">—</div><div class="stat-foot">items in your catalog</div></article></section>
    <section class="layout"><div class="panel" id="inventory"><div class="panel-head"><div><h2>Shop inventory</h2><div class="subtle">Stocked, disbursed, remaining quantities, and product details</div></div><div class="actions"><input class="search" id="search" type="search" placeholder="Search items or scan SKU/barcode…" aria-label="Search items or scan SKU/barcode"><select class="category-filter" id="categoryFilter" aria-label="Filter by category"><option value="">All categories</option></select><select class="category-filter" id="stockStatusFilter" aria-label="Filter by stock status"><option value="">All stock status</option><option value="out">Out of stock</option><option value="low">Low stock</option><option value="in">In stock</option></select><button class="btn secondary" id="exportInventory" type="button">↓ Export CSV</button><button class="btn secondary" id="printStocktakeButton" type="button">Print count sheet</button><button class="btn secondary" id="stocktakeButton" type="button">Stocktake</button></div></div><div class="table-wrap"><table><thead><tr><th data-sort-header="name" aria-sort="ascending"><button class="table-sort" data-sort="name" type="button">Product <span class="sort-indicator" aria-hidden="true">↑</span></button></th><th data-sort-header="category" aria-sort="none"><button class="table-sort" data-sort="category" type="button">Category <span class="sort-indicator" aria-hidden="true"></span></button></th><th data-sort-header="total" aria-sort="none"><button class="table-sort" data-sort="total" type="button">Stocked <span class="sort-indicator" aria-hidden="true"></span></button></th><th data-sort-header="disbursed" aria-sort="none"><button class="table-sort" data-sort="disbursed" type="button">Disbursed <span class="sort-indicator" aria-hidden="true"></span></button></th><th data-sort-header="available" aria-sort="none"><button class="table-sort" data-sort="available" type="button">Remaining <span class="sort-indicator" aria-hidden="true"></span></button></th><th data-sort-header="price" aria-sort="none"><button class="table-sort" data-sort="price" type="button">Unit price <span class="sort-indicator" aria-hidden="true"></span></button></th><th data-sort-header="status" aria-sort="none"><button class="table-sort" data-sort="status" type="button">Status <span class="sort-indicator" aria-hidden="true"></span></button></th><th></th></tr></thead><tbody id="resourceRows"></tbody></table></div></div>
      <aside class="side"><section class="panel form-panel"><h2>Quick transaction</h2><div class="subtle">Record a sale, loan, or return.</div><div class="tabs"><button class="tab active" data-mode="borrow">Borrow</button><button class="tab" data-mode="return">Return</button><button class="tab" data-mode="sale">Sale</button></div><form id="transactionForm"><input id="selectedLoanId" type="hidden"><div class="field" id="fellowField"><label for="fellow">Fellow</label><select id="fellow" required></select></div><div class="field" id="customerField" hidden><label for="customer">Customer (optional)</label><input id="customer" maxlength="100" placeholder="Customer name"></div><div class="field"><label for="resource">Shop item</label><select id="resource" required></select></div><div class="field" id="dueDateField"><label for="dueDate">Due date</label><input id="dueDate" type="date" required></div><div class="field"><label for="quantity">Quantity</label><input id="quantity" type="number" min="1" step="1" value="1" required></div><button class="btn full" id="transactionButton" type="submit">Record loan</button><div class="form-message" id="transactionMessage" role="status"></div></form></section><section class="panel"><div class="panel-head"><div><h2>Restock watch</h2><div class="subtle">Items at or below their individual restock thresholds</div></div></div><div class="watch-list" id="stockWatch"></div></section>
      <section class="panel" id="loans"><div class="panel-head"><div><h2>Currently on loan</h2><div class="subtle">Due dates and overdue reminders</div></div><div class="actions"><button class="btn secondary" id="exportLoans" type="button">↓ Export CSV</button></div></div><div class="loan-reminder" id="loanReminder" role="status" aria-live="polite" hidden></div><div class="loan-controls"><input class="search" id="loanSearch" type="search" placeholder="Search fellow or item…" aria-label="Search current loans"><select class="category-filter" id="loanFilter" aria-label="Filter current loans"><option value="all">All open loans</option><option value="overdue">Overdue</option><option value="soon">Due within 3 days</option><option value="later">Due later</option><option value="no_date">No due date</option></select></div><div class="loan-list" id="loanList"></div></section><section class="panel"><div class="panel-head"><div><h2>Recent sales</h2><div class="subtle">Latest recorded item disbursements</div></div></div><div class="loan-list" id="salesList"></div></section></aside>
    </section>
    <section class="panel" id="assetPassports" style="margin-top:20px">
      <div class="panel-head"><div><h2>Asset passports</h2><div class="subtle">Track individual equipment, condition, location, and a permanent change history.</div></div><div class="actions"><input class="search" id="assetSearch" type="search" placeholder="Search tag, serial, item, or location…" aria-label="Search asset passports"><button class="btn" id="createAssetButton" type="button" hidden>＋ Register asset</button></div></div>
      <div class="table-wrap"><table><thead><tr><th>Asset tag</th><th>Inventory item</th><th>Serial number</th><th>Location</th><th>Condition</th><th>Status</th><th>History</th></tr></thead><tbody id="assetRows"><tr><td colspan="7" class="empty">Loading asset passports…</td></tr></tbody></table></div>
      <div id="assetMessage" class="form-message" role="status"></div>
    </section>
    <section class="panel" id="transactions" style="margin-top:20px"><div class="panel-head"><div><h2>Transaction history</h2><div class="subtle">Search sales, stock changes, loans, and returns</div></div><div class="actions"><input class="search" id="historySearch" type="search" placeholder="Search item, customer or note…" aria-label="Search transaction history"><select class="category-filter" id="historyType" aria-label="Filter transaction type"><option value="all">All transactions</option><option value="sale">Sales</option><option value="opening_stock">Opening stock</option><option value="restock">Stock-ins</option><option value="borrow">Loans</option><option value="return">Returns</option><option value="damaged">Damaged / lost</option><option value="stocktake_adjustment">Stocktake adjustment</option></select><label class="history-date-label" for="historyFrom">From <input class="category-filter" id="historyFrom" type="date" aria-label="History start date"></label><label class="history-date-label" for="historyTo">To <input class="category-filter" id="historyTo" type="date" aria-label="History end date"></label><button class="btn secondary" id="exportHistory" type="button">↓ Export CSV</button></div></div><div class="table-wrap"><table><thead><tr><th>Date and time</th><th>Type</th><th>Item</th><th>Quantity</th><th>Customer, fellow or note</th></tr></thead><tbody id="historyRows"><tr><td colspan="5" class="empty">Loading transaction history…</td></tr></tbody></table></div><div class="form-message" id="historyMessage" role="status"></div></section>
    <section class="panel" id="reportsPanel" style="margin-top:20px"><div class="panel-head"><div><h2>Reports</h2><div class="subtle" id="reportPeriod">Sales, top items, and current stock value</div></div><div class="actions"><label class="history-date-label" for="reportFrom">From <input class="category-filter" id="reportFrom" type="date" aria-label="Report start date"></label><label class="history-date-label" for="reportTo">To <input class="category-filter" id="reportTo" type="date" aria-label="Report end date"></label><button class="btn secondary" id="exportReport" type="button">↓ Export CSV</button><button class="btn secondary" id="backupButton" type="button">↓ Download backup</button></div></div><div class="stats" style="margin:0 0 18px"><article class="stat"><div class="stat-label">Sales in period</div><div class="stat-value" id="monthRevenue">—</div><div class="stat-foot">recorded sales value</div></article><article class="stat"><div class="stat-label">Units sold in period</div><div class="stat-value" id="monthUnits">—</div><div class="stat-foot">items sold</div></article><article class="stat"><div class="stat-label">All-time sales</div><div class="stat-value" id="lifetimeRevenue">—</div><div class="stat-foot" id="lifetimeUnits">— units sold</div></article><article class="stat"><div class="stat-label">Stock value</div><div class="stat-value" id="stockValue">—</div><div class="stat-foot">remaining units × current unit price</div></article></div><div class="panel-head" style="margin-bottom:8px"><div><h2 style="font-size:15px">Best sellers</h2></div></div><div class="table-wrap"><table><thead><tr><th>Product</th><th>Units sold</th><th>Sales value</th></tr></thead><tbody id="bestSellerRows"><tr><td colspan="3" class="empty">Loading reports…</td></tr></tbody></table></div><div class="subtle" style="margin-top:10px">Revenue uses the price recorded when each sale was made. Sales recorded before price tracking was added may have no revenue value.</div><div class="form-message" id="reportMessage" role="status"></div></section>
    <section class="panel" id="auditPanel" style="margin-top:20px"><div class="panel-head"><div><h2>Audit log</h2><div class="subtle">Who changed what, and when</div></div><div class="actions"><input class="search" id="auditSearch" type="search" placeholder="Search person, action, item…" aria-label="Search audit log"><button class="btn secondary" id="exportAudit" type="button">↓ Export CSV</button></div></div><div class="table-wrap"><table><thead><tr><th>Time</th><th>Who</th><th>Action</th><th>Item / target</th><th>Details</th></tr></thead><tbody id="auditRows"><tr><td colspan="5" class="empty">Loading audit log…</td></tr></tbody></table></div><div class="form-message" id="auditMessage" role="status"></div></section>
    <div class="footnote"><span id="footerBrandName">ResourceHub</span> · Vendor stock management</div>
  </main><div class="toast" id="toast" role="status"></div>
  <dialog id="addDialog"><form id="addForm"><h2 style="margin:0 0 5px">Add shop item</h2><p class="subtle" style="margin:0 0 16px">Create a product and add its starting stock.</p><div class="field"><label for="newId">Item ID</label><input id="newId" placeholder="e.g. P004" required></div><div class="field"><label for="newName">Product name</label><input id="newName" placeholder="e.g. Headphones" required></div><div class="field"><label for="newCategory">Category</label><input id="newCategory" placeholder="e.g. Electronics" required></div><div class="field"><label for="newTotal">Starting stock</label><input id="newTotal" type="number" min="1" step="1" value="1" required></div><div class="field"><label for="newSku">SKU / barcode</label><input id="newSku" maxlength="80"></div><div class="field"><label for="newPrice">Unit price</label><input id="newPrice" type="number" min="0" step="0.01" value="0" required></div><div class="field"><label for="newSupplier">Supplier</label><input id="newSupplier" maxlength="100"></div><div class="field"><label for="newThreshold">Restock alert at</label><input id="newThreshold" type="number" min="0" step="1" value="3" required></div><div style="display:flex;gap:9px;justify-content:flex-end;margin-top:18px"><button type="button" class="btn secondary" id="cancelAdd">Cancel</button><button class="btn" type="submit">Add item</button></div><div id="addMessage" class="form-message" role="status"></div></form></dialog>
  <dialog id="detailsDialog"><form id="detailsForm"><h2 style="margin:0 0 5px">Product details</h2><p class="subtle" id="detailsItemName" style="margin:0 0 16px"></p><input id="detailsId" type="hidden"><div class="field"><label for="detailsSku">SKU / barcode</label><input id="detailsSku" maxlength="80"></div><div class="field"><label for="detailsPrice">Unit price</label><input id="detailsPrice" type="number" min="0" step="0.01" required></div><div class="field"><label for="detailsSupplier">Supplier</label><input id="detailsSupplier" maxlength="100"></div><div class="field"><label for="detailsThreshold">Restock alert at</label><input id="detailsThreshold" type="number" min="0" step="1" required></div><div style="display:flex;gap:9px;justify-content:flex-end;margin-top:18px"><button type="button" class="btn secondary" id="cancelDetails">Cancel</button><button class="btn" type="submit">Save details</button></div><div id="detailsMessage" class="form-message" role="status"></div></form></dialog>
    <dialog id="assetCreateDialog"><form id="assetCreateForm"><h2 style="margin:0 0 5px">Register an individual asset</h2><p class="subtle" style="margin:0 0 16px">This creates a passport for one physical item and does not change inventory quantities.</p><div class="field"><label for="assetResource">Inventory item</label><select id="assetResource" required></select></div><div class="field"><label for="assetSerial">Serial number</label><input id="assetSerial" maxlength="120"></div><div class="field"><label for="assetLocation">Current location</label><input id="assetLocation" maxlength="120" placeholder="e.g. Lagos office · Room 2"></div><div class="field"><label for="assetCondition">Condition</label><select id="assetCondition"><option value="good">Good</option><option value="fair">Fair</option><option value="poor">Poor</option><option value="damaged">Damaged</option></select></div><div class="field"><label for="assetNotes">Notes</label><textarea id="assetNotes" maxlength="1000" rows="3"></textarea></div><div style="display:flex;gap:9px;justify-content:flex-end;margin-top:18px"><button type="button" class="btn secondary" id="cancelAssetCreate">Cancel</button><button class="btn" type="submit">Create passport</button></div><div id="assetCreateMessage" class="form-message" role="status"></div></form></dialog>
  <dialog id="assetDetailDialog"><form id="assetDetailForm"><h2 id="assetDetailTitle" style="margin:0 0 5px">Asset passport</h2><p class="subtle" id="assetDetailSubtitle" style="margin:0 0 16px"></p><input id="assetDetailTag" type="hidden"><div class="field"><label for="assetDetailLocation">Location</label><input id="assetDetailLocation" maxlength="120" required></div><div class="field"><label for="assetDetailCondition">Condition</label><select id="assetDetailCondition"><option value="good">Good</option><option value="fair">Fair</option><option value="poor">Poor</option><option value="damaged">Damaged</option></select></div><div class="field"><label for="assetDetailStatus">Status</label><select id="assetDetailStatus"><option value="available">Available</option><option value="assigned">Assigned</option><option value="maintenance">Maintenance</option><option value="retired">Retired</option></select></div><div class="field"><label for="assetDetailNotes">Notes</label><textarea id="assetDetailNotes" maxlength="1000" rows="2"></textarea></div><h3 style="margin:18px 0 8px">Asset history</h3><div class="table-wrap" style="max-height:24vh"><table><thead><tr><th>When</th><th>Event</th><th>Details</th><th>By</th></tr></thead><tbody id="assetEventRows"></tbody></table></div>
<h3 style="margin:20px 0 8px">Maintenance &amp; repairs</h3>
<p class="subtle" style="margin:0 0 12px">Record repairs for this individual asset. Maintenance records do not change stock quantities.</p>
<div id="maintenanceCreatePanel">
  <div class="field"><label for="maintenanceProblem">Reported problem</label><textarea id="maintenanceProblem" maxlength="1000" rows="2" required placeholder="Describe the fault or damage"></textarea></div>
  <div class="field"><label for="maintenanceAction">Action taken</label><textarea id="maintenanceAction" maxlength="1000" rows="2" placeholder="Describe the repair or inspection"></textarea></div>
  <div class="field"><label for="maintenanceCost">Repair cost</label><input id="maintenanceCost" type="number" min="0" step="0.01" value="0" required></div>
  <div class="field"><label for="maintenanceTechnician">Technician</label><input id="maintenanceTechnician" maxlength="120" placeholder="Technician or service provider"></div>
  <div class="field"><label for="maintenanceStatus">Status</label><select id="maintenanceStatus"><option value="open">Open</option><option value="in_progress">In progress</option><option value="completed">Completed</option><option value="cancelled">Cancelled</option></select></div>
  <button class="btn" id="createMaintenanceButton" type="button">Record maintenance</button>
</div>
<div id="maintenanceMessage" class="form-message" role="status"></div>
<div class="table-wrap" style="max-height:36vh;margin-top:12px"><table><thead><tr><th>Reported</th><th>Problem / action</th><th>Cost</th><th>Technician</th><th>Status</th><th>Manage</th></tr></thead><tbody id="maintenanceRows"><tr><td colspan="6" class="empty">Loading maintenance records…</td></tr></tbody></table></div>
<div style="display:flex;gap:9px;justify-content:flex-end;margin-top:18px"><button type="button" class="btn secondary" id="cancelAssetDetail">Close</button><button class="btn" id="saveAssetDetail" type="submit">Save changes</button></div><div id="assetDetailMessage" class="form-message" role="status"></div></form></dialog>
  <dialog id="stocktakeDialog"><form id="stocktakeForm"><h2 style="margin:0 0 5px">Stocktake</h2><p class="subtle" style="margin:0 0 14px">Count units currently on the shelf. Items out on loan are excluded. Leave uncounted items blank; enter 0 if none are present.</p><div class="table-wrap" style="max-height:48vh"><table><thead><tr><th>Item</th><th>Expected on shelf</th><th>Counted on shelf</th></tr></thead><tbody id="stocktakeRows"></tbody></table></div><div class="field" style="margin-top:14px"><label for="stocktakeNote">Stocktake note</label><input id="stocktakeNote" maxlength="200" placeholder="e.g. Monthly count" required></div><div style="display:flex;gap:9px;justify-content:flex-end;margin-top:18px"><button type="button" class="btn secondary" id="cancelStocktake">Cancel</button><button class="btn" type="submit">Review adjustments</button></div><div id="stocktakeMessage" class="form-message" role="status"></div></form></dialog>
<dialog id="restockDialog"><form id="restockForm"><h2 style="margin:0 0 5px">Stock in items</h2><p class="subtle" style="margin:0 0 16px">Record newly received units and update remaining stock.</p><div class="field"><label for="restockItem">Shop item</label><select id="restockItem" required></select></div><div class="field"><label for="restockQuantity">Quantity received</label><input id="restockQuantity" type="number" min="1" step="1" value="1" required></div><div style="display:flex;gap:9px;justify-content:flex-end;margin-top:18px"><button type="button" class="btn secondary" id="cancelRestock">Cancel</button><button class="btn" type="submit">Update stock</button></div><div id="restockMessage" class="form-message" role="status"></div></form></dialog>
  <dialog id="staffDialog"><h2 style="margin:0 0 5px">Staff accounts</h2><p class="subtle" style="margin:0 0 16px">Create staff logins for sales, loans, and returns.</p><div class="table-wrap"><table><thead><tr><th>Name</th><th>Username</th><th>Role</th><th>Manage</th></tr></thead><tbody id="staffRows"></tbody></table></div><form id="staffForm" style="margin-top:22px"><h3 style="font-size:14px">Add staff member</h3><div class="field"><label for="staffName">Name</label><input id="staffName" maxlength="80" required></div><div class="field"><label for="staffUsername">Username</label><input id="staffUsername" minlength="3" maxlength="40" required></div><div class="field"><label for="staffPassword">Initial password</label><input id="staffPassword" type="password" minlength="10" autocomplete="new-password" required></div><div style="display:flex;gap:9px;justify-content:flex-end;margin-top:18px"><button type="button" class="btn secondary" id="closeStaff">Close</button><button class="btn" type="submit">Create staff account</button></div><div id="staffMessage" class="form-message" role="status"></div></form></dialog>
  <dialog id="shopSettingsDialog"><form id="shopSettingsForm"><h2 style="margin:0 0 5px">Shop settings</h2><p class="subtle" style="margin:0 0 16px">Set the identity customers and staff see throughout this site.</p><div class="field"><label for="shopName">Shop name</label><input id="shopName" maxlength="60" required></div><div class="field"><label for="shopLogo">Logo image (PNG, JPEG, or WebP · max 250 KB)</label><input id="shopLogo" type="file" accept="image/png,image/jpeg,image/webp"><div style="display:flex;align-items:center;gap:12px;margin-top:10px"><img id="shopLogoPreview" alt="Logo preview" hidden style="width:48px;height:48px;object-fit:contain;border:1px solid var(--line);border-radius:10px;padding:3px"><button type="button" class="btn secondary" id="removeShopLogo">Remove logo</button></div></div><div class="field"><label for="shopColor">Brand color</label><div style="display:flex;align-items:center;gap:10px"><input id="shopColor" type="color" style="width:54px;height:42px;padding:4px"><span class="subtle">Used for buttons, highlights, and the logo tile.</span></div></div><div class="field"><label for="shopCurrency">Currency code (ISO 4217)</label><input id="shopCurrency" maxlength="3" minlength="3" pattern="[A-Za-z]{3}" placeholder="NGN" required></div><div style="display:flex;gap:9px;justify-content:flex-end;margin-top:18px"><button type="button" class="btn secondary" id="cancelShopSettings">Cancel</button><button class="btn" type="submit">Save shop settings</button></div><div id="shopSettingsMessage" class="form-message" role="status"></div>
<div class="field">
  <h3>Email delivery</h3>
  <p>SMTP credentials are read securely from the server's private .env file.</p>
  <label for="testEmailAddress">Send a test email to</label>
  <input id="testEmailAddress" type="email" maxlength="254"
         placeholder="you@example.com" autocomplete="email" required>
  <button type="button" class="btn secondary" id="testEmailButton">Send test email</button>
  <div id="testEmailMessage" class="form-message" role="status"></div>
</div>
</form></dialog>
  <dialog id="passwordDialog"><form id="passwordForm"><h2 style="margin:0 0 5px">Change password</h2><p class="subtle" style="margin:0 0 16px">You stay signed in on this device. Your other devices are signed out.</p><div class="field"><label for="currentPassword">Current password</label><input id="currentPassword" type="password" autocomplete="current-password" required></div><div class="field"><label for="newPassword">New password (at least 10 characters)</label><input id="newPassword" type="password" minlength="10" autocomplete="new-password" required></div><div style="display:flex;gap:9px;justify-content:flex-end;margin-top:18px"><button type="button" class="btn secondary" id="cancelPassword">Cancel</button><button class="btn" type="submit">Save new password</button></div><div id="passwordMessage" class="form-message" role="status"></div></form></dialog>
  <dialog id="confirmDialog"><h2 id="confirmTitle" style="margin:0 0 6px">Confirm inventory change</h2><p id="confirmDetail" class="subtle" style="margin:0 0 20px"></p><div style="display:flex;gap:9px;justify-content:flex-end"><button type="button" class="btn secondary" id="confirmCancel">Cancel</button><button type="button" class="btn" id="confirmOk">Confirm</button></div></dialog>
  <dialog id="loanDueDateDialog"><form id="loanDueDateForm"><h2 style="margin:0 0 5px">Change loan due date</h2><p class="subtle" id="loanDueDateDetail" style="margin:0 0 16px"></p><input id="loanDueId" type="hidden"><div class="field"><label for="loanDueDate">New due date</label><input id="loanDueDate" type="date" required></div><div style="display:flex;gap:9px;justify-content:flex-end;margin-top:18px"><button type="button" class="btn secondary" id="cancelLoanDueDate">Cancel</button><button class="btn" type="submit">Save due date</button></div><div id="loanDueDateMessage" class="form-message" role="status"></div></form></dialog>
  <section id="printStocktakeSheet" aria-hidden="true"><h1 id="printStocktakeTitle">Stocktake count sheet</h1><p id="printStocktakeDate"></p><p>Count units on the shelf. Exclude units currently on loan.</p><table><thead><tr><th>Item</th><th>Item ID</th><th>SKU / barcode</th><th>Category</th><th>Expected on shelf</th><th>Counted on shelf</th></tr></thead><tbody id="printStocktakeRows"></tbody></table></section>
  <script>
    const $ = selector => document.querySelector(selector);
    const landingNav=$('.landing-nav');
    const updateLandingNavState=()=>landingNav.classList.toggle('is-scrolled',window.scrollY>2);
    window.addEventListener('scroll',updateLandingNavState,{passive:true});
    updateLandingNavState();
    const passwordToggle=$('.auth-password-toggle'),passwordInput=$('#authPassword');passwordToggle.addEventListener('click',()=>{const visible=passwordInput.type==='password';passwordInput.type=visible?'text':'password';passwordToggle.setAttribute('aria-label',visible?'Hide password':'Show password');passwordToggle.setAttribute('aria-pressed',String(visible));passwordToggle.classList.toggle('is-visible',visible)});
    let state = null, mode = 'borrow', currentRole = 'staff', registrationOpen = false, toastTimer, historyTimer, pendingLogo = null, turnstileSiteKey = '', inventorySortKey = 'name', inventorySortDirection = 'asc';
    $('#today').textContent = new Intl.DateTimeFormat(undefined,{weekday:'long',month:'long',day:'numeric',year:'numeric'}).format(new Date());
    const pageLinks=[...document.querySelectorAll('#appMenu a')];
    function updateActivePageLink(){const current=location.hash||'#dashboard';pageLinks.forEach(link=>{const active=link.getAttribute('href')===current;link.classList.toggle('active',active);if(active)link.setAttribute('aria-current','page');else link.removeAttribute('aria-current')})}
    window.addEventListener('hashchange',updateActivePageLink);updateActivePageLink();
    document.addEventListener('click',event=>{const menu=$('#accountMenu');if(menu.open&&!menu.contains(event.target))menu.open=false});
    document.addEventListener('keydown',event=>{if(event.key!=='Escape'||document.querySelector('dialog[open]'))return;const menu=$('#accountMenu');if(menu.open){menu.open=false;menu.querySelector('summary').focus()}});

    $('#accountMenu').addEventListener('click',event=>{if(event.target.closest('button'))$('#accountMenu').open=false});
    function esc(value){return String(value).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
    async function api(path, body){const response=await fetch(path,{method:body?'POST':'GET',headers:body?{'Content-Type':'application/json'}:{},body:body?JSON.stringify(body):undefined});const data=await response.json();if(!response.ok)throw new Error(data.error||'Something went wrong.');return data}
    function money(value){const currency=state?.settings?.currency||'NGN';try{return new Intl.NumberFormat(undefined,{style:'currency',currency}).format(Number(value)||0)}catch(error){return Number(value||0).toFixed(2)+' '+currency}}
    function applyShopSettings(settings){if(!settings)return;const name=settings.shop_name||'ResourceHub',color=settings.brand_color||'#536dfe';const rgb=[1,3,5].map(i=>parseInt(color.slice(i,i+2),16)),onColor=(rgb[0]*299+rgb[1]*587+rgb[2]*114)/1000>150?'#16212b':'#ffffff';document.documentElement.style.setProperty('--on-blue',onColor);document.documentElement.style.setProperty('--blue',color);document.title=`${name} · Vendor stock desk`;['landingBrandName','landingCtaBrand','authBrandName','authPanelBrandName','appBrandName','footerBrandName'].forEach(id=>{const element=$('#'+id);if(element)element.textContent=name});document.querySelectorAll('[data-shop-logo]').forEach(mark=>{const image=mark.querySelector('.shop-logo'),symbol=mark.querySelector('svg');mark.style.backgroundColor=settings.logo_data?'transparent':color;image.hidden=!settings.logo_data;symbol.hidden=!!settings.logo_data;if(settings.logo_data)image.src=settings.logo_data;else image.removeAttribute('src')})}
    function confirmAction(title,detail,buttonLabel='Confirm'){const dialog=$('#confirmDialog');$('#confirmTitle').textContent=title;$('#confirmDetail').textContent=detail;$('#confirmOk').textContent=buttonLabel;return new Promise(resolve=>{let accepted=false;const closed=()=>{dialog.removeEventListener('close',closed);resolve(accepted)};$('#confirmCancel').onclick=()=>{accepted=false;dialog.close()};$('#confirmOk').onclick=()=>{accepted=true;dialog.close()};dialog.addEventListener('close',closed,{once:true});dialog.showModal()})}
    function showToast(message){const box=$('#toast');box.textContent=message;box.classList.add('show');clearTimeout(toastTimer);toastTimer=setTimeout(()=>box.classList.remove('show'),2800)}
    $('#stockWatch').addEventListener('click',event=>{const button=event.target.closest('.quick-restock');if(!button||currentRole!=='owner')return;const item=state.resources.find(resource=>resource.id===button.dataset.id);if(!item)return;$('#restockItem').innerHTML=state.resources.map(resource=>`<option value="${esc(resource.id)}">${esc(resource.name)} (${esc(resource.id)} · ${resource.available} remaining)</option>`).join('');$('#restockItem').value=item.id;$('#restockQuantity').value=String(Math.max(1,Number(item.reorder_level||0)+1-Number(item.available||0)));$('#restockMessage').className='form-message';$('#restockMessage').textContent='';$('#restockDialog').showModal();$('#restockQuantity').focus()});
    function inventorySortValue(item,key){if(key==='total')return Number(item.total)||0;if(key==='disbursed')return (Number(item.total)||0)-(Number(item.available)||0);if(key==='available')return Number(item.available)||0;if(key==='price')return Number(item.price)||0;if(key==='status')return item.available===0?0:item.available<=item.reorder_level?1:2;if(key==='category')return item.category||'';return item.name||''}
    function sortInventoryItems(items){return items.sort((a,b)=>{const left=inventorySortValue(a,inventorySortKey),right=inventorySortValue(b,inventorySortKey),comparison=typeof left==='number'?left-right:String(left).localeCompare(String(right),undefined,{numeric:true,sensitivity:'base'});return (inventorySortDirection==='asc'?comparison:-comparison)||String(a.id).localeCompare(String(b.id),undefined,{numeric:true,sensitivity:'base'})})}
    function updateInventorySortHeaders(){document.querySelectorAll('#inventory [data-sort-header]').forEach(header=>{const active=header.dataset.sortHeader===inventorySortKey;header.setAttribute('aria-sort',active?(inventorySortDirection==='asc'?'ascending':'descending'):'none');header.querySelector('.sort-indicator').textContent=active?(inventorySortDirection==='asc'?'↑':'↓'):''})}
    document.querySelectorAll('#inventory .table-sort').forEach(button=>button.addEventListener('click',()=>{if(inventorySortKey===button.dataset.sort)inventorySortDirection=inventorySortDirection==='asc'?'desc':'asc';else{inventorySortKey=button.dataset.sort;inventorySortDirection='asc'}render()}));
    $('#printStocktakeButton').addEventListener('click',()=>{if(!state){showToast('Inventory is still loading.');return}const search=$('#search').value.trim().toLowerCase(),category=$('#categoryFilter').value.toLowerCase(),items=sortInventoryItems(state.resources.filter(item=>{const matchesSearch=`${item.name} ${item.category} ${item.id} ${item.sku||''}`.toLowerCase().includes(search),matchesCategory=!category||item.category.toLowerCase()===category;return matchesSearch&&matchesCategory}));if(!items.length){showToast('No inventory items match the current search and category.');return}$('#printStocktakeTitle').textContent=`${state.settings.shop_name||'ResourceHub'} · Stocktake count sheet`;$('#printStocktakeDate').textContent=`Printed ${new Date().toLocaleString()}`;$('#printStocktakeRows').innerHTML=items.map(item=>`<tr><td>${esc(item.name)}</td><td>${esc(item.id)}</td><td>${esc(item.sku||'—')}</td><td>${esc(item.category)}</td><td>${item.available}</td><td></td></tr>`).join('');const sheet=$('#printStocktakeSheet');sheet.classList.add('print-ready');window.addEventListener('afterprint',()=>sheet.classList.remove('print-ready'),{once:true});window.print()});
    function render(){if(!state)return;applyShopSettings(state.settings);const resources=state.resources,filter=$('#search').value.trim().toLowerCase(),categoryControl=$('#categoryFilter'),oldCategory=categoryControl.value,categories=[...new Set(resources.map(r=>r.category))].sort((a,b)=>a.localeCompare(b));categoryControl.innerHTML='<option value="">All categories</option>'+categories.map(name=>`<option value="${esc(name)}">${esc(name)}</option>`).join('');if(categories.includes(oldCategory))categoryControl.value=oldCategory;const category=categoryControl.value.toLowerCase(),stockStatus=$('#stockStatusFilter').value;const shown=sortInventoryItems(resources.filter(r=>{const matchesSearch=(r.name+' '+r.category+' '+r.id+' '+(r.sku||'')).toLowerCase().includes(filter),matchesCategory=!category||r.category.toLowerCase()===category,matchesStatus=!stockStatus||(stockStatus==='out'?r.available===0:stockStatus==='low'?r.available>0&&r.available<=r.reorder_level:r.available>r.reorder_level);return matchesSearch&&matchesCategory&&matchesStatus}));updateInventorySortHeaders();
      $('#totalUnits').textContent=state.summary.total;$('#availableUnits').textContent=state.summary.available;$('#borrowedUnits').textContent=state.summary.borrowed;$('#resourceCount').textContent=resources.length;
      $('#resourceRows').innerHTML=shown.length?shown.map(r=>{const status=r.available===0?['Out of stock','out']:r.available<=r.reorder_level?['Restock soon','low']:['In stock',''];return `<tr><td><div class="resource-name">${esc(r.name)}</div><div class="resource-id">${esc(r.id)}${r.sku?' · '+esc(r.sku):''}</div></td><td>${esc(r.category)}</td><td>${r.total}</td><td>${r.total-r.available}</td><td><strong>${r.available}</strong></td><td>${money(r.price)}</td><td><span class="badge ${status[1]}">${status[0]}</span></td><td>${currentRole==='owner'?`<button class="btn secondary edit-details" data-id="${esc(r.id)}" type="button">Details</button> <button class="btn secondary delete-resource" data-id="${esc(r.id)}" type="button">Delete</button>`:''}</td></tr>`}).join(''):`<tr><td colspan="8" class="empty">${resources.length?'No items match your search.':'<div class="inventory-empty" role="status"><span class="inventory-empty-icon" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7.5 12 3l9 4.5v9L12 21l-9-4.5zM3 7.5 12 12m0 9v-9m9-4.5L12 12M8 5l8 4"/></svg></span><p>Add your first item to start tracking stock.</p>'+(currentRole==='owner'?'<button class="btn" type="button" data-empty-add>Add your first item</button>':'')+'</div>'}</td></tr>`;
      const lowStock=resources.filter(r=>r.available<=r.reorder_level);$('#stockWatch').innerHTML=lowStock.length?lowStock.map(r=>`<div class="watch-row"><div><div class="watch-name">${esc(r.name)}</div><div class="watch-detail">${esc(r.id)} · alert at ${r.reorder_level}</div></div><span class="watch-count ${r.available===0?'out':''}">${r.available} remaining</span>${currentRole==='owner'?`<button class="btn secondary quick-restock" type="button" data-id="${esc(r.id)}" aria-label="Stock in ${esc(r.name)}">Stock in</button>`:''}</div>`).join(''):'<div class="empty" style="padding:14px">All items are above their restock thresholds.</div>';
      const dueDate=$('#dueDate'), today=localDateValue(0);dueDate.min=today;if(!dueDate.value)dueDate.value=localDateValue(state.loan_days||14);
      const fellowSelect=$('#fellow'), previousFellow=fellowSelect.value;fellowSelect.innerHTML=state.fellows.map(f=>`<option value="${esc(f.id)}">${esc(f.name)}</option>`).join('');if(state.fellows.some(f=>f.id===previousFellow))fellowSelect.value=previousFellow;
      const select=$('#resource'), previous=select.value, choices=mode==='return'?state.loans.filter(l=>l.fellow_id===fellowSelect.value):resources.filter(r=>r.available>0);select.innerHTML=choices.map(r=>{const id=r.id||r.resource_id;const item=resources.find(x=>x.id===id);const count=mode==='return'?r.quantity+' on loan':r.available+' remaining';return `<option value="${esc(id)}">${esc(item?.name||r.name)} (${count})</option>`}).join('');if(choices.some(r=>(r.id||r.resource_id)===previous))select.value=previous;
      $('#fellowField').hidden=mode==='sale';$('#fellow').required=mode!=='sale';$('#customerField').hidden=mode!=='sale';$('#dueDateField').hidden=mode!=='borrow';$('#dueDate').required=mode==='borrow';$('#resource').disabled=!choices.length;$('#transactionButton').disabled=!choices.length;$('#transactionButton').textContent=mode==='borrow'?'Record loan':mode==='return'?'Record return':'Record sale';
      const loanRecords=state.loan_records||[],todayValue=localDateValue(0),overdueLoans=loanRecords.filter(l=>l.due_date&&l.due_date<todayValue),soonLoans=loanRecords.filter(l=>l.due_date&&l.due_date>=todayValue&&l.due_date<=localDateValue(3));const reminder=$('#loanReminder');if(overdueLoans.length||soonLoans.length){const overdueUnits=overdueLoans.reduce((sum,l)=>sum+l.quantity,0),soonUnits=soonLoans.reduce((sum,l)=>sum+l.quantity,0),parts=[];if(overdueLoans.length)parts.push(`${overdueLoans.length} overdue loan${overdueLoans.length===1?'':'s'} (${overdueUnits} unit${overdueUnits===1?'':'s'})`);if(soonLoans.length)parts.push(`${soonLoans.length} due within 3 days (${soonUnits} unit${soonUnits===1?'':'s'})`);reminder.textContent=parts.join(' · ');reminder.classList.toggle('overdue',overdueLoans.length>0);reminder.hidden=false}else{reminder.textContent='';reminder.classList.remove('overdue');reminder.hidden=true}
      const visibleLoans=filteredLoanRecords();
      $('#loanList').innerHTML=visibleLoans.length?visibleLoans.map(l=>{const days=l.due_date?dateOffset(l.due_date):null,kind=days!==null&&days<0?'overdue':days!==null&&days<=3?'soon':'',dueLabel=!l.due_date?'No due date':days<0?`Overdue by ${Math.abs(days)} day${Math.abs(days)===1?'':'s'} · due ${formatDueDate(l.due_date)}`:days===0?`Due today · ${formatDueDate(l.due_date)}`:days===1?`Due tomorrow · ${formatDueDate(l.due_date)}`:days<=3?`Due in ${days} days · ${formatDueDate(l.due_date)}`:`Due ${formatDueDate(l.due_date)}`;return `<div class="loan-row ${kind}"><div><div class="loan-who">${esc(l.fellow_name)}</div><div class="loan-what">${esc(l.resource_name)}</div><div class="loan-due ${kind}">${esc(dueLabel)}</div></div><div class="loan-actions"><div class="loan-count">${l.quantity} unit${l.quantity===1?'':'s'}</div><button class="btn secondary loan-return" type="button" data-loan-id="${l.loan_id}" data-quantity="${l.quantity}" data-fellow="${esc(l.fellow_id)}" data-resource="${esc(l.resource_id)}" aria-label="Prepare return for ${esc(l.fellow_name)}: ${esc(l.resource_name)}">Prepare return</button><button class="btn secondary loan-date-update" type="button" data-loan-id="${l.loan_id}" data-fellow="${esc(l.fellow_name)}" data-resource-name="${esc(l.resource_name)}" data-due-date="${esc(l.due_date||'')}" aria-label="Change due date for ${esc(l.fellow_name)}: ${esc(l.resource_name)}">Change due date</button></div></div>`}).join(''):loanRecords.length?'<div class="empty" style="padding:15px 0">No loans match this search or filter.</div>':'<div class="empty" style="padding:15px 0">Nothing is currently on loan.</div>';
      $('#salesList').innerHTML=state.sales.length?state.sales.map(s=>`<div class="loan-row"><div><div class="loan-who">${esc(s.resource_name)} × ${s.quantity}</div><div class="loan-what">${esc(s.customer||'Walk-in customer')} · ${esc(new Date(s.created_at).toLocaleString())}</div></div><div class="loan-count">Sale</div></div>`).join(''):'<div class="empty" style="padding:15px 0">No sales recorded yet.</div>';
    }
    function localDateValue(offset=0){const day=new Date();day.setDate(day.getDate()+offset);return `${day.getFullYear()}-${String(day.getMonth()+1).padStart(2,'0')}-${String(day.getDate()).padStart(2,'0')}`}
    function dateOffset(value){const [year,month,day]=value.split('-').map(Number),[todayYear,todayMonth,todayDay]=localDateValue().split('-').map(Number);return Math.round((Date.UTC(year,month-1,day)-Date.UTC(todayYear,todayMonth-1,todayDay))/86400000)}
    function formatDueDate(value){return new Date(`${value}T00:00:00`).toLocaleDateString(undefined,{year:'numeric',month:'short',day:'numeric'})}
    function filteredLoanRecords(){const search=$('#loanSearch').value.trim().toLowerCase(),status=$('#loanFilter').value;return (state?.loan_records||[]).filter(loan=>{const days=loan.due_date?dateOffset(loan.due_date):null,matchesStatus=status==='all'||status==='overdue'&&days<0||status==='soon'&&days!==null&&days>=0&&days<=3||status==='later'&&days!==null&&days>3||status==='no_date'&&days===null,matchesSearch=`${loan.fellow_name} ${loan.fellow_id} ${loan.resource_name} ${loan.resource_id}`.toLowerCase().includes(search);return matchesStatus&&matchesSearch})}
    function csvCell(value){const cell=String(value??''),safe=/^[\s]*[=+\-@]/.test(cell)?"'"+cell:cell;return `"${safe.replace(/"/g,'""')}"`}
    async function refresh(){state=await api('/api/state');render();const loads=[loadHistory(),loadAssetPassports()];if(currentRole==='owner'){loads.push(loadReports());loads.push(loadAudit())}await Promise.all(loads)}
    async function loadHistory(){const query=new URLSearchParams({q:$('#historySearch').value,action:$('#historyType').value,from:$('#historyFrom').value,to:$('#historyTo').value});try{const result=await api('/api/history?'+query.toString());$('#historyMessage').textContent='';const labels={sale:'Sale',opening_stock:'Opening stock',restock:'Stock-in',borrow:'Loan',return:'Return',damaged:'Damaged / lost',stocktake_adjustment:'Stocktake adjustment'};$('#historyRows').innerHTML=result.history.length?result.history.map(row=>`<tr><td>${esc(new Date(row.created_at).toLocaleString())}</td><td>${esc(labels[row.action]||row.action)}</td><td><div class="resource-name">${esc(row.item)}</div><div class="resource-id">${esc(row.resource_id)}</div></td><td>${row.action==='stocktake_adjustment'&&row.quantity>0?'+':''}${row.quantity}</td><td>${esc(row.party||'—')}</td></tr>`).join(''):'<tr><td colspan="5" class="empty">No transactions match your filters.</td></tr>'}catch(error){$('#historyMessage').className='form-message error';$('#historyMessage').textContent=error.message}}
    function scheduleHistory(){clearTimeout(historyTimer);historyTimer=setTimeout(loadHistory,180)}
    $('#historySearch').addEventListener('input',scheduleHistory);$('#historyType').addEventListener('change',loadHistory);$('#historyFrom').addEventListener('change',loadHistory);$('#historyTo').addEventListener('change',loadHistory);
    $('#exportHistory').addEventListener('click',async()=>{try{const query=new URLSearchParams({q:$('#historySearch').value,action:$('#historyType').value,from:$('#historyFrom').value,to:$('#historyTo').value}),result=await api('/api/history?'+query.toString());if(!result.history.length){showToast('No transactions match the current search, type, and date range.');return}const rows=[['date_time','action','item_id','item_name','quantity','customer_fellow_or_note'],...result.history.map(row=>[row.created_at,row.action,row.resource_id,row.item,row.quantity,row.party||''])],csv='\ufeff'+rows.map(row=>row.map(csvCell).join(',')).join('\r\n'),file=URL.createObjectURL(new Blob([csv],{type:'text/csv;charset=utf-8'})),link=document.createElement('a');link.href=file;link.download='resourcehub-transaction-history.csv';link.click();setTimeout(()=>URL.revokeObjectURL(file),1000)}catch(error){$('#historyMessage').className='form-message error';$('#historyMessage').textContent=error.message}});
    async function loadReports(){try{const today=localDateValue();if(!$('#reportFrom').value&&!$('#reportTo').value){$('#reportFrom').value=`${today.slice(0,7)}-01`;$('#reportTo').value=today}const query=new URLSearchParams({from:$('#reportFrom').value,to:$('#reportTo').value}),report=await api('/api/reports?'+query.toString());$('#reportMessage').textContent='';$('#reportPeriod').textContent=`Sales, best sellers, and current stock value · ${report.period}`;$('#monthRevenue').textContent=money(report.period_revenue);$('#monthUnits').textContent=report.period_units;$('#lifetimeRevenue').textContent=money(report.lifetime_revenue);$('#lifetimeUnits').textContent=`${report.lifetime_units} units sold`;$('#stockValue').textContent=money(report.stock_value);$('#bestSellerRows').innerHTML=report.best_sellers.length?report.best_sellers.map(item=>`<tr><td><div class="resource-name">${esc(item.name)}</div><div class="resource-id">${esc(item.id)}</div></td><td>${item.units}</td><td>${money(item.revenue)}</td></tr>`).join(''):'<tr><td colspan="3" class="empty">No sales recorded in this period.</td></tr>'}catch(error){$('#reportMessage').className='form-message error';$('#reportMessage').textContent=error.message}}
    $('#reportFrom').addEventListener('change',loadReports);$('#reportTo').addEventListener('change',loadReports);
    $('#exportInventory').addEventListener('click',()=>{const search=$('#search').value.trim().toLowerCase(),category=$('#categoryFilter').value.toLowerCase(),stockStatus=$('#stockStatusFilter').value,resources=state.resources.filter(item=>{const matchesSearch=`${item.name} ${item.category} ${item.id} ${item.sku||''}`.toLowerCase().includes(search),matchesCategory=!category||item.category.toLowerCase()===category,matchesStatus=!stockStatus||(stockStatus==='out'?item.available===0:stockStatus==='low'?item.available>0&&item.available<=item.reorder_level:item.available>item.reorder_level);return matchesSearch&&matchesCategory&&matchesStatus});if(!resources.length){showToast('No inventory items match the current filters.');return}const rows=[['item_id','name','sku','category','stocked','disbursed','remaining','unit_price','supplier','reorder_level','status'],...resources.map(item=>[item.id,item.name,item.sku||'',item.category,item.total,item.total-item.available,item.available,item.price,item.supplier||'',item.reorder_level,item.available===0?'Out of stock':item.available<=item.reorder_level?'Restock soon':'In stock'])],csv='\ufeff'+rows.map(row=>row.map(csvCell).join(',')).join('\r\n'),file=URL.createObjectURL(new Blob([csv],{type:'text/csv;charset=utf-8'})),link=document.createElement('a');link.href=file;link.download='resourcehub-inventory.csv';link.click();setTimeout(()=>URL.revokeObjectURL(file),1000)});
    $('#exportLoans').addEventListener('click',()=>{const records=filteredLoanRecords();if(!records.length){showToast('No loans match the current search and filter.');return}const rows=[['fellow_id','fellow_name','item_id','item_name','outstanding_quantity','due_date','status','days_overdue'],...records.map(loan=>{const days=loan.due_date?dateOffset(loan.due_date):null,status=days===null?'No due date':days<0?'Overdue':days===0?'Due today':days<=3?'Due soon':'On time';return [loan.fellow_id,loan.fellow_name,loan.resource_id,loan.resource_name,loan.quantity,loan.due_date||'',status,days!==null&&days<0?Math.abs(days):0]})],csv='\ufeff'+rows.map(row=>row.map(csvCell).join(',')).join('\r\n'),file=URL.createObjectURL(new Blob([csv],{type:'text/csv;charset=utf-8'})),link=document.createElement('a');link.href=file;link.download='resourcehub-loans.csv';link.click();setTimeout(()=>URL.revokeObjectURL(file),1000)});
    $('#exportReport').addEventListener('click',async()=>{try{const query=new URLSearchParams({from:$('#reportFrom').value,to:$('#reportTo').value}),response=await fetch('/api/reports/export.csv?'+query.toString());if(!response.ok){const data=await response.json();throw new Error(data.error||'Could not export report.')}const file=URL.createObjectURL(await response.blob()),link=document.createElement('a');link.href=file;link.download='resourcehub-report.csv';link.click();setTimeout(()=>URL.revokeObjectURL(file),1000)}catch(error){$('#reportMessage').className='form-message error';$('#reportMessage').textContent=error.message}});
    let auditTimer;
    async function loadAudit(){try{const result=await api('/api/audit?q='+encodeURIComponent($('#auditSearch').value));$('#auditMessage').textContent='';const labels={item_added:'Item added',item_updated:'Item updated',item_deleted:'Item deleted',stock_in:'Stock-in',stocktake:'Stocktake',sale:'Sale',loan:'Loan',return:'Return',loan_due_date:'Due date updated',staff_created:'Staff added',staff_removed:'Staff removed',settings_changed:'Settings changed',password_changed:'Password changed',staff_password_reset:'Staff password reset',password_reset:'Password reset'};$('#auditRows').innerHTML=result.audit.length?result.audit.map(row=>`<tr><td>${esc(new Date(row.created_at).toLocaleString())}</td><td>${esc(row.actor)}<div class="resource-id">${esc(row.role)}</div></td><td>${esc(labels[row.action]||row.action)}</td><td>${esc(row.target||'—')}</td><td style="white-space:normal;min-width:240px">${esc(row.detail)}</td></tr>`).join(''):'<tr><td colspan="5" class="empty">No audit entries match.</td></tr>'}catch(error){$('#auditMessage').className='form-message error';$('#auditMessage').textContent=error.message}}
    $('#auditSearch').addEventListener('input',()=>{clearTimeout(auditTimer);auditTimer=setTimeout(loadAudit,180)});
    $('#exportAudit').addEventListener('click',async()=>{try{const response=await fetch('/api/audit/export.csv?q='+encodeURIComponent($('#auditSearch').value));if(!response.ok){const data=await response.json();throw new Error(data.error||'Could not export the audit log.')}const file=URL.createObjectURL(await response.blob()),link=document.createElement('a');link.href=file;link.download='resourcehub-audit-log.csv';link.click();setTimeout(()=>URL.revokeObjectURL(file),1000)}catch(error){$('#auditMessage').className='form-message error';$('#auditMessage').textContent=error.message}});
    $('#backupButton').addEventListener('click',async()=>{try{const response=await fetch('/api/backup');if(!response.ok){const data=await response.json();throw new Error(data.error||'Could not download the backup.')}const file=URL.createObjectURL(await response.blob()),link=document.createElement('a');link.href=file;link.download='resourcehub-backup.zip';link.click();setTimeout(()=>URL.revokeObjectURL(file),1000)}catch(error){$('#reportMessage').className='form-message error';$('#reportMessage').textContent=error.message}});
    $('#search').addEventListener('input',render);$('#categoryFilter').addEventListener('change',render);$('#stockStatusFilter').addEventListener('change',render);$('#loanSearch').addEventListener('input',render);$('#loanFilter').addEventListener('change',render);document.querySelectorAll('.tab[data-mode]').forEach(tab=>tab.addEventListener('click',()=>{mode=tab.dataset.mode;$('#selectedLoanId').value='';$('#quantity').removeAttribute('max');$('#quantity').value='1';document.querySelectorAll('.tab[data-mode]').forEach(t=>t.classList.toggle('active',t===tab));$('#transactionMessage').textContent='';render()}));$('#fellow').addEventListener('change',()=>{$('#selectedLoanId').value='';$('#quantity').removeAttribute('max');$('#quantity').value='1';render()});$('#resource').addEventListener('change',()=>{$('#selectedLoanId').value='';$('#quantity').removeAttribute('max');$('#quantity').value='1'});$('#loanList').addEventListener('click',event=>{const dateButton=event.target.closest('.loan-date-update');if(dateButton){const today=localDateValue(),currentDue=dateButton.dataset.dueDate;$('#loanDueId').value=dateButton.dataset.loanId;$('#loanDueDate').min=today;$('#loanDueDate').value=currentDue&&currentDue>=today?currentDue:localDateValue(state.loan_days||14);$('#loanDueDateDetail').textContent=`${dateButton.dataset.fellow} · ${dateButton.dataset.resourceName} · Current due date: ${currentDue||'not set'}`;$('#loanDueDateMessage').className='form-message';$('#loanDueDateMessage').textContent='';$('#loanDueDateDialog').showModal();$('#loanDueDate').focus();return}const button=event.target.closest('.loan-return');if(!button)return;const fellowId=button.dataset.fellow,resourceId=button.dataset.resource;mode='return';document.querySelectorAll('.tab[data-mode]').forEach(tab=>tab.classList.toggle('active',tab.dataset.mode===mode));$('#transactionMessage').textContent='';render();$('#fellow').value=fellowId;render();$('#resource').value=resourceId;$('#selectedLoanId').value=button.dataset.loanId;$('#quantity').max=button.dataset.quantity;$('#quantity').value=button.dataset.quantity;$('#transactionForm').scrollIntoView({behavior:'smooth',block:'center'});$('#quantity').focus({preventScroll:true})});$('#cancelLoanDueDate').addEventListener('click',()=>$('#loanDueDateDialog').close());$('#loanDueDateForm').addEventListener('submit',async event=>{event.preventDefault();const message=$('#loanDueDateMessage');message.className='form-message';message.textContent='';try{const result=await api('/api/loan-due-date',{loan_id:Number($('#loanDueId').value),due_date:$('#loanDueDate').value});$('#loanDueDateDialog').close();await refresh();showToast(result.message)}catch(error){message.classList.add('error');message.textContent=error.message}});
    $('#resourceRows').addEventListener('click',async event=>{const button=event.target.closest('.delete-resource');if(!button)return;const item=state.resources.find(r=>r.id===button.dataset.id);if(!item)return;try{const accepted=await confirmAction('Delete product?',`Delete ${item.name} (${item.id})? Products with loan, sales, or stocktake history cannot be deleted.`,'Delete product');if(!accepted)return;const result=await api('/api/delete-resource',{resource_id:item.id});await refresh();showToast(result.message)}catch(error){showToast(error.message)}});
    $('#resourceRows').addEventListener('click',event=>{const button=event.target.closest('.edit-details');if(!button)return;const item=state.resources.find(r=>r.id===button.dataset.id);if(!item)return;$('#detailsId').value=item.id;$('#detailsItemName').textContent=`${item.name} · ${item.id}`;$('#detailsSku').value=item.sku||'';$('#detailsPrice').value=Number(item.price).toFixed(2);$('#detailsSupplier').value=item.supplier||'';$('#detailsThreshold').value=item.reorder_level;$('#detailsMessage').textContent='';$('#detailsDialog').showModal()});$('#cancelDetails').addEventListener('click',()=>$('#detailsDialog').close());$('#detailsForm').addEventListener('submit',async event=>{event.preventDefault();const message=$('#detailsMessage');message.className='form-message';message.textContent='';try{const result=await api('/api/resource-details',{resource_id:$('#detailsId').value,sku:$('#detailsSku').value,price:Number($('#detailsPrice').value),supplier:$('#detailsSupplier').value,reorder_level:Number($('#detailsThreshold').value)});$('#detailsDialog').close();await refresh();showToast(result.message)}catch(error){message.classList.add('error');message.textContent=error.message}});
    $('#transactionForm').addEventListener('submit',async event=>{event.preventDefault();const message=$('#transactionMessage');message.className='form-message';message.textContent='';try{const payload={resource_id:$('#resource').value,quantity:Number($('#quantity').value)};if(mode==='sale'){payload.customer=$('#customer').value;const item=state.resources.find(r=>r.id===payload.resource_id);if(!item)return;const amount=(Number(item.price)||0)*payload.quantity;const accepted=await confirmAction('Confirm sale?',`Sell ${payload.quantity} × ${item.name}? Remaining stock will decrease from ${item.available} to ${item.available-payload.quantity}. Sale value: ${money(amount)}.`,'Record sale');if(!accepted)return}else{payload.fellow_id=$('#fellow').value;if(mode==='borrow')payload.due_date=$('#dueDate').value;if(mode==='return'&&$('#selectedLoanId').value)payload.loan_id=Number($('#selectedLoanId').value)}const result=await api('/api/'+mode,payload);message.textContent=result.message;if(mode==='return'){$('#selectedLoanId').value='';$('#quantity').removeAttribute('max')}await refresh();if(mode==='sale')$('#customer').value='';showToast(result.message)}catch(error){message.classList.add('error');message.textContent=error.message}});
    $('#stocktakeButton').addEventListener('click',async()=>{try{await refresh();if(!state.resources.length){showToast('Add an item before starting a stocktake.');return}$('#stocktakeRows').innerHTML=state.resources.map(item=>`<tr><td><div class="resource-name">${esc(item.name)}</div><div class="resource-id">${esc(item.id)}</div></td><td>${item.available}</td><td><input class="stocktake-count" type="number" min="0" step="1" inputmode="numeric" data-resource-id="${esc(item.id)}" data-expected="${item.available}" aria-label="Counted on-shelf units for ${esc(item.name)}" style="width:120px"></td></tr>`).join('');$('#stocktakeNote').value='';$('#stocktakeMessage').className='form-message';$('#stocktakeMessage').textContent='';$('#stocktakeDialog').showModal()}catch(error){showToast(error.message)}});$('#cancelStocktake').addEventListener('click',()=>$('#stocktakeDialog').close());$('#stocktakeForm').addEventListener('submit',async event=>{event.preventDefault();const message=$('#stocktakeMessage');message.className='form-message';message.textContent='';const counts=[...document.querySelectorAll('.stocktake-count')].filter(input=>input.value!=='').map(input=>({resource_id:input.dataset.resourceId,expected:Number(input.dataset.expected),counted:Number(input.value)}));if(!counts.length){message.classList.add('error');message.textContent='Enter a count for at least one item.';return}try{const differences=counts.filter(item=>item.expected!==item.counted),preview=differences.slice(0,4).map(change=>{const item=state.resources.find(resource=>resource.id===change.resource_id);return `${item?.name||change.resource_id}: ${change.expected} → ${change.counted}`}).join(', '),detail=`Count ${counts.length} item(s). ${differences.length?`Adjust ${differences.length}: ${preview}${differences.length>4?', …':''}.`:'No differences found.'} Note: ${$('#stocktakeNote').value.trim()}`;const accepted=await confirmAction('Review stocktake?',detail,'Save stocktake');if(!accepted)return;const result=await api('/api/stocktake',{counts,note:$('#stocktakeNote').value});$('#stocktakeDialog').close();event.target.reset();await refresh();showToast(result.message)}catch(error){message.classList.add('error');message.textContent=error.message}});$('#restockButton').addEventListener('click',()=>{if(!state.resources.length){showToast('Add an item before stocking in.');return}$('#restockItem').innerHTML=state.resources.map(r=>`<option value="${esc(r.id)}">${esc(r.name)} (${esc(r.id)} · ${r.available} remaining)</option>`).join('');$('#restockMessage').textContent='';$('#restockDialog').showModal()});$('#cancelRestock').addEventListener('click',()=>$('#restockDialog').close());$('#restockForm').addEventListener('submit',async event=>{event.preventDefault();const message=$('#restockMessage');message.className='form-message';message.textContent='';try{const id=$('#restockItem').value,quantity=Number($('#restockQuantity').value),item=state.resources.find(r=>r.id===id);if(!item)return;const accepted=await confirmAction('Confirm stock-in?',`Add ${quantity} unit${quantity===1?'':'s'} of ${item.name}. Remaining stock will increase from ${item.available} to ${item.available+quantity}.`,'Update stock');if(!accepted)return;const result=await api('/api/restock',{resource_id:id,quantity});$('#restockDialog').close();event.target.reset();$('#restockQuantity').value='1';await refresh();showToast(result.message)}catch(error){message.classList.add('error');message.textContent=error.message}});$('#resourceRows').addEventListener('click',event=>{if(event.target.closest('[data-empty-add]'))$('#addButton').click()});$('#addButton').addEventListener('click',()=>{$('#addMessage').textContent='';$('#addDialog').showModal()});$('#cancelAdd').addEventListener('click',()=>$('#addDialog').close());$('#addForm').addEventListener('submit',async event=>{event.preventDefault();const message=$('#addMessage');message.className='form-message';message.textContent='';try{const result=await api('/api/resources',{id:$('#newId').value,name:$('#newName').value,category:$('#newCategory').value,total:Number($('#newTotal').value),sku:$('#newSku').value,price:Number($('#newPrice').value),supplier:$('#newSupplier').value,reorder_level:Number($('#newThreshold').value)});$('#addDialog').close();event.target.reset();$('#newTotal').value='1';$('#newPrice').value='0';$('#newThreshold').value='3';await refresh();showToast(result.message)}catch(error){message.classList.add('error');message.textContent=error.message}});

    async function loadAssetPassports(){
      const message=$('#assetMessage');
      try{
        const query=new URLSearchParams({q:$('#assetSearch').value});
        const result=await api('/api/assets?'+query.toString());
        $('#assetRows').innerHTML=result.assets.length?result.assets.map(asset=>`<tr><td><strong>${esc(asset.asset_tag)}</strong></td><td><div class="resource-name">${esc(asset.resource_name)}</div><div class="resource-id">${esc(asset.resource_id)}</div></td><td>${esc(asset.serial_number||'—')}</td><td>${esc(asset.location||'—')}</td><td>${esc(asset.condition)}</td><td><span class="badge">${esc(asset.status)}</span></td><td><button class="btn secondary asset-history" data-tag="${esc(asset.asset_tag)}" type="button">View history</button>${currentRole==='owner'?'<button class="btn secondary asset-label" data-tag="'+esc(asset.asset_tag)+'" type="button">Print label</button>':''}</td></tr>`).join(''):'<tr><td colspan="7" class="empty">No asset passports yet. Register individually tracked equipment to begin its history.</td></tr>';
        $('#createAssetButton').hidden=currentRole!=='owner';
        message.textContent='';
      }catch(error){message.className='form-message error';message.textContent=error.message}
    }
    $('#assetSearch').addEventListener('input',()=>loadAssetPassports());
    $('#createAssetButton').addEventListener('click',()=>{
      $('#assetResource').innerHTML=state.resources.map(item=>`<option value="${esc(item.id)}">${esc(item.name)} (${esc(item.id)})</option>`).join('');
      $('#assetCreateMessage').className='form-message';$('#assetCreateMessage').textContent='';
      $('#assetCreateDialog').showModal();
    });
    $('#cancelAssetCreate').addEventListener('click',()=>$('#assetCreateDialog').close());
    $('#assetCreateForm').addEventListener('submit',async event=>{
      event.preventDefault();const message=$('#assetCreateMessage');message.className='form-message';message.textContent='';
      try{const result=await api('/api/assets',{resource_id:$('#assetResource').value,serial_number:$('#assetSerial').value,location:$('#assetLocation').value,condition:$('#assetCondition').value,notes:$('#assetNotes').value});$('#assetCreateDialog').close();event.target.reset();await loadAssetPassports();showToast(result.message)}
      catch(error){message.classList.add('error');message.textContent=error.message}
    });
    $('#assetRows').addEventListener('click', event => {
      const button = event.target.closest('.asset-label');
      if (!button) return;
      const url = '/api/asset-label?tag=' + encodeURIComponent(button.dataset.tag);
      window.open(url, '_blank', 'noopener');
    });
    async function loadMaintenanceRecords(tag){
      const rows=$('#maintenanceRows'),message=$('#maintenanceMessage'),owner=currentRole==='owner';
      try{
        const result=await api('/api/maintenance?tag='+encodeURIComponent(tag));
        const records=result.records||[];
        rows.innerHTML=records.length?records.map(record=>{
          const reported=record.reported_at?new Date(record.reported_at+'Z').toLocaleString():'—';
          const cost=money(record.cost||0);
          const statuses=[['open','Open'],['in_progress','In progress'],['completed','Completed'],['cancelled','Cancelled']];
          const statusLabel=(statuses.find(item=>item[0]===record.status)||[record.status,record.status])[1];
          return `<tr>
            <td>${esc(reported)}<div class="subtle">#${Number(record.id)}</div></td>
            <td><strong>${esc(record.problem)}</strong><div class="field" style="margin-top:6px"><label for="maintenanceAction-${Number(record.id)}">Action taken</label><textarea id="maintenanceAction-${Number(record.id)}" data-maintenance-action maxlength="1000" rows="2" ${owner?'':'disabled'}>${esc(record.action_taken||'')}</textarea></div></td>
            <td><input aria-label="Repair cost" data-maintenance-cost type="number" min="0" step="0.01" value="${esc(record.cost??0)}" ${owner?'':'disabled'} style="min-width:90px"></td>
            <td><input aria-label="Technician" data-maintenance-technician maxlength="120" value="${esc(record.technician||'')}" ${owner?'':'disabled'} style="min-width:110px"></td>
            <td>${owner?`<select aria-label="Maintenance status" data-maintenance-status>${statuses.map(item=>`<option value="${item[0]}" ${record.status===item[0]?'selected':''}>${item[1]}</option>`).join('')}</select>`:esc(statusLabel)}${record.completed_at?`<div class="subtle">Completed: ${esc(new Date(record.completed_at+'Z').toLocaleDateString())}</div>`:''}</td>
            <td>${owner?`<button class="btn secondary save-maintenance" type="button" data-id="${Number(record.id)}">Save</button>`:'View only'}</td>
          </tr>`;
        }).join(''):'<tr><td colspan="6" class="empty">No maintenance records for this asset yet.</td></tr>';
        $('#maintenanceCreatePanel').hidden=!owner;
        message.className='form-message';message.textContent='';
      }catch(error){
        rows.innerHTML='<tr><td colspan="6" class="empty">Could not load maintenance records.</td></tr>';
        message.className='form-message error';message.textContent=error.message;
      }
    }

    $('#createMaintenanceButton').addEventListener('click',async()=>{
      if(currentRole!=='owner')return;
      const message=$('#maintenanceMessage');message.className='form-message';message.textContent='';
      const problem=$('#maintenanceProblem').value.trim();
      if(!problem){message.classList.add('error');message.textContent='Enter the reported problem.';$('#maintenanceProblem').focus();return}
      try{
        const result=await api('/api/maintenance',{
          asset_tag:$('#assetDetailTag').value,
          problem,
          action_taken:$('#maintenanceAction').value.trim(),
          cost:$('#maintenanceCost').value,
          technician:$('#maintenanceTechnician').value.trim(),
          status:$('#maintenanceStatus').value
        });
        message.textContent=result.message||'Maintenance record created.';
        $('#maintenanceProblem').value='';
        $('#maintenanceAction').value='';
        $('#maintenanceCost').value='0';
        $('#maintenanceTechnician').value='';
        $('#maintenanceStatus').value='open';
        await loadMaintenanceRecords($('#assetDetailTag').value);
        showToast('Maintenance record created.');
      }catch(error){message.className='form-message error';message.textContent=error.message}
    });

    $('#maintenanceRows').addEventListener('click',async event=>{
      const button=event.target.closest('.save-maintenance');
      if(!button||currentRole!=='owner')return;
      const row=button.closest('tr'),id=Number(button.dataset.id),message=$('#maintenanceMessage');
      message.className='form-message';message.textContent='';
      try{
        const result=await api('/api/maintenance/update',{
          id,
          action_taken:row.querySelector('[data-maintenance-action]').value.trim(),
          cost:row.querySelector('[data-maintenance-cost]').value,
          technician:row.querySelector('[data-maintenance-technician]').value.trim(),
          status:row.querySelector('[data-maintenance-status]').value
        });
        message.textContent=result.message||'Maintenance record updated.';
        await loadMaintenanceRecords($('#assetDetailTag').value);
        showToast('Maintenance record updated.');
      }catch(error){message.className='form-message error';message.textContent=error.message}
    });

    $('#assetRows').addEventListener('click',async event=>{
      const button=event.target.closest('.asset-history');if(!button)return;
      try{
        const result=await api('/api/asset?tag='+encodeURIComponent(button.dataset.tag)),asset=result.asset;
        $('#assetDetailTag').value=asset.asset_tag;$('#assetDetailTitle').textContent=asset.asset_tag;
        $('#assetDetailSubtitle').textContent=`${asset.resource_name} (${asset.resource_id}) · Serial: ${asset.serial_number||'not recorded'}`;
        $('#assetDetailLocation').value=asset.location;$('#assetDetailCondition').value=asset.condition;$('#assetDetailStatus').value=asset.status;$('#assetDetailNotes').value=asset.notes;
        $('#assetEventRows').innerHTML=asset.events.length?asset.events.map(row=>`<tr><td>${esc(new Date(row.created_at+'Z').toLocaleString())}</td><td>${esc(row.event_type)}</td><td>${esc(row.detail)}</td><td>${esc(row.actor||'—')}</td></tr>`).join(''):'<tr><td colspan="4" class="empty">No history recorded.</td></tr>';
        await loadMaintenanceRecords(asset.asset_tag);
        $('#assetDetailMessage').className='form-message';$('#assetDetailMessage').textContent='';
        const owner=currentRole==='owner';['assetDetailLocation','assetDetailCondition','assetDetailStatus','assetDetailNotes','saveAssetDetail'].forEach(id=>$('#'+id).disabled=!owner);
        $('#assetDetailDialog').showModal();
      }catch(error){showToast(error.message)}
    });
    $('#cancelAssetDetail').addEventListener('click',()=>$('#assetDetailDialog').close());
    $('#assetDetailForm').addEventListener('submit',async event=>{
      event.preventDefault();const message=$('#assetDetailMessage');message.className='form-message';message.textContent='';
      try{const result=await api('/api/assets/update',{asset_tag:$('#assetDetailTag').value,location:$('#assetDetailLocation').value,condition:$('#assetDetailCondition').value,status:$('#assetDetailStatus').value,notes:$('#assetDetailNotes').value});showToast(result.message);$('#assetDetailDialog').close();await loadAssetPassports()}
      catch(error){message.classList.add('error');message.textContent=error.message}
    });

    let authMode='login';
    function renderTurnstileWidget(){const target=$('#turnstileContainer');if(target&&turnstileSiteKey&&window.turnstile&&!target.dataset.widget){window.turnstile.render(target,{sitekey:turnstileSiteKey});target.dataset.widget='1'}}
    function setAuthMode(next){if(next==='register'&&!registrationOpen)next='login';authMode=next;const registering=next==='register';if(registering&&!$('#registrationFields'))$('#nameField').insertAdjacentHTML('afterend','<div id="registrationFields"><div class="field"><label for="companyName">Company name</label><input id="companyName" maxlength="100" required></div><div class="field"><label for="companyProducts">What do you sell?</label><input id="companyProducts" maxlength="120" placeholder="Products or services"></div><div class="field"><label for="companyPhone">Company phone</label><input id="companyPhone" autocomplete="tel" maxlength="24" required></div><div class="field"><label for="companyEmail">Company email</label><input id="companyEmail" type="email" autocomplete="email" maxlength="254" required></div><div class="field email-code-field"><label for="emailCode">Email code</label><div class="email-code-controls"><input id="emailCode" inputmode="numeric" maxlength="6" placeholder="Email code" required><button class="btn secondary" id="sendEmailCode" type="button">Send code</button></div></div><div id="turnstileContainer"></div></div>');if($('#registrationFields')){$('#registrationFields').hidden=!registering;$('#registrationFields').querySelectorAll('input').forEach(input=>input.required=registering)}renderTurnstileWidget();$('#authTitle').textContent=registering?'Register your company':'Welcome back';$('#authSubtitle').textContent=registering?'Create a shop account. Verify your email and complete the bot check.':'Sign in to manage your shop and staff.';if(registering&&!turnstileSiteKey)$('#authSubtitle').textContent='Signup setup is incomplete: the site owner must configure Cloudflare Turnstile.';$('#nameField').hidden=!registering;$('#authName').required=registering;$('#authPassword').autocomplete=registering?'new-password':'current-password';$('#authSubmit').textContent=registering?'Create shop account':'Sign in';$('#registerTab').hidden=!registrationOpen;document.querySelectorAll('[data-auth-mode]').forEach(tab=>tab.classList.toggle('active',tab.dataset.authMode===next));$('#authError').textContent=''}
    function showApp(name,role){currentRole=role||'staff';$('#accountName').textContent=`${name} · ${currentRole}`;$('#staffButton').hidden=currentRole!=='owner';$('#shopSettingsButton').hidden=currentRole!=='owner';$('#stocktakeButton').hidden=currentRole!=='owner';$('#restockButton').hidden=currentRole!=='owner';$('#addButton').hidden=currentRole!=='owner';$('#exportReport').hidden=currentRole!=='owner';$('#backupButton').hidden=currentRole!=='owner';$('#reportsPanel').hidden=currentRole!=='owner';$('#auditPanel').hidden=currentRole!=='owner';$('#reportsNavLink').hidden=currentRole!=='owner';$('#auditNavLink').hidden=currentRole!=='owner';$('#landingScreen').hidden=true;$('#authScreen').hidden=true;$('#appScreen').hidden=false;refresh().catch(error=>showToast(error.message))}
    const baseSetAuthMode=setAuthMode;
    setAuthMode=function(next){baseSetAuthMode(next);$('#resetForm').hidden=true;$('#authForm').hidden=false;document.querySelector('.auth-tabs').hidden=false;$('#forgotLink').hidden=authMode!=='login'};
    $('#forgotLink').addEventListener('click',()=>{$('#authForm').hidden=true;document.querySelector('.auth-tabs').hidden=true;$('#forgotLink').hidden=true;$('#resetForm').hidden=false;$('#resetUsername').value=$('#authUsername').value;$('#authTitle').textContent='Reset your password';$('#authSubtitle').textContent='We email a code to the address on your owner account.';$('#resetMessage').textContent='';$('#authError').textContent=''});
    $('#resetBack').addEventListener('click',()=>setAuthMode('login'));
    $('#resetSend').addEventListener('click',async()=>{const box=$('#resetMessage');box.style.color='var(--red)';box.textContent='';try{const result=await api('/api/password-reset/request',{username:$('#resetUsername').value});box.textContent=result.message;box.style.color='var(--green)'}catch(error){box.textContent=error.message}});
    $('#resetForm').addEventListener('submit',async event=>{event.preventDefault();const box=$('#resetMessage');box.style.color='var(--red)';box.textContent='';try{const result=await api('/api/password-reset/confirm',{username:$('#resetUsername').value,code:$('#resetCode').value,new_password:$('#resetPassword').value});event.target.reset();setAuthMode('login');$('#authError').style.color='var(--green)';$('#authError').textContent=result.message}catch(error){box.textContent=error.message}});
    $('#passwordButton').addEventListener('click',()=>{$('#passwordForm').reset();$('#passwordMessage').className='form-message';$('#passwordMessage').textContent='';$('#passwordDialog').showModal()});
    $('#cancelPassword').addEventListener('click',()=>$('#passwordDialog').close());
    $('#passwordForm').addEventListener('submit',async event=>{event.preventDefault();const box=$('#passwordMessage');box.className='form-message';box.textContent='';try{const result=await api('/api/change-password',{current_password:$('#currentPassword').value,new_password:$('#newPassword').value});$('#passwordDialog').close();event.target.reset();showToast(result.message)}catch(error){box.classList.add('error');box.textContent=error.message}});
    $('#staffRows').addEventListener('click',async event=>{const button=event.target.closest('.reset-staff');if(!button)return;if(!window.confirm(`Reset the password for ${button.dataset.username}? A temporary password will be shown once.`))return;const message=$('#staffMessage');try{const result=await api('/api/staff/reset-password',{username:button.dataset.username});message.className='form-message';message.textContent=result.message}catch(error){message.className='form-message error';message.textContent=error.message}});
    document.querySelectorAll('[data-open-auth]').forEach(button=>button.addEventListener('click',()=>{$('#landingScreen').hidden=true;$('#authScreen').hidden=false;setAuthMode('login')}));
    $('#backLanding').addEventListener('click',()=>{$('#authScreen').hidden=true;$('#landingScreen').hidden=false});
    document.querySelectorAll('[data-auth-mode]').forEach(tab=>tab.addEventListener('click',()=>setAuthMode(tab.dataset.authMode)));
    $('#authForm').addEventListener('submit',async event=>{event.preventDefault();const errorBox=$('#authError');errorBox.textContent='';const payload={username:$('#authUsername').value,password:$('#authPassword').value};if(authMode==='register'){payload.name=$('#authName').value;payload.company_name=$('#companyName').value;payload.products=$('#companyProducts').value;payload.phone=$('#companyPhone').value;payload.email=$('#companyEmail').value;payload.email_code=$('#emailCode').value;payload.turnstile_token=document.querySelector('[name="cf-turnstile-response"]')?.value||''}try{const result=await api('/api/'+(authMode==='register'?'register':'login'),payload);event.target.reset();showApp(result.name,result.role);registrationOpen=true}catch(error){errorBox.textContent=error.message}});
    $('#authForm').addEventListener('click',async event=>{if(event.target.id!=='sendEmailCode')return;const errorBox=$('#authError');errorBox.textContent='';try{const result=await api('/api/send-verification',{email:$('#companyEmail').value,turnstile_token:document.querySelector('[name="cf-turnstile-response"]')?.value||''});errorBox.textContent=result.message;errorBox.style.color='var(--green)'}catch(error){errorBox.textContent=error.message;errorBox.style.color='var(--red)'}finally{if(window.turnstile)window.turnstile.reset()}});
    $('#shopSettingsButton').addEventListener('click',()=>{const settings=state.settings;$('#shopName').value=settings.shop_name;$('#shopColor').value=settings.brand_color;$('#shopCurrency').value=settings.currency;$('#shopLogo').value='';pendingLogo=null;$('#shopLogoPreview').src=settings.logo_data||'';$('#shopLogoPreview').hidden=!settings.logo_data;$('#shopSettingsMessage').textContent='';$('#shopSettingsDialog').showModal()});$('#cancelShopSettings').addEventListener('click',()=>$('#shopSettingsDialog').close());$('#shopLogo').addEventListener('change',()=>{const file=$('#shopLogo').files[0];if(!file)return;if(file.size>250000){$('#shopSettingsMessage').className='form-message error';$('#shopSettingsMessage').textContent='Choose a logo no larger than 250 KB.';$('#shopLogo').value='';return}const reader=new FileReader();reader.onload=()=>{pendingLogo=reader.result;$('#shopLogoPreview').src=pendingLogo;$('#shopLogoPreview').hidden=false;$('#shopSettingsMessage').textContent=''};reader.onerror=()=>{$('#shopSettingsMessage').className='form-message error';$('#shopSettingsMessage').textContent='Could not read this image.'};reader.readAsDataURL(file)});$('#removeShopLogo').addEventListener('click',()=>{pendingLogo='';$('#shopLogo').value='';$('#shopLogoPreview').removeAttribute('src');$('#shopLogoPreview').hidden=true});$('#shopSettingsForm').addEventListener('submit',async event=>{event.preventDefault();const message=$('#shopSettingsMessage');message.className='form-message';message.textContent='';try{const result=await api('/api/settings',{shop_name:$('#shopName').value,brand_color:$('#shopColor').value,currency:$('#shopCurrency').value,logo_data:pendingLogo===null?state.settings.logo_data:pendingLogo});pendingLogo=null;$('#shopSettingsDialog').close();await refresh();showToast(result.message)}catch(error){message.classList.add('error');message.textContent=error.message}});$('#staffButton').addEventListener('click',async()=>{try{await loadStaff();$('#staffMessage').textContent='';$('#staffDialog').showModal()}catch(error){showToast(error.message)}});$('#closeStaff').addEventListener('click',()=>$('#staffDialog').close());$('#staffRows').addEventListener('click',async event=>{const button=event.target.closest('.remove-staff');if(!button)return;if(!window.confirm(`Remove staff account ${button.dataset.username}?`))return;try{const result=await api('/api/staff/delete',{username:button.dataset.username});await loadStaff();$('#staffMessage').textContent=result.message}catch(error){$('#staffMessage').className='form-message error';$('#staffMessage').textContent=error.message}});async function loadStaff(){const result=await api('/api/staff');$('#staffRows').innerHTML=result.staff.length?result.staff.map(person=>`<tr><td>${esc(person.name)}</td><td>${esc(person.username)}</td><td>Staff</td><td><button class="btn secondary reset-staff" data-username="${esc(person.username)}" type="button">Reset password</button> <button class="btn secondary remove-staff" data-username="${esc(person.username)}" type="button">Remove</button></td></tr>`).join(''):'<tr><td colspan="4" class="empty">No staff accounts yet.</td></tr>'}$('#staffForm').addEventListener('submit',async event=>{event.preventDefault();const message=$('#staffMessage');message.className='form-message';message.textContent='';try{const result=await api('/api/staff',{name:$('#staffName').value,username:$('#staffUsername').value,password:$('#staffPassword').value});event.target.reset();await loadStaff();message.textContent=result.message}catch(error){message.classList.add('error');message.textContent=error.message}});$('#logoutButton').addEventListener('click',async()=>{try{await api('/api/logout',{});$('#appScreen').hidden=true;$('#authScreen').hidden=true;$('#landingScreen').hidden=false;$('#authPassword').value='';$('#staffButton').hidden=true;$('#shopSettingsButton').hidden=true;$('#registerTab').hidden=!registrationOpen;setAuthMode('login')}catch(error){showToast(error.message)}});
    api('/api/me').then(me=>{registrationOpen=me.registration_open;turnstileSiteKey=me.turnstile_site_key||'';applyShopSettings(me.settings);$('#registerTab').hidden=!registrationOpen;if(turnstileSiteKey){const script=document.createElement('script');script.src='https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit';script.async=true;script.defer=true;document.head.appendChild(script);script.onload=renderTurnstileWidget}if(me.authenticated)showApp(me.name,me.role);else setAuthMode('login')}).catch(error=>showToast(error.message));setInterval(()=>{if(!$('#appScreen').hidden)refresh().catch(()=>{})},30000);
  
document.querySelector('#testEmailButton').addEventListener('click', async () => {
  const button = document.querySelector('#testEmailButton');
  const message = document.querySelector('#testEmailMessage');
  const email = document.querySelector('#testEmailAddress').value.trim();

  message.className = 'form-message';
  message.textContent = '';
  button.disabled = true;

  try {
    const result = await api('/api/settings/test-email', { email });
    message.textContent = result.message || 'Test email sent.';
  } catch (error) {
    message.classList.add('error');
    message.textContent = error.message || 'Email test failed. Check server settings.';
  } finally {
    button.disabled = false;
  }
});
</script>
</div></div></body></html>'''


def save_users():
    """Atomically write user password hashes to the local auth file."""
    path = os.path.abspath(AUTH_FILE)
    temp_path = None
    try:
        if os.path.isfile(path):
            # The account registry is shared, so keep its backups outside any tenant folder.
            backup_dir = ROOT_BACKUP_DIR
            os.makedirs(backup_dir, exist_ok=True)
            auth_backup = os.path.join(backup_dir, f"auth-{date.today().isoformat()}.json")
            temporary_auth = None
            try:
                with tempfile.NamedTemporaryFile("wb", dir=backup_dir, prefix=".auth-", delete=False) as backup:
                    temporary_auth = backup.name
                shutil.copy2(path, temporary_auth)
                os.replace(temporary_auth, auth_backup)
                temporary_auth = None
            finally:
                if temporary_auth and os.path.exists(temporary_auth):
                    os.remove(temporary_auth)
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=os.path.dirname(path),
                                         delete=False) as f:
            temp_path = f.name
            json.dump({"users": users, "shops": shops}, f, indent=2)
            f.write("\n")
        os.replace(temp_path, path)
    finally:
        if temp_path and os.path.exists(temp_path):
            os.remove(temp_path)


def load_users():
    """Load account hashes; refuse to overwrite an unreadable account file."""
    try:
        with open(AUTH_FILE, "r", encoding="utf-8") as f:
            saved = json.load(f)
    except FileNotFoundError:
        return
    if not isinstance(saved, dict) or not isinstance(saved.get("users"), dict):
        raise RuntimeError("auth.json has an invalid format; account data was not loaded.")
    for username, user in saved["users"].items():
        if (not isinstance(username, str) or not isinstance(user, dict)
                or not isinstance(user.get("name"), str)
                or not isinstance(user.get("salt"), str)
                or not isinstance(user.get("password_hash"), str)):
            raise RuntimeError("auth.json has an invalid account record.")
        try:
            salt, digest = bytes.fromhex(user["salt"]), bytes.fromhex(user["password_hash"])
        except ValueError as error:
            raise RuntimeError("auth.json contains an invalid password hash.") from error
        if len(salt) != 16 or len(digest) != 32:
            raise RuntimeError("auth.json contains an invalid password hash.")
        user.setdefault("role", "owner")
        if user["role"] not in ("owner", "staff"):
            raise RuntimeError("auth.json contains an invalid user role.")
    saved_shops = saved.get("shops", {})
    if not isinstance(saved_shops, dict):
        raise RuntimeError("auth.json contains an invalid shops record.")
    shops.update(saved_shops)
    if not shops:
        # Keep the existing single-shop database and its accounts as the first tenant.
        legacy_shop = inventory.get_shop_settings().get("shop_name", "ResourceHub")
        shops["default"] = {"id": "default", "name": legacy_shop,
                            "phone": "", "email": "", "slug": "default"}
    for username, user in saved["users"].items():
        user.setdefault("shop_id", "default")
        if (not isinstance(user["shop_id"], str) or user["shop_id"] not in shops
                or not isinstance(shops[user["shop_id"]], dict)
                or shops[user["shop_id"]].get("id") != user["shop_id"]):
            raise RuntimeError("auth.json contains a user assigned to an unknown shop.")
    for shop_id, shop in shops.items():
        if (not isinstance(shop_id, str) or (shop_id != "default" and not re.fullmatch(r"[a-f0-9]{16}", shop_id))
                or not isinstance(shop, dict) or shop.get("id") != shop_id
                or not isinstance(shop.get("name"), str) or not isinstance(shop.get("slug"), str)):
            raise RuntimeError("auth.json contains an invalid shop record.")
    users.update(saved["users"])


def snapshot():
    """Return UI-ready data while callers hold LOCK."""
    inventory.refresh_state()
    resources = deepcopy(inventory.resources)
    loans = []
    for resource in resources:
        for fellow_id in inventory.fellows:
            quantity = inventory.outstanding(fellow_id, resource["id"])
            if quantity > 0:
                loans.append({
                    "fellow_id": fellow_id,
                    "fellow_name": inventory.fellows[fellow_id],
                    "resource_id": resource["id"],
                    "resource_name": resource["name"],
                    "quantity": quantity,
                })
    total = sum(item["total"] for item in resources)
    available = sum(item["available"] for item in resources)
    with inventory.connect() as db:
        recent_sales = [dict(row) for row in db.execute("""SELECT s.quantity,s.customer,s.created_at,
            r.name AS resource_name FROM sales s JOIN resources r ON r.id=s.resource_id
            ORDER BY s.id DESC LIMIT 8""")]
        loan_records = [dict(row) for row in db.execute("""SELECT b.id AS loan_id,b.fellow_id,
            f.name AS fellow_name,b.resource_id,r.name AS resource_name,b.due_date,
            b.quantity-COALESCE(SUM(ret.quantity),0) AS quantity
            FROM loans b JOIN fellows f ON f.id=b.fellow_id JOIN resources r ON r.id=b.resource_id
            LEFT JOIN loans ret ON ret.loan_id=b.id AND ret.type IN ('return','damaged')
            WHERE b.type='borrow' GROUP BY b.id HAVING b.quantity-COALESCE(SUM(ret.quantity),0)>0
            ORDER BY CASE WHEN b.due_date IS NULL THEN 1 ELSE 0 END,b.due_date,b.id""")]
    return {
        "resources": resources,
        "fellows": [{"id": key, "name": name} for key, name in inventory.fellows.items()],
        "loans": loans,
        "loan_records": loan_records,
        "loan_days": inventory.LOAN_DAYS,
        "sales": recent_sales,
        "settings": inventory.get_shop_settings(),
        "summary": {"total": total, "available": available, "borrowed": total - available},
    }


def transaction_history(search_text="", action="all", date_from="", date_to=""):
    """Return searchable sales, restocks, and lending events, newest first."""
    with inventory.connect() as db:
        rows = [dict(row) for row in db.execute("""SELECT event.action,event.resource_id,event.item,
            event.quantity,event.party,event.created_at FROM (
              SELECT 'sale' AS action,s.resource_id,r.name AS item,s.quantity,s.customer AS party,s.created_at
                FROM sales s JOIN resources r ON r.id=s.resource_id
              UNION ALL
              SELECT m.movement_type,m.resource_id,r.name,m.quantity,'',m.created_at
                FROM stock_movements m JOIN resources r ON r.id=m.resource_id
              UNION ALL
              SELECT 'stocktake_adjustment',a.resource_id,r.name,a.quantity,a.note,a.created_at
                FROM stock_adjustments a JOIN resources r ON r.id=a.resource_id
              UNION ALL
              SELECT l.type,l.resource_id,r.name,l.quantity,f.name,l.created_at
                FROM loans l JOIN resources r ON r.id=l.resource_id JOIN fellows f ON f.id=l.fellow_id
            ) AS event ORDER BY event.created_at DESC""")]
    term = search_text.strip().casefold()
    if action != "all":
        rows = [row for row in rows if row["action"] == action]
    if date_from:
        rows = [row for row in rows if row["created_at"][:10] >= date_from]
    if date_to:
        rows = [row for row in rows if row["created_at"][:10] <= date_to]
    if term:
        rows = [row for row in rows if term in " ".join(str(row[key] or "") for key in
                ("action", "resource_id", "item", "quantity", "party")).casefold()]
    return rows[:500]


def reports_summary(date_from="", date_to=""):
    inventory.refresh_state()
    today = date.today()
    if not date_from and not date_to:
        date_from = today.replace(day=1).isoformat()
        date_to = today.isoformat()
    date_filters = []
    date_values = []
    if date_from:
        date_filters.append("substr(s.created_at,1,10)>=?")
        date_values.append(date_from)
    if date_to:
        date_filters.append("substr(s.created_at,1,10)<=?")
        date_values.append(date_to)
    date_clause = " WHERE " + " AND ".join(date_filters) if date_filters else ""
    with inventory.connect() as db:
        period_sales = db.execute("""SELECT COALESCE(SUM(s.quantity),0) AS units,
            COALESCE(SUM(s.quantity*s.unit_price),0) AS revenue FROM sales s""" + date_clause,
            date_values).fetchone()
        lifetime = db.execute("""SELECT COALESCE(SUM(quantity),0) AS units,
            COALESCE(SUM(quantity*unit_price),0) AS revenue FROM sales""").fetchone()
        best_sellers = [dict(row) for row in db.execute("""SELECT r.id,r.name,
            SUM(s.quantity) AS units,SUM(s.quantity*s.unit_price) AS revenue
            FROM sales s JOIN resources r ON r.id=s.resource_id
            """ + date_clause + """ GROUP BY r.id,r.name ORDER BY units DESC,revenue DESC LIMIT 5""", date_values)]
        inventory_rows = [dict(row) for row in db.execute("SELECT id,name,category,sku,supplier,price FROM resources ORDER BY name COLLATE NOCASE")]
    stock_value = sum(item["available"] * item["price"] for item in inventory.resources)
    period_label = f"{date_from} – {date_to}" if date_from and date_to else f"From {date_from}" if date_from else f"Through {date_to}" if date_to else "All dates"
    return {
        "period": period_label,
        "period_units": period_sales["units"], "period_revenue": period_sales["revenue"],
        "lifetime_units": lifetime["units"], "lifetime_revenue": lifetime["revenue"],
        "stock_value": stock_value, "best_sellers": best_sellers,
        "inventory": [{**item, "available": next(r["available"] for r in inventory.resources if r["id"] == item["id"])} for item in inventory_rows],
    }


def reports_csv(date_from="", date_to=""):
    report = reports_summary(date_from, date_to)
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    safe = lambda value: "'" + value if isinstance(value, str) and value.startswith(("=", "+", "-", "@", "\t", "\r")) else value
    writer.writerow(["section", "item_id", "item", "category", "sku", "quantity", "unit_price", "value", "details"])
    writer.writerow(["sales_summary", "", "", "", "", report["period_units"], "", f"{report['period_revenue']:.2f}", f"Selected period: {report['period']}"])
    writer.writerow(["sales_lifetime", "", "", "", "", report["lifetime_units"], "", f"{report['lifetime_revenue']:.2f}", "All recorded sales"])
    writer.writerow(["stock_summary", "", "", "", "", "", "", f"{report['stock_value']:.2f}", "Current stock value"])
    for item in report["inventory"]:
        writer.writerow(["inventory", safe(item["id"]), safe(item["name"]), safe(item["category"]), safe(item["sku"]),
                         item["available"], f"{item['price']:.2f}", f"{item['available']*item['price']:.2f}", safe(item["supplier"])])
    for item in report["best_sellers"]:
        writer.writerow(["best_seller", safe(item["id"]), safe(item["name"]), "", "", item["units"], "", f"{item['revenue']:.2f}", f"Selected period: {report['period']}"])
    return output.getvalue()


def backup_archive(shop_id):
    database_backup = inventory.backup_database()
    if database_backup is None:
        return None
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.write(database_backup, arcname="campus_resources.db")
        tenant_users = {name: user for name, user in users.items() if user.get("shop_id") == shop_id}
        tenant_auth = {"users": tenant_users, "shops": {shop_id: shops[shop_id]}}
        archive.writestr("shop-accounts.json", json.dumps(tenant_auth, indent=2) + "\n")
        archive.writestr("RESTORE.txt", "Restore campus_resources.db to this shop's database folder. shop-accounts.json contains password hashes for this shop's accounts; keep it private.\n")
    return output.getvalue()


class Handler(BaseHTTPRequestHandler):
    server_version = "ResourceHub/1.0"

    def respond(self, status, payload, content_type="application/json; charset=utf-8", headers=None):
        body = payload if isinstance(payload, bytes) else payload.encode("utf-8") if isinstance(payload, str) else json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Content-Security-Policy", "frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        self.send_header("Cache-Control", "no-store")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def read_json(self, max_bytes=16_384):
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if size < 1 or size > max_bytes:
                raise ValueError("Request body is empty or too large.")
            if "application/json" not in self.headers.get("Content-Type", ""):
                raise ValueError("Send request data as JSON.")
            data = json.loads(self.rfile.read(size), parse_constant=reject_json_constant,
                              parse_float=parse_finite_float)
            if not isinstance(data, dict):
                raise ValueError("Request data must be a JSON object.")
            return data
        except (ValueError, json.JSONDecodeError) as error:
            raise ValueError("Invalid request data.") from error

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            self.respond(200, PAGE, "text/html; charset=utf-8")
        elif path == "/api/state":
            with LOCK:
                if not self.current_user():
                    self.respond(401, {"error": "Please sign in to continue."})
                    return
                self.respond(200, snapshot())
        elif path in ("/api/assets/qr", "/api/asset-label"):
            with LOCK:
                user = self.current_user()
                if not user:
                    self.respond(401, {"error": "Please sign in to continue."})
                    return
                if user.get("role") != "owner":
                    self.respond(403, {"error": "Owner access required."})
                    return

                query = parse_qs(urlsplit(self.path).query)
                tag = query.get("tag", [""])[0].strip()
                if not tag or len(tag) > 32:
                    self.respond(400, {"error": "A valid asset tag is required."})
                    return

                try:
                    asset = inventory.get_asset_passport(tag)
                    if not asset:
                        self.respond(404, {"error": "Asset passport not found."})
                        return
                    qr_png = make_qr_png(tag)
                except ValueError:
                    self.respond(400, {"error": "A valid asset tag is required."})
                    return

                if path == "/api/assets/qr":
                    self.send_response(200)
                    self.send_header("Content-Type", "image/png")
                    self.send_header("Content-Length", str(len(qr_png)))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(qr_png)
                    return

                safe_tag = escape(tag, quote=True)
                safe_name = escape(
                    str(asset.get("resource_name") or asset.get("resource_id") or ""),
                    quote=True,
                )
                page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ResourceHub asset label - {safe_tag}</title>
<style>
body{{font-family:Arial,sans-serif;margin:0;padding:24px;color:#111}}
.label{{width:320px;max-width:100%;border:1px solid #222;padding:16px;text-align:center}}
img{{width:180px;height:180px;display:block;margin:12px auto}}
.tag{{font-size:22px;font-weight:bold;overflow-wrap:anywhere}}
.name{{font-size:14px;margin-top:8px}}
@media print{{@page{{margin:8mm}}body{{padding:0}}.no-print{{display:none}}.label{{break-inside:avoid}}}}
</style></head><body>
<div class="label"><strong>ResourceHub</strong>
<img alt="QR code for asset {safe_tag}" src="/api/assets/qr?tag={safe_tag}">
<div class="tag">{safe_tag}</div><div class="name">{safe_name}</div></div>
<p class="no-print"><button onclick="window.print()">Print label</button></p>
</body></html>""".encode("utf-8")
                self.respond(200, page, "text/html; charset=utf-8")
        elif path == "/api/assets":
            with LOCK:
                username = self.current_user()
                if not username:
                    self.respond(401, {"error": "Please sign in to continue."})
                    return
                query = parse_qs(urlparse(self.path).query)
                try:
                    assets = inventory.list_asset_passports(query.get("q", [""])[0][:120])
                except ValueError as error:
                    self.respond(400, {"error": str(error)})
                    return
                self.respond(200, {"assets": assets})
        elif path == "/api/maintenance":
            with LOCK:
                if not self.current_user():
                    self.respond(401, {"error": "Please sign in to continue."})
                    return
                query = parse_qs(urlparse(self.path).query)
                tag = query.get("tag", [None])[0]
                status = query.get("status", [None])[0]
                try:
                    records = inventory.list_maintenance_records(
                        asset_tag=tag, status=status
                    )
                except ValueError as error:
                    self.respond(400, {"error": str(error)})
                    return
                self.respond(200, {"records": records})
        elif path == "/api/asset":
            with LOCK:
                if not self.current_user():
                    self.respond(401, {"error": "Please sign in to continue."})
                    return
                tag = parse_qs(urlparse(self.path).query).get("tag", [""])[0]
                try:
                    asset = inventory.get_asset_passport(tag)
                except ValueError as error:
                    self.respond(400, {"error": str(error)})
                    return
                if asset is None:
                    self.respond(404, {"error": "Asset passport not found."})
                    return
                self.respond(200, {"asset": asset})
        elif path == "/api/history":
            with LOCK:
                if not self.current_user():
                    self.respond(401, {"error": "Please sign in to continue."})
                    return
                query = parse_qs(urlparse(self.path).query)
                search_text = query.get("q", [""])[0][:120]
                action = query.get("action", ["all"])[0]
                date_from = query.get("from", [""])[0]
                date_to = query.get("to", [""])[0]
                if action not in ("all", "sale", "opening_stock", "restock", "borrow", "return", "damaged", "stocktake_adjustment"):
                    self.respond(400, {"error": "Choose a valid transaction type."})
                    return
                try:
                    date_from = date.fromisoformat(date_from).isoformat() if date_from else ""
                    date_to = date.fromisoformat(date_to).isoformat() if date_to else ""
                except ValueError:
                    self.respond(400, {"error": "Enter valid start and end dates."})
                    return
                if date_from and date_to and date_from > date_to:
                    self.respond(400, {"error": "Start date must be on or before end date."})
                    return
                self.respond(200, {"history": transaction_history(search_text, action, date_from, date_to)})
        elif path == "/api/staff":
            with LOCK:
                username = self.current_user()
                if not username:
                    self.respond(401, {"error": "Please sign in to continue."})
                    return
                if users[username]["role"] != "owner":
                    self.respond(403, {"error": "Only the owner can manage staff accounts."})
                    return
                staff = [{"username": key, "name": value["name"]} for key, value in users.items()
                         if value["role"] == "staff" and value.get("shop_id") == users[username].get("shop_id")]
                self.respond(200, {"staff": staff})
        elif path in ("/api/audit", "/api/audit/export.csv"):
            with LOCK:
                username = self.current_user()
                if not username:
                    self.respond(401, {"error": "Please sign in to continue."})
                    return
                if users[username]["role"] != "owner":
                    self.respond(403, {"error": "Only the owner can view the audit log."})
                    return
                search_text = parse_qs(urlparse(self.path).query).get("q", [""])[0][:120]
                try:
                    if path.endswith(".csv"):
                        self.respond(200, audit.to_csv(inventory, search_text), "text/csv; charset=utf-8",
                                     headers={"Content-Disposition": "attachment; filename=resourcehub-audit-log.csv"})
                    else:
                        self.respond(200, {"audit": audit.entries(inventory, search_text)})
                except sqlite3.Error:
                    self.respond(500, {"error": "Could not read the audit log."})
        elif path in ("/api/reports", "/api/reports/export.csv", "/api/backup"):
            with LOCK:
                username = self.current_user()
                if not username:
                    self.respond(401, {"error": "Please sign in to continue."})
                    return
                if path == "/api/backup":
                    if users[username]["role"] != "owner":
                        self.respond(403, {"error": "Only the owner can download database backups."})
                        return
                    try:
                        backup_data = backup_archive(users[username]["shop_id"])
                    except (OSError, sqlite3.Error):
                        self.respond(500, {"error": "Could not prepare the backup archive."})
                        return
                    if backup_data is None:
                        self.respond(404, {"error": "The inventory database is not available."})
                        return
                    self.respond(200, backup_data, "application/zip",
                                 headers={"Content-Disposition": "attachment; filename=resourcehub-backup.zip"})
                elif users[username]["role"] != "owner":
                    self.respond(403, {"error": "Only the owner can view or export reports."})
                    return
                else:
                    query = parse_qs(urlparse(self.path).query)
                    date_from = query.get("from", [""])[0]
                    date_to = query.get("to", [""])[0]
                    try:
                        date_from = date.fromisoformat(date_from).isoformat() if date_from else ""
                        date_to = date.fromisoformat(date_to).isoformat() if date_to else ""
                    except ValueError:
                        self.respond(400, {"error": "Enter valid report start and end dates."})
                        return
                    if date_from and date_to and date_from > date_to:
                        self.respond(400, {"error": "Report start date must be on or before the end date."})
                        return
                    if path.endswith(".csv"):
                        self.respond(200, reports_csv(date_from, date_to), "text/csv; charset=utf-8",
                                     headers={"Content-Disposition": "attachment; filename=resourcehub-report.csv"})
                    else:
                        self.respond(200, reports_summary(date_from, date_to))
        elif path == "/api/me":
            with LOCK:
                username = self.current_user()
                if not username:
                    activate_shop("default")
                self.respond(200, {"authenticated": bool(username),
                                   "name": users[username]["name"] if username else "",
                                   "role": users[username]["role"] if username else "",
                                   "registration_open": True,
                                   "turnstile_site_key": TURNSTILE_SITE_KEY,
                                   "settings": inventory.get_shop_settings()})
        else:
            self.respond(404, {"error": "Page not found."})

    def client_ip(self):
        """Return the visitor's IP. Behind a reverse proxy, set RESOURCEHUB_PROXY_HOPS
        to the number of trusted proxies (1 on Render) so limits apply per visitor."""
        address = self.client_address
        peer = address[0]
        if PROXY_HOPS < 1:
            return peer
        parts = [p.strip() for p in self.headers.get("X-Forwarded-For", "").split(",") if p.strip()]
        if len(parts) < PROXY_HOPS:
            return peer
        try:
            return str(ipaddress.ip_address(parts[-PROXY_HOPS]))
        except ValueError:
            return peer

    def current_user(self):
        """Return a valid account session and extend its inactivity timeout."""
        from http.cookies import SimpleCookie

        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
            token = cookie[SESSION_COOKIE].value
        except (KeyError, TypeError, ValueError):
            return None

        session = sessions.get(token)
        now = time.time()

        if not isinstance(session, dict) or session.get("expires", 0) <= now:
            sessions.pop(token, None)
            return None

        username = session.get("username")
        shop_id = session.get("shop_id")
        user = users.get(username) if isinstance(username, str) else None

        # Reject sessions whose account or tenant no longer matches.
        if (
            not isinstance(user, dict)
            or user.get("shop_id") != shop_id
            or user.get("role") not in ("owner", "staff")
        ):
            sessions.pop(token, None)
            return None

        activate_shop(shop_id)
        session["expires"] = now + SESSION_TTL
        return username

    def session_token(self):
        from http.cookies import SimpleCookie
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
            return cookie[SESSION_COOKIE].value
        except (KeyError, TypeError, ValueError):
            return None

    def set_session(self, username):
        token = secrets.token_urlsafe(32)
        sessions[token] = {"username": username, "shop_id": users[username]["shop_id"],
                           "expires": time.time() + SESSION_TTL}
        secure = "; Secure" if COOKIE_SECURE else ""
        return f"{SESSION_COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={SESSION_TTL}{secure}"

    def clear_session(self):
        from http.cookies import SimpleCookie
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
            token = cookie[SESSION_COOKIE].value
            sessions.pop(token, None)
        except Exception:
            pass
        secure = "; Secure" if COOKIE_SECURE else ""
        return f"{SESSION_COOKIE}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0{secure}"

    def is_same_origin_request(self):
        origin = self.headers.get("Origin")
        if not origin:
            return False

        expected_host = self.headers.get("Host", "").lower()
        parsed_origin = urlparse(origin)

        if parsed_origin.scheme not in ("http", "https"):
            return False
        if not parsed_origin.netloc or parsed_origin.path not in ("", "/"):
            return False
        if parsed_origin.query or parsed_origin.fragment or parsed_origin.username:
            return False

        return parsed_origin.netloc.lower() == expected_host

    def handle_asset_passport_post(self, path):
        try:
            data = self.read_json(max_bytes=16_384)
            with LOCK:
                username = self.current_user()
                if not username:
                    self.respond(401, {"error": "Your session expired. Please sign in again."})
                    return
                if users[username]["role"] != "owner":
                    self.respond(403, {"error": "Only the owner can manage asset passports."})
                    return
                if path == "/api/assets":
                    fields = ("resource_id", "serial_number", "location", "condition", "notes")
                    if any(not isinstance(data.get(key, ""), str) for key in fields):
                        raise ValueError("Asset passport fields must be text.")
                    success, message, asset = inventory.create_asset_passport(
                        data.get("resource_id", ""), data.get("serial_number", ""),
                        data.get("location", ""), data.get("condition", "good"),
                        data.get("notes", ""), actor=username,
                    )
                    if not success:
                        self.respond(400, {"error": message})
                        return
                    audit_log(self, username, "asset_created", asset["asset_tag"], message)
                    self.respond(201, {"message": message, "asset": asset})
                    return
                fields = ("asset_tag", "location", "condition", "status", "notes")
                if not isinstance(data.get("asset_tag"), str):
                    raise ValueError("Asset tag is required.")
                if any(key in data and not isinstance(data[key], str) for key in fields[1:]):
                    raise ValueError("Asset passport fields must be text.")
                success, message = inventory.update_asset_passport(
                    data["asset_tag"],
                    condition=data.get("condition"),
                    location=data.get("location"),
                    status=data.get("status"),
                    notes=data.get("notes"),
                    actor=username,
                )
                if not success:
                    self.respond(400, {"error": message})
                    return
                audit_log(self, username, "asset_updated", data["asset_tag"], message)
                self.respond(200, {"message": message})
        except ValueError as error:
            self.respond(400, {"error": str(error)})
        except (OSError, sqlite3.Error) as error:
            print(f"Asset passport action {path} failed: {error!r}", flush=True)
            self.respond(500, {"error": "Could not complete this asset passport change."})

    def handle_maintenance_post(self, path):
        try:
            data = self.read_json(max_bytes=16_384)
            with LOCK:
                username = self.current_user()
                if not username:
                    self.respond(401, {"error": "Your session expired. Please sign in again."})
                    return
                if users[username]["role"] != "owner":
                    self.respond(403, {"error": "Only the owner can manage maintenance records."})
                    return

                if path == "/api/maintenance":
                    text_fields = ("asset_tag", "problem", "action_taken", "technician", "status")
                    for field in text_fields:
                        if field in data and not isinstance(data[field], str):
                            raise ValueError(f"{field.replace('_', ' ').capitalize()} must be text.")
                    success, message, record = inventory.create_maintenance_record(
                        data.get("asset_tag", ""),
                        data.get("problem", ""),
                        action_taken=data.get("action_taken", ""),
                        cost=data.get("cost", 0),
                        technician=data.get("technician", ""),
                        status=data.get("status", "open"),
                        reported_by=username,
                    )
                    if not success:
                        self.respond(400, {"error": message})
                        return
                    audit_log(self, username, "maintenance_created",
                              record["asset_tag"], message)
                    self.respond(201, {"message": message, "record": record})
                    return

                if "id" not in data:
                    raise ValueError("Maintenance record ID is required.")
                text_fields = ("action_taken", "technician", "status")
                for field in text_fields:
                    if field in data and not isinstance(data[field], str):
                        raise ValueError(f"{field.replace('_', ' ').capitalize()} must be text.")
                success, message = inventory.update_maintenance_record(
                    data["id"],
                    action_taken=data.get("action_taken"),
                    cost=data.get("cost"),
                    technician=data.get("technician"),
                    status=data.get("status"),
                )
                if not success:
                    self.respond(400, {"error": message})
                    return
                audit_log(self, username, "maintenance_updated",
                          str(data["id"]), message)
                self.respond(200, {"message": message})
        except ValueError as error:
            self.respond(400, {"error": str(error)})
        except (OSError, sqlite3.Error) as error:
            print(f"Maintenance action {path} failed: {error!r}", flush=True)
            self.respond(500, {"error": "Could not complete this maintenance action."})

    def do_POST(self):
        path = urlparse(self.path).path
        if not self.is_same_origin_request():
            self.respond(403, {"error": "Request origin validation failed."})
            return
        if path in ("/api/maintenance", "/api/maintenance/update"):
            self.handle_maintenance_post(path)
            return
        if path in ("/api/assets", "/api/assets/update"):
            self.handle_asset_passport_post(path)
            return
        if path == "/api/send-verification":
            self.handle_email_verification()
            return
        if path in ("/api/register", "/api/login", "/api/logout"):
            self.handle_auth(path)
            return
        if path == "/api/change-password":
            self.handle_change_password()
            return
        if path == "/api/staff/reset-password":
            self.handle_staff_password_reset()
            return
        if path == "/api/password-reset/request":
            self.handle_reset_request()
            return
        if path == "/api/password-reset/confirm":
            self.handle_reset_confirm()
            return
        if path == "/api/staff":
            self.handle_staff_creation()
            return
        if path == "/api/staff/delete":
            self.handle_staff_deletion()
            return
        if path == "/api/settings/test-email":
            self.handle_test_email()
            return
        if path == "/api/settings":
            self.handle_shop_settings()
            return
        if path not in ("/api/resources", "/api/resource-details", "/api/delete-resource", "/api/restock", "/api/stocktake", "/api/sale", "/api/borrow", "/api/return", "/api/loan-due-date"):
            self.respond(404, {"error": "Action not found."})
            return
        try:
            data = self.read_json(max_bytes=128_000 if path == "/api/stocktake" else 16_384)
            if path == "/api/resources":
                if any(not isinstance(data.get(key, ""), str) for key in ("id", "name", "category", "sku", "supplier")):
                    raise ValueError("ID, name and category must be text.")
                if type(data.get("total")) is not int:
                    raise ValueError("Total units must be a whole number.")
                if type(data.get("price", 0)) not in (int, float) or type(data.get("reorder_level", 3)) is not int:
                    raise ValueError("Enter a valid price and restock threshold.")
            elif path == "/api/resource-details":
                if not isinstance(data.get("resource_id"), str) or not isinstance(data.get("sku"), str) or not isinstance(data.get("supplier"), str):
                    raise ValueError("Item ID, SKU, and supplier must be text.")
                if type(data.get("price")) not in (int, float) or type(data.get("reorder_level")) is not int:
                    raise ValueError("Enter a valid price and restock threshold.")
            elif path in ("/api/restock", "/api/delete-resource"):
                if not isinstance(data.get("resource_id"), str):
                    raise ValueError("Item ID must be text.")
                if path == "/api/restock" and type(data.get("quantity")) is not int:
                    raise ValueError("Restock quantity must be a whole number.")
            elif path == "/api/stocktake":
                if not isinstance(data.get("counts"), list) or not isinstance(data.get("note"), str):
                    raise ValueError("Stocktake counts and note are required.")
            elif path == "/api/sale":
                if not isinstance(data.get("resource_id"), str):
                    raise ValueError("Item ID must be text.")
                if type(data.get("quantity")) is not int:
                    raise ValueError("Quantity must be a whole number.")
                if not isinstance(data.get("customer", ""), str):
                    raise ValueError("Customer name must be text.")
            elif path == "/api/loan-due-date":
                if type(data.get("loan_id")) is not int or not isinstance(data.get("due_date"), str):
                    raise ValueError("Loan ID and due date are required.")
            else:
                if any(not isinstance(data.get(key), str) for key in ("fellow_id", "resource_id")):
                    raise ValueError("Fellow and equipment IDs must be text.")
                if type(data.get("quantity")) is not int:
                    raise ValueError("Quantity must be a whole number.")
                if path == "/api/return" and data.get("loan_id") is not None and type(data.get("loan_id")) is not int:
                    raise ValueError("Loan ID must be a whole number.")
            with LOCK:
                if not self.current_user():
                    self.respond(401, {"error": "Your session expired. Please sign in again."})
                    return
                username = self.current_user()
                if path in ("/api/resources", "/api/resource-details", "/api/delete-resource", "/api/restock", "/api/stocktake") and users[username]["role"] != "owner":
                    self.respond(403, {"error": "Only the owner can manage products or stock."})
                    return
                before_resources = deepcopy(inventory.resources)
                before_records = deepcopy(inventory.borrow_records)
                stocktake_changes = []
                if path == "/api/resources":
                    success, message = inventory.add_resource(
                        data.get("id", ""), data.get("name", ""),
                        data.get("category", ""), data.get("total"),
                        data.get("sku", ""), data.get("price", 0),
                        data.get("supplier", ""), data.get("reorder_level", 3),
                    )
                elif path == "/api/resource-details":
                    success, message = inventory.update_resource_details(
                        data.get("resource_id", ""), data.get("sku", ""),
                        data.get("price"), data.get("supplier", ""), data.get("reorder_level"),
                    )
                elif path == "/api/delete-resource":
                    success, message = inventory.delete_resource(data.get("resource_id", ""))
                elif path == "/api/restock":
                    success, message = inventory.restock_resource(
                        data.get("resource_id", ""), data.get("quantity"),
                    )
                elif path == "/api/stocktake":
                    success, message, stocktake_changes = inventory.apply_stocktake(
                        data.get("counts"), data.get("note"),
                    )
                elif path == "/api/sale":
                    success, message = inventory.record_sale(
                        data.get("resource_id", ""), data.get("quantity"), data.get("customer", ""),
                    )
                elif path == "/api/borrow":
                    success, message = inventory.borrow(
                        data.get("fellow_id", ""), data.get("resource_id", ""), data.get("quantity"),
                        data.get("due_date"),
                    )
                elif path == "/api/loan-due-date":
                    success, message = inventory.update_loan_due_date(
                        data.get("loan_id"), data.get("due_date"),
                    )
                else:
                    success, message = inventory.return_item(
                        data.get("fellow_id", ""), data.get("resource_id", ""), data.get("quantity"),
                        loan_id=data.get("loan_id"),
                    )
                if not success:
                    status = 409 if path == "/api/stocktake" and message.startswith("Inventory changed") else 400
                    self.respond(status, {"error": message})
                    return
                try:
                    inventory.save_data()
                except (OSError, TypeError, ValueError) as error:
                    inventory.resources[:] = before_resources
                    inventory.borrow_records[:] = before_records
                    self.respond(500, {"error": f"Could not save this change: {error}"})
                    return
                target = ("stocktake" if path == "/api/stocktake" else
                          f"loan:{data.get('loan_id')}" if path == "/api/loan-due-date" else
                          data.get("resource_id") or data.get("id", ""))
                audit_log(self, username, AUDIT_ACTIONS[path], target,
                          audit_detail(path, data, message, before_resources, stocktake_changes))
            self.respond(200, {"message": message})
        except ValueError as error:
            self.respond(400, {"error": str(error)})
        except (OSError, sqlite3.Error) as error:
            print(f"Inventory action {path} failed: {error!r}", flush=True)
            self.respond(500, {
                "error": "Could not complete this change. Check the server terminal for the cause and check inventory before retrying."
            })

    def handle_staff_creation(self):
        try:
            data = self.read_json()
            name = data.get("name", "")
            username = data.get("username", "")
            password = data.get("password", "")
            if not all(isinstance(value, str) for value in (name, username, password)):
                raise ValueError("Enter a name, username, and password.")
            name, username = name.strip(), username.strip().lower()
            if not name or len(name) > 80:
                raise ValueError("Name must be between 1 and 80 characters.")
            if len(username) < 3 or len(username) > 40 or not all(
                    char.isalnum() or char in "._-" for char in username):
                raise ValueError("Username must be 3–40 characters and use letters, numbers, dots, dashes or underscores.")
            if len(password) < 10:
                raise ValueError("Use a password with at least 10 characters.")
            with LOCK:
                actor = self.current_user()
                if not actor:
                    self.respond(401, {"error": "Your session expired. Please sign in again."})
                    return
                if users[actor]["role"] != "owner":
                    self.respond(403, {"error": "Only the owner can create staff accounts."})
                    return
                if username in users:
                    self.respond(409, {"error": "That username is already registered."})
                    return
                salt = secrets.token_bytes(16)
                digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PASSWORD_ROUNDS)
                users[username] = {"name": name, "salt": salt.hex(),
                                   "password_hash": digest.hex(), "role": "staff",
                                   "shop_id": users[actor]["shop_id"]}
                try:
                    save_users()
                except OSError as error:
                    users.pop(username, None)
                    self.respond(500, {"error": f"Could not save this staff account: {error}"})
                    return
                audit_log(self, actor, "staff_created", username, f"name={name}")
            self.respond(201, {"message": f"Staff account created for {name}."})
        except ValueError as error:
            self.respond(400, {"error": str(error)})

    def handle_email_verification(self):
        try:
            data = self.read_json()
            email = data.get("email", "")
            if not isinstance(email, str) or len(email) > 254 or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
                raise ValueError("Enter a valid email address.")
            email = email.strip().lower()
            ip = self.client_ip()
            if not verify_turnstile(data.get("turnstile_token", ""), ip):
                raise ValueError("Complete the bot verification check and try again.")
            now = time.time()
            with LOCK:
                purge_expired()
                previous = email_challenges.get(email)
                if (email in email_sending
                        or (previous and previous.get("sent", 0) > now - 60)
                        or email_ip_sends.get(ip, 0) > now - 60):
                    self.respond(429, {"error": "Wait one minute before requesting another code."})
                    return
                # Reserve the slot first, then send outside LOCK so a slow mail
                # server cannot freeze every other shop.
                email_sending.add(email)
                email_ip_sends[ip] = now
            try:
                code = send_email_code(email)
            except Exception:
                with LOCK:
                    email_sending.discard(email)
                    email_ip_sends.pop(ip, None)
                raise
            with LOCK:
                email_challenges[email] = {"code": code, "expires": time.time() + 600,
                                           "attempts": 0, "sent": time.time()}
                email_sending.discard(email)
            self.respond(200, {"message": "Verification code sent. Check your email."})
        except ValueError as error:
            self.respond(400, {"error": str(error)})
        except (OSError, RuntimeError, smtplib.SMTPException) as error:
            self.respond(503, {"error": str(error) or "Could not send the verification email."})

    def handle_change_password(self):
        try:
            data = self.read_json(max_bytes=4_000)
            current, new = data.get("current_password", ""), data.get("new_password", "")
            if not isinstance(current, str) or not current:
                raise ValueError("Enter your current password.")
            check_new_password(new)
            if new == current:
                raise ValueError("Choose a new password that is different from the current one.")
            ip = self.client_ip()
            with LOCK:
                username = self.current_user()
                if not username:
                    self.respond(401, {"error": "Your session expired. Please sign in again."})
                    return
                retry_after = login_retry_after(ip)
                if retry_after:
                    self.respond(429, {"error": "Too many failed attempts. Try again later."},
                                 headers={"Retry-After": str(retry_after)})
                    return
                snapshot = dict(users[username])
                token = self.session_token()
            expected = bytes.fromhex(snapshot["password_hash"])
            actual = hashlib.pbkdf2_hmac("sha256", current.encode("utf-8"),
                                         bytes.fromhex(snapshot["salt"]), PASSWORD_ROUNDS)
            if not hmac.compare_digest(actual, expected):
                with LOCK:
                    record_login_failure(ip)
                self.respond(403, {"error": "Your current password is incorrect."})
                return
            salt, digest = hash_new_password(new)
            with LOCK:
                if (self.current_user() != username
                        or users[username]["password_hash"] != snapshot["password_hash"]):
                    self.respond(409, {"error": "Your account changed while saving. Please try again."})
                    return
                user = users[username]
                old = (user["salt"], user["password_hash"])
                user["salt"], user["password_hash"] = salt.hex(), digest.hex()
                try:
                    save_users()
                except OSError as error:
                    user["salt"], user["password_hash"] = old
                    self.respond(500, {"error": f"Could not save the new password: {error}"})
                    return
                sign_out_user(username, keep_token=token)
                LOGIN_ATTEMPTS.pop(ip, None)
                audit_log(self, username, "password_changed", "", "changed by the user")
            self.respond(200, {"message": "Password changed. Other devices were signed out."})
        except ValueError as error:
            self.respond(400, {"error": str(error)})

    def handle_staff_password_reset(self):
        try:
            data = self.read_json(max_bytes=2_000)
            target = data.get("username", "")
            if not isinstance(target, str):
                raise ValueError("Choose a valid staff username.")
            target = target.strip().lower()
            with LOCK:
                actor = self.current_user()
                if not actor:
                    self.respond(401, {"error": "Your session expired. Please sign in again."})
                    return
                if users[actor]["role"] != "owner":
                    self.respond(403, {"error": "Only the owner can reset staff passwords."})
                    return
                member = users.get(target)
                if (not member or member.get("role") != "staff"
                        or member.get("shop_id") != users[actor].get("shop_id")):
                    self.respond(404, {"error": "Staff member not found in this shop."})
                    return
            temporary = secrets.token_urlsafe(9)
            salt, digest = hash_new_password(temporary)
            with LOCK:
                if self.current_user() != actor or users.get(target) is not member:
                    self.respond(409, {"error": "The account changed while saving. Please try again."})
                    return
                old = (member["salt"], member["password_hash"])
                member["salt"], member["password_hash"] = salt.hex(), digest.hex()
                try:
                    save_users()
                except OSError as error:
                    member["salt"], member["password_hash"] = old
                    self.respond(500, {"error": f"Could not reset this password: {error}"})
                    return
                sign_out_user(target)
                audit_log(self, actor, "staff_password_reset", target)
            self.respond(200, {"message": f"New password for {target}: {temporary}  Share it privately. "
                                          "They can change it after signing in."})
        except ValueError as error:
            self.respond(400, {"error": str(error)})

    def handle_reset_request(self):
        try:
            data = self.read_json(max_bytes=2_000)
            username = data.get("username", "")
            if not isinstance(username, str) or not username.strip():
                raise ValueError("Enter your username.")
            username = username.strip().lower()[:40]
            smtp_settings()  # configuration problems are reported the same way for everyone
            ip, now = self.client_ip(), time.time()
            reply = {"message": "If that owner account has a recovery email, a code has been sent to it."}
            with LOCK:
                for key in [k for k, t in reset_ip_sends.items() if t < now - 3600]:
                    reset_ip_sends.pop(key, None)
                for key in [k for k, c in reset_challenges.items() if c["expires"] < now]:
                    reset_challenges.pop(key, None)
                if reset_ip_sends.get(ip, 0) > now - 60:
                    self.respond(429, {"error": "Wait one minute before requesting another code."})
                    return
                reset_ip_sends[ip] = now
                user = users.get(username)
                target = user.get("email") if user and user.get("role") == "owner" else None
                prior = reset_challenges.get(username)
                if target and not (prior and prior["sent"] > now - 60):
                    code = f"{secrets.randbelow(1_000_000):06d}"
                    reset_challenges[username] = {"code": code, "expires": now + RESET_TTL,
                                                  "attempts": 0, "sent": now}
                    Thread(target=send_reset_email, args=(username, target, code), daemon=True).start()
            self.respond(200, reply)
        except ValueError as error:
            self.respond(400, {"error": str(error)})
        except RuntimeError as error:
            self.respond(503, {"error": str(error)})

    def handle_reset_confirm(self):
        try:
            data = self.read_json(max_bytes=4_000)
            username, code, new = data.get("username", ""), data.get("code", ""), data.get("new_password", "")
            if not isinstance(username, str) or not isinstance(code, str):
                raise ValueError("Enter your username and the code from your email.")
            check_new_password(new)
            username, ip = username.strip().lower()[:40], self.client_ip()
            invalid = "That code is invalid or expired. Request a new one."
            with LOCK:
                retry_after = login_retry_after(ip)
                if retry_after:
                    self.respond(429, {"error": "Too many failed attempts. Try again later."},
                                 headers={"Retry-After": str(retry_after)})
                    return
                challenge = reset_challenges.get(username)
                if (not challenge or challenge["expires"] < time.time() or challenge["attempts"] >= 5
                        or not hmac.compare_digest(code.strip().encode("utf-8"), challenge["code"].encode("utf-8"))):
                    if challenge:
                        challenge["attempts"] += 1
                    record_login_failure(ip)
                    raise ValueError(invalid)
                reset_challenges.pop(username, None)  # a code works once
            salt, digest = hash_new_password(new)
            with LOCK:
                user = users.get(username)
                if not user:
                    raise ValueError(invalid)
                old = (user["salt"], user["password_hash"])
                user["salt"], user["password_hash"] = salt.hex(), digest.hex()
                try:
                    save_users()
                except OSError as error:
                    user["salt"], user["password_hash"] = old
                    self.respond(500, {"error": f"Could not save the new password: {error}"})
                    return
                sign_out_user(username)
                LOGIN_ATTEMPTS.pop(ip, None)
                try:
                    activate_shop(user["shop_id"])
                    audit_log(self, username, "password_reset", "", "reset with an emailed code")
                except (RuntimeError, OSError, sqlite3.Error):
                    pass
            self.respond(200, {"message": "Password updated. Sign in with your new password."})
        except ValueError as error:
            self.respond(400, {"error": str(error)})

    def handle_staff_deletion(self):
        try:
            data = self.read_json()
            target = data.get("username", "")
            if not isinstance(target, str):
                raise ValueError("Choose a valid staff username.")
            target = target.strip().lower()
            with LOCK:
                actor = self.current_user()
                if not actor:
                    self.respond(401, {"error": "Your session expired. Please sign in again."})
                    return
                if users[actor]["role"] != "owner":
                    self.respond(403, {"error": "Only the owner can manage staff accounts."})
                    return
                member = users.get(target)
                if not member or member.get("role") != "staff" or member.get("shop_id") != users[actor].get("shop_id"):
                    self.respond(404, {"error": "Staff member not found in this shop."})
                    return
                users.pop(target)
                try:
                    save_users()
                except OSError as error:
                    users[target] = member
                    self.respond(500, {"error": f"Could not remove this staff account: {error}"})
                    return
                for token, session in list(sessions.items()):
                    if session["username"] == target:
                        sessions.pop(token, None)
                audit_log(self, actor, "staff_removed", target)
            self.respond(200, {"message": f"Removed staff account {target}."})
        except ValueError as error:
            self.respond(400, {"error": str(error)})

    def handle_test_email(self):
        try:
            data = self.read_json(max_bytes=10_000)
            with LOCK:
                actor = self.current_user()
                if not actor:
                    self.respond(401, {"error": "Your session expired. Please sign in again."})
                    return
                if users[actor]["role"] != "owner":
                    self.respond(403, {"error": "Only the owner can test email settings."})
                    return

            send_test_email(str(data.get("email", "")))
            self.respond(200, {"message": "Test email sent successfully. Check the recipient inbox."})
        except ValueError as error:
            self.respond(400, {"error": str(error)})
        except (OSError, RuntimeError, smtplib.SMTPException, ValueError):
            self.respond(503, {"error": "Email test failed. Check your SMTP settings in .env."})

    def handle_shop_settings(self):
        try:
            data = self.read_json(max_bytes=400_000)
            with LOCK:
                actor = self.current_user()
                if not actor:
                    self.respond(401, {"error": "Your session expired. Please sign in again."})
                    return
                if users[actor]["role"] != "owner":
                    self.respond(403, {"error": "Only the owner can change shop settings."})
                    return
                success, message = inventory.update_shop_settings(
                    data.get("shop_name"), data.get("brand_color"),
                    data.get("currency"), data.get("logo_data", ""),
                )
                if success:
                    audit_log(self, actor, "settings_changed", "",
                              f"name={data.get('shop_name')}; currency={data.get('currency')}; "
                              f"color={data.get('brand_color')}")
            if not success:
                self.respond(400, {"error": message})
                return
            self.respond(200, {"message": message})
        except ValueError as error:
            self.respond(400, {"error": str(error)})
        except (OSError, sqlite3.Error):
            self.respond(500, {"error": "Could not save shop settings safely."})

    def handle_auth(self, path):
        try:
            data = self.read_json()
            hashed = prehash_password(path, data, self.client_ip())
            with LOCK:
                purge_expired()
                if path == "/api/logout":
                    cookie = self.clear_session()
                    self.respond(200, {"message": "You have been signed out."}, headers={"Set-Cookie": cookie})
                    return

                username = data.get("username", "")
                password = data.get("password", "")
                if not isinstance(username, str) or not isinstance(password, str):
                    raise ValueError("Enter a username and password.")
                username = username.strip().lower()
                login_ip = self.client_ip()

                if path == "/api/login":
                    retry_after = login_retry_after(login_ip)
                    if retry_after:
                        self.respond(
                            429,
                            {"error": "Too many failed sign-in attempts. Try again later."},
                            headers={"Retry-After": str(retry_after)},
                        )
                        return

                if path == "/api/register":
                    name = data.get("name", "")
                    company = data.get("company_name", "")
                    products = data.get("products", "")
                    email = data.get("email", "")
                    phone = data.get("phone", "")
                    email_code = data.get("email_code", "")
                    if not all(isinstance(value, str) for value in (name, company, products, email, phone, email_code)):
                        raise ValueError("Complete the company and owner details.")
                    name, company, products, email, phone = name.strip(), company.strip(), products.strip(), email.strip().lower(), phone.strip()
                    if not name:
                        raise ValueError("Enter your name.")
                    if len(name.strip()) > 80:
                        raise ValueError("Your name must be 80 characters or fewer.")
                    if not company or len(company) > 100:
                        raise ValueError("Company name must be between 1 and 100 characters.")
                    if len(products) > 120:
                        raise ValueError("Product or service description must be 120 characters or fewer.")
                    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email) or len(email) > 254:
                        raise ValueError("Enter a valid company email address.")
                    if not re.fullmatch(r"[+0-9() .-]{7,24}", phone):
                        raise ValueError("Enter a valid company phone number.")
                    if len(username) < 3 or len(username) > 40 or not all(
                            char.isalnum() or char in "._-" for char in username):
                        raise ValueError("Username must be 3–40 characters and use letters, numbers, dots, dashes or underscores.")
                    if len(password) < 10:
                        raise ValueError("Use a password with at least 10 characters.")
                    challenge = email_challenges.get(email)
                    if (not challenge or challenge["expires"] < time.time()
                            or challenge["attempts"] >= 5 or not hmac.compare_digest(email_code.encode("utf-8"), challenge["code"].encode("utf-8"))):
                        if challenge:
                            challenge["attempts"] += 1
                        raise ValueError("Enter the valid email verification code sent to your company email.")
                    if not verify_turnstile(data.get("turnstile_token", ""), self.client_ip()):
                        raise ValueError("Complete the bot verification check and try again.")
                    if username in users:
                        self.respond(409, {"error": "That username is already registered."})
                        return
                    if hashed is None:
                        salt = secrets.token_bytes(16)
                        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PASSWORD_ROUNDS)
                    else:
                        salt, digest = hashed["salt"], hashed["digest"]
                    shop_id = secrets.token_hex(8)
                    slug_base = re.sub(r"[^a-z0-9]+", "-", company.lower()).strip("-")[:50] or "shop"
                    slug = slug_base
                    while any(shop["slug"] == slug for shop in shops.values()):
                        slug = f"{slug_base}-{secrets.token_hex(2)}"
                    create_shop_database(shop_id, company, phone, email)
                    shops[shop_id] = {"id": shop_id, "name": company, "phone": phone,
                                      "email": email, "products": products, "slug": slug}
                    users[username] = {"name": name, "salt": salt.hex(),
                                       "password_hash": digest.hex(), "role": "owner",
                                       "shop_id": shop_id, "email": email}
                    try:
                        save_users()
                    except OSError as error:
                        users.pop(username, None)
                        shops.pop(shop_id, None)
                        self.respond(500, {"error": f"Could not save your account: {error}"})
                        return
                    email_challenges.pop(email, None)
                else:
                    user = users.get(username)
                    if not user:
                        record_login_failure(login_ip)
                        self.respond(401, {"error": "Username or password is incorrect."})
                        return
                    expected = bytes.fromhex(user["password_hash"])
                    user_salt = bytes.fromhex(user["salt"])
                    if hashed is not None and hashed["salt"] == user_salt:
                        actual = hashed["digest"]
                    else:
                        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), user_salt, PASSWORD_ROUNDS)
                    if not hmac.compare_digest(actual, expected):
                        record_login_failure(login_ip)
                        self.respond(401, {"error": "Username or password is incorrect."})
                        return

                    LOGIN_ATTEMPTS.pop(login_ip, None)

                cookie = self.set_session(username)
                self.respond(200, {"message": "Signed in.", "name": users[username]["name"], "role": users[username]["role"]},
                             headers={"Set-Cookie": cookie})
        except ValueError as error:
            self.respond(400, {"error": str(error)})
        except (OSError, RuntimeError, sqlite3.Error) as error:
            self.respond(500, {"error": f"Could not create the shop account: {error}"})

    def log_message(self, fmt, *args):
        print("%s - %s" % (self.address_string(), fmt % args))


def main():
    
    inventory.load_data()
    load_users()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"ResourceHub is running at http://{HOST}:{PORT}")
    print("Press Ctrl+C to stop the server.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping ResourceHub...")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
