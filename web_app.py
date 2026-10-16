"""A small browser interface for the ResourceHub resource manager."""
from copy import deepcopy
import csv
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
import re
import secrets
import shutil
import sqlite3
import smtplib
import tempfile
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from threading import RLock
import time
from urllib.parse import parse_qs, urlparse
import zipfile

import main as inventory


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
TURNSTILE_SITE_KEY = os.environ.get(
    "TURNSTILE_SITE_KEY", "1x00000000000000000000AA" if LOCAL_DEVELOPMENT else "")


def activate_shop(shop_id):
    """Select the isolated SQLite file for a shop while holding LOCK."""
    if shop_id != "default" and not re.fullmatch(r"[a-f0-9]{16}", str(shop_id)):
        raise RuntimeError("Invalid shop account configuration.")
    if shop_id == "default":
        inventory.DB_FILE = os.path.join(inventory.DATA_DIR, "campus_resources.db")
        inventory.BACKUP_DIR = ROOT_BACKUP_DIR
    else:
        shop_dir = os.path.join(TENANT_DIR, shop_id)
        os.makedirs(shop_dir, exist_ok=True)
        inventory.DB_FILE = os.path.join(shop_dir, "campus_resources.db")
        inventory.BACKUP_DIR = os.path.join(shop_dir, "backups")
    if os.path.exists(inventory.DB_FILE):
        inventory.refresh_state()


def create_shop_database(shop_id, shop_name, phone, email):
    """Create an empty tenant database without the demo inventory."""
    previous_db, previous_backup = inventory.DB_FILE, inventory.BACKUP_DIR
    try:
        activate_shop(shop_id)
        inventory.init_db()
        with inventory.connect() as db:
            db.execute("DELETE FROM loans")
            db.execute("DELETE FROM sales")
            db.execute("DELETE FROM stock_movements")
            db.execute("DELETE FROM resources")
            db.execute("DELETE FROM fellows")
            db.execute("UPDATE shop_settings SET shop_name=? WHERE id=1", (shop_name,))
        inventory.refresh_state()
    finally:
        inventory.DB_FILE, inventory.BACKUP_DIR = previous_db, previous_backup
        inventory.refresh_state()


def send_email_code(email):
    host = os.environ.get("RESOURCEHUB_SMTP_HOST", "smtp.gmail.com")
    sender = os.environ.get("RESOURCEHUB_SMTP_FROM", "")
    password = os.environ.get("RESOURCEHUB_SMTP_PASSWORD", "").replace(" ", "")
    if (not sender or not password or sender.lower() == "your-sender@gmail.com"
            or password.lower() == "your-google-app-password"):
        raise RuntimeError("Replace the example sender and password in .env with your sending Gmail address and its Google App Password.")
    if len(password) != 16:
        raise RuntimeError("The Gmail app password should be 16 characters. Use a Google App Password, not your regular Gmail password.")
    code = f"{secrets.randbelow(1_000_000):06d}"
    message = (f"From: ResourceHub <{sender}>\r\nTo: {email}\r\n"
               "Subject: Your ResourceHub verification code\r\n"
               "Content-Type: text/plain; charset=utf-8\r\n\r\n"
               f"Your verification code is {code}. It expires in 10 minutes.")
    port = int(os.environ.get("RESOURCEHUB_SMTP_PORT", "587"))
    with smtplib.SMTP(host, port, timeout=15) as smtp:
        smtp.starttls()
        smtp.login(os.environ.get("RESOURCEHUB_SMTP_USERNAME", sender),
                   password)
        smtp.sendmail(sender, [email], message.encode("utf-8"))
    email_challenges[email.lower()] = {"code": code, "expires": time.time() + 600, "attempts": 0}


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

PAGE = r'''<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="theme-color" content="#101b2d">
  <title>ResourceHub · Vendor stock desk</title>
  <style>
    :root{color-scheme:light;--ink:#182538;--muted:#718096;--line:#e6eaf0;--paper:#fff;--wash:#f4f6fa;--navy:#101b2d;--blue:#536dfe;--green:#16866a;--amber:#b66a12;--red:#bb4052;--shadow:0 12px 30px #17233a0b}
    *{box-sizing:border-box}[hidden]{display:none!important}body{margin:0;background:var(--wash);color:var(--ink);font:15px/1.5 Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif}button,input,select{font:inherit}button{cursor:pointer}
    .shell{min-height:100vh}.topbar{height:72px;background:var(--navy);color:white;display:flex;align-items:center;justify-content:space-between;padding:0 max(24px,calc((100vw - 1320px)/2));gap:18px}.brand{display:flex;align-items:center;gap:12px;font-weight:700;letter-spacing:.01em}.brand-mark{height:36px;width:36px;border-radius:11px;background:var(--blue);display:grid;place-items:center;color:#fff;position:relative;overflow:hidden;flex:0 0 auto}.brand-mark svg{width:21px;height:21px}.brand-mark .shop-logo{width:100%;height:100%;object-fit:contain;padding:2px}.brand small{display:block;color:#aab6ca;font-size:11px;font-weight:500;letter-spacing:.08em;text-transform:uppercase}.top-right{display:flex;align-items:center;gap:10px;color:#c2ccda;font-size:13px}.live{height:8px;width:8px;background:#42d3a5;border-radius:50%;box-shadow:0 0 0 4px #42d3a522}
    main{max-width:1320px;margin:auto;padding:36px 24px 60px}.heading{display:flex;justify-content:space-between;align-items:flex-end;gap:18px;margin-bottom:26px}.eyebrow{color:var(--blue);font-weight:700;text-transform:uppercase;letter-spacing:.12em;font-size:11px}.heading h1{font-size:30px;line-height:1.2;margin:7px 0}.heading p{color:var(--muted);margin:0}.today{color:var(--muted);font-size:13px;white-space:nowrap}
    .stats{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:15px;margin-bottom:22px}.stat,.panel{background:var(--paper);border:1px solid var(--line);border-radius:15px;box-shadow:var(--shadow)}.stat{padding:19px 20px;position:relative;overflow:hidden}.stat:after{content:"";position:absolute;width:64px;height:64px;border-radius:50%;right:-18px;top:-20px;background:#536dfe0b}.stat-label{font-size:12px;color:var(--muted);font-weight:600}.stat-value{font-size:27px;font-weight:750;letter-spacing:-.04em;margin-top:7px}.stat-foot{font-size:11px;color:#99a3b2;margin-top:2px}
    .layout{display:grid;grid-template-columns:minmax(0,1fr) 330px;gap:18px;align-items:start}.panel{padding:21px}.panel-head{display:flex;justify-content:space-between;align-items:center;gap:12px;margin-bottom:17px}.panel-head h2{font-size:17px;margin:0;letter-spacing:-.02em}.subtle{color:var(--muted);font-size:12px;margin-top:3px}.actions{display:flex;gap:9px;align-items:center}.search{width:205px;padding:9px 11px;border:1px solid var(--line);border-radius:9px;background:#fff;outline:none}.search:focus,input:focus,select:focus{border-color:#8997ff;box-shadow:0 0 0 3px #536dfe19;outline:none}.btn{border:0;border-radius:9px;background:var(--blue);color:#fff;font-weight:650;padding:10px 14px;box-shadow:0 4px 10px #536dfe25}.btn:hover{background:#4059e8}.btn.secondary{background:#f0f2ff;color:#4059e8;box-shadow:none}.btn.secondary:hover{background:#e5e9ff}.btn.full{width:100%;margin-top:4px}.table-wrap{overflow:auto}table{width:100%;border-collapse:collapse;white-space:nowrap}th{text-align:left;color:#8a95a5;font-size:10px;text-transform:uppercase;letter-spacing:.09em;padding:11px 10px;border-bottom:1px solid var(--line)}td{padding:13px 10px;border-bottom:1px solid #eff1f5;font-size:13px}tbody tr:last-child td{border-bottom:0}.resource-name{font-weight:650}.resource-id{color:#97a1af;font-size:11px;margin-top:2px}.badge{display:inline-flex;align-items:center;gap:6px;border-radius:99px;padding:4px 8px;font-size:11px;font-weight:650;background:#eaf7f2;color:var(--green)}.badge:before{content:"";width:6px;height:6px;border-radius:50%;background:currentColor}.badge.low{background:#fff4e6;color:var(--amber)}.badge.out{background:#fff0f1;color:var(--red)}.empty{text-align:center;padding:32px;color:var(--muted)}
    .side{display:grid;gap:16px}.form-panel h2{font-size:16px;margin:0}.form-panel .subtle{margin-bottom:16px}.tabs{display:flex;background:#f1f3f8;padding:4px;border-radius:10px;margin-bottom:17px}.tab{border:0;background:transparent;border-radius:7px;flex:1;padding:8px;color:var(--muted);font-weight:650;font-size:12px}.tab.active{color:var(--ink);background:#fff;box-shadow:0 1px 4px #14213d13}.field{margin-bottom:12px}.field label{display:block;font-size:11px;color:#5c697b;font-weight:650;margin-bottom:5px}.field input,.field select{width:100%;border:1px solid var(--line);border-radius:9px;padding:10px 11px;background:#fff;color:var(--ink)}.form-message{min-height:18px;margin-top:10px;font-size:12px;color:var(--green)}.form-message.error{color:var(--red)}.loan-list{display:grid;gap:11px}.loan-row{display:flex;justify-content:space-between;gap:10px;align-items:center;border-bottom:1px solid #eff1f5;padding-bottom:10px}.loan-row:last-child{border:0;padding:0}.loan-who{font-size:12px;font-weight:650}.loan-what{font-size:11px;color:var(--muted)}.loan-count{font-weight:700;font-size:12px;color:#394c69;white-space:nowrap}.toast{position:fixed;right:22px;bottom:22px;background:#182538;color:#fff;padding:12px 16px;border-radius:10px;box-shadow:var(--shadow);opacity:0;transform:translateY(10px);transition:.2s;pointer-events:none}.toast.show{opacity:1;transform:none}.footnote{margin-top:17px;color:#9aa4b2;font-size:11px;text-align:center}.auth-screen{min-height:100vh;display:grid;place-items:center;padding:24px;background:radial-gradient(ellipse at 18% 12%,#263963 0,transparent 42%),var(--navy)}.auth-card{width:min(430px,100%);background:white;border-radius:19px;padding:34px;box-shadow:0 25px 80px #0004}.auth-brand{display:flex;align-items:center;gap:11px;font-weight:750;margin-bottom:27px}.auth-card h1{font-size:25px;margin:0;letter-spacing:-.04em}.auth-card>p{color:var(--muted);margin:6px 0 22px;font-size:13px}.auth-tabs{margin-bottom:20px}.auth-card .field{margin-bottom:14px}.auth-error{color:var(--red);font-size:12px;min-height:18px;margin:10px 0}.account-name{color:white;font-size:12px}.logout{border:1px solid #ffffff35;color:white;background:transparent;border-radius:8px;padding:7px 10px;font-size:12px}.logout:hover{background:#ffffff14}
    @media(max-width:940px){.layout{grid-template-columns:1fr}.side{grid-template-columns:repeat(2,minmax(0,1fr))}.stats{grid-template-columns:repeat(2,1fr)}}@media(max-width:600px){.topbar{height:62px;padding:0 17px}.top-right .status-text{display:none}main{padding:25px 14px 40px}.heading{align-items:flex-start;flex-direction:column}.heading h1{font-size:26px}.today{display:none}.stats{gap:10px}.stat{padding:15px}.stat-value{font-size:24px}.layout{gap:13px}.panel{padding:16px}.panel-head{align-items:flex-start;flex-direction:column}.actions,.search{width:100%}.side{grid-template-columns:1fr}th,td{padding-left:7px;padding-right:7px}}
    .landing-screen{min-height:100vh;overflow:hidden;background:radial-gradient(ellipse at 83% 44%,#e8edff 0,transparent 34%),linear-gradient(135deg,#f8faff,#fff 58%,#eef2ff)}
    .landing-nav{height:82px;max-width:1240px;margin:auto;padding:0 28px;display:flex;align-items:center;justify-content:space-between}
    .landing-brand{display:flex;align-items:center;gap:12px;color:var(--ink);font-size:18px;font-weight:800}.landing-brand .brand-mark{color:white}
    .landing-content{max-width:1240px;min-height:calc(100vh - 82px);margin:auto;padding:48px 28px 74px;display:grid;grid-template-columns:1.02fr .98fr;align-items:center;gap:50px}
    .landing-copy .eyebrow{display:inline-flex;align-items:center;gap:8px;background:#eef1ff;padding:8px 12px;border-radius:99px}.landing-copy h1{max-width:650px;margin:22px 0 17px;font-size:clamp(42px,6vw,72px);line-height:1.02;letter-spacing:-.065em;color:#14213a}.landing-copy h1 span{color:var(--blue)}.landing-copy>p{max-width:540px;margin:0;color:#64738a;font-size:18px;line-height:1.7}.landing-actions{display:flex;align-items:center;gap:14px;margin-top:30px}.landing-actions .btn{padding:13px 19px;border-radius:11px}.landing-note{font-size:12px;color:#8793a5;margin-top:19px}
    .preview-wrap{position:relative;padding:24px}.preview-glow{position:absolute;inset:12% 4%;background:#536dfe26;filter:blur(46px);border-radius:40%}.inventory-preview{position:relative;background:#fff;border:1px solid #e8ebf3;border-radius:22px;padding:22px;box-shadow:0 30px 90px #273c7320;transform:rotate(1deg)}.preview-top{display:flex;justify-content:space-between;align-items:center;margin-bottom:22px}.preview-title{font-weight:750}.preview-status{font-size:11px;color:var(--green);background:#e9f8f2;padding:6px 9px;border-radius:99px}.preview-stats{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}.preview-stat{padding:13px;border:1px solid #edf0f5;border-radius:12px}.preview-stat strong{display:block;font-size:23px;letter-spacing:-.04em}.preview-stat small{font-size:10px;color:#8490a2}.preview-list{margin-top:20px;border:1px solid #edf0f5;border-radius:14px;overflow:hidden}.preview-row{display:grid;grid-template-columns:1fr auto auto;gap:20px;padding:13px 15px;align-items:center;border-bottom:1px solid #f0f2f6;font-size:12px}.preview-row:last-child{border-bottom:0}.preview-row span:first-child{font-weight:650}.preview-row small{color:#8490a2}.preview-pill{background:#eef1ff;color:#536dfe;padding:5px 8px;border-radius:99px;font-size:10px;font-weight:700}.landing-points{display:flex;gap:20px;margin-top:27px;color:#65748b;font-size:12px}.landing-points span{display:flex;align-items:center;gap:7px}.landing-points b{color:var(--green);font-size:15px}
    .auth-back{display:block;margin:0 auto 14px;background:none;border:0;color:#536dfe;font-size:12px;font-weight:650}
    .landing-features{max-width:1184px;margin:0 auto;padding:0 28px 76px;display:grid;grid-template-columns:repeat(3,1fr);gap:15px}.landing-feature{padding:21px 22px;background:#ffffffc9;border:1px solid #e8ebf3;border-radius:16px}.landing-feature small{color:#536dfe;font-weight:750;letter-spacing:.1em;font-size:10px}.landing-feature h2{font-size:17px;margin:10px 0 6px;letter-spacing:-.02em}.landing-feature p{margin:0;color:#718096;font-size:12px;line-height:1.65}
    .category-filter{min-width:150px;padding:11px 12px;border:1px solid var(--line);border-radius:9px;background:#fff;color:var(--ink)}.watch-list{display:grid;gap:8px}.watch-row{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:10px 11px;border-radius:10px;background:#fafbfe}.watch-name{font-size:12px;font-weight:650}.watch-detail{font-size:10px;color:var(--muted)}.watch-count{font-size:11px;font-weight:700;color:var(--amber);white-space:nowrap}.watch-count.out{color:var(--red)}
    @media(max-width:850px){.landing-content{grid-template-columns:1fr;gap:20px;padding-top:55px}.landing-copy h1{max-width:720px}.preview-wrap{max-width:620px;width:100%;margin:auto}.landing-screen{overflow:auto}}
    @media(max-width:700px){.landing-features{grid-template-columns:1fr;padding:0 20px 48px}.category-filter{width:100%}}
    @media(max-width:520px){.landing-nav{height:70px;padding:0 18px}.landing-content{padding:44px 20px 52px}.landing-copy h1{font-size:43px}.landing-copy>p{font-size:15px}.landing-actions{align-items:flex-start;flex-direction:column}.preview-wrap{padding:10px 0}.inventory-preview{padding:15px}.preview-stat{padding:9px}.preview-stat strong{font-size:19px}.preview-row{gap:8px;padding:12px 10px}.landing-points{flex-wrap:wrap;gap:10px 16px}}
    /* Dashboard polish: clearer surfaces, spacing, focus, and interaction feedback. */
    body{background:#f5f7fc}
    .topbar{position:sticky;top:0;z-index:5;background:#111d32;box-shadow:0 5px 20px #101b2d18}
    main{max-width:1400px;padding-top:42px}
    .heading{margin-bottom:30px}.heading h1{font-size:34px;letter-spacing:-.045em}.heading p{font-size:14px;margin-top:7px}
    .stats{gap:17px;margin-bottom:25px}.stat{border-radius:17px;padding:21px 22px;transition:transform .18s ease,box-shadow .18s ease}.stat:hover{transform:translateY(-3px);box-shadow:0 16px 35px #17233a12}.stat-value{font-size:30px}
    .panel{border-radius:17px;box-shadow:0 8px 28px #17233a08}.layout{gap:20px}.panel-head h2{font-size:18px}.subtle{line-height:1.5}
    .search{width:230px;padding:11px 13px}.btn{transition:transform .16s ease,background .16s ease,box-shadow .16s ease}.btn:hover{transform:translateY(-1px);box-shadow:0 7px 16px #536dfe35}.btn:active{transform:translateY(0)}.btn:disabled{cursor:not-allowed;opacity:.48;transform:none;box-shadow:none}
    table{border-spacing:0}th{padding-top:13px;padding-bottom:13px}td{padding-top:15px;padding-bottom:15px}tbody tr{transition:background .14s ease}tbody tr:hover{background:#f8f9ff}.badge{padding:5px 10px}
    .form-panel{border-top:3px solid #536dfe}.tabs{padding:5px}.tab{padding:9px}.field input,.field select{padding:11px 12px;transition:border-color .15s ease,box-shadow .15s ease}.field input:disabled,.field select:disabled{background:#f4f6fa;color:#9aa4b2;cursor:not-allowed}
    .loan-row{padding:12px 0}.loan-list{gap:0}.loan-row:hover .loan-who{color:#536dfe}.empty{border-radius:12px;background:#fafbfe}
    :focus-visible{outline:3px solid #8997ff!important;outline-offset:2px}
    @media(prefers-reduced-motion:reduce){*,*::before,*::after{scroll-behavior:auto!important;transition:none!important}}
    @media(max-width:600px){main{padding-top:28px}.heading h1{font-size:28px}.search{width:100%}.actions{display:grid;grid-template-columns:1fr 1fr}.actions .search,.actions .btn{grid-column:1/-1}.category-filter{min-width:0}.topbar{backdrop-filter:blur(12px)}}
  </style>
</head>
<body><div class="shell">
  <section class="landing-screen" id="landingScreen">
    <nav class="landing-nav"><div class="landing-brand"><div class="brand-mark" data-shop-logo><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 8.5 12 4l8 4.5v9L12 22l-8-4.5zM4 8.5l8 4.5 8-4.5M12 13v9m-8-13 8 4.5 8-4.5" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/></svg><img class="shop-logo" alt="Shop logo" hidden></div><span id="landingBrandName">ResourceHub</span></div><button class="btn" type="button" data-open-auth>Sign in</button></nav>
    <div class="landing-content">
      <div class="landing-copy"><div class="eyebrow"><span class="live"></span> DIGITAL SHOP INVENTORY</div><h1>Your stock.<br><span>Clearly in hand.</span></h1><p>Track every shop item, how many have been disbursed, and what remains so you can plan when to stock in.</p><div class="landing-actions"><button class="btn" type="button" data-open-auth>Open <span id="landingCtaBrand">ResourceHub</span> <span aria-hidden="true">→</span></button><span class="subtle">Simple stock control for your shop.</span></div><div class="landing-points"><span><b>✓</b> See remaining stock</span><span><b>✓</b> Record transactions</span><span><b>✓</b> Get low stock alerts</span></div></div>
      <div class="preview-wrap"><div class="preview-glow"></div><div class="inventory-preview"><div class="preview-top"><div><div class="eyebrow">SAMPLE RESOURCE DESK</div><div class="preview-title">A quick look inside</div></div><span class="preview-status">Inventory overview</span></div><div class="preview-stats"><div class="preview-stat"><strong>18</strong><small>Total units</small></div><div class="preview-stat"><strong>12</strong><small>Available now</small></div><div class="preview-stat"><strong>6</strong><small>On loan</small></div></div><div class="preview-list"><div class="preview-row"><span>💻 Laptop</span><small>Electronics</small><span class="preview-pill">7 ready</span></div><div class="preview-row"><span>⌨️ Keyboard</span><small>Accessories</small><span class="preview-pill">3 ready</span></div><div class="preview-row"><span>🎧 Headset</span><small>Accessories</small><span class="preview-pill">2 ready</span></div></div></div></div>
    </div>
    <section class="landing-features" aria-label="ResourceHub features"><article class="landing-feature"><small>01 / INVENTORY</small><h2>Know what’s in the shop</h2><p>See stocked, disbursed, and remaining quantities for every item.</p></article><article class="landing-feature"><small>02 / STOCK IN</small><h2>Update incoming stock</h2><p>Record new deliveries to keep the available count current.</p></article><article class="landing-feature"><small>03 / TRANSACTIONS</small><h2>Keep transactions straight</h2><p>Keep the existing borrow, return, and low stock tracking tools.</p></article></section>
  </section>
  <section class="auth-screen" id="authScreen" hidden><div class="auth-card"><button class="auth-back" type="button" id="backLanding">← Back to ResourceHub</button><div class="auth-brand"><div class="brand-mark" data-shop-logo><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 8.5 12 4l8 4.5v9L12 22l-8-4.5zM4 8.5l8 4.5 8-4.5M12 13v9m-8-13 8 4.5 8-4.5" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/></svg><img class="shop-logo" alt="Shop logo" hidden></div><div><span id="authBrandName">ResourceHub</span><small style="display:block;color:#718096;font-size:10px;font-weight:600;letter-spacing:.08em;text-transform:uppercase">Vendor stock desk</small></div></div><h1 id="authTitle">Welcome back</h1><p id="authSubtitle">Sign in to manage shop items and stock.</p><div class="tabs auth-tabs"><button class="tab active" type="button" data-auth-mode="login">Sign in</button><button class="tab" type="button" id="registerTab" data-auth-mode="register">Create owner account</button></div><form id="authForm"><div class="field" id="nameField" hidden><label for="authName">Your name</label><input id="authName" autocomplete="name" maxlength="80"></div><div class="field"><label for="authUsername">Username</label><input id="authUsername" autocomplete="username" minlength="3" maxlength="40" required></div><div class="field"><label for="authPassword">Password</label><input id="authPassword" type="password" autocomplete="current-password" minlength="10" required></div><button class="btn full" type="submit" id="authSubmit">Sign in</button><div class="auth-error" id="authError" role="alert"></div></form><div class="subtle">Your accounts and inventory are stored in the configured data directory.</div></div></section>
  <div id="appScreen" hidden>
  <header class="topbar"><div class="brand"><div class="brand-mark" data-shop-logo><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 8.5 12 4l8 4.5v9L12 22l-8-4.5zM4 8.5l8 4.5 8-4.5M12 13v9m-8-13 8 4.5 8-4.5" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/></svg><img class="shop-logo" alt="Shop logo" hidden></div><div><span id="appBrandName">ResourceHub</span><small>Digital vendor stock desk</small></div></div><div class="top-right"><span class="live"></span><span class="status-text">Inventory system online</span><span class="account-name" id="accountName"></span><button class="logout" id="shopSettingsButton" hidden>Shop settings</button><button class="logout" id="staffButton" hidden>Manage staff</button><button class="logout" id="logoutButton">Sign out</button></div></header>
  <main>
    <section class="heading"><div><div class="eyebrow">Operations overview</div><h1>Vendor stock desk</h1><p>Track items in stock, disbursements, and what remains to help plan restocking.</p></div><div class="today" id="today"></div></section>
    <section class="stats" aria-label="Inventory summary"><article class="stat"><div class="stat-label">Total items</div><div class="stat-value" id="totalUnits">—</div><div class="stat-foot">units across all resources</div></article><article class="stat"><div class="stat-label">Remaining in stock</div><div class="stat-value" id="availableUnits">—</div><div class="stat-foot">units currently in stock</div></article><article class="stat"><div class="stat-label">Disbursed / on loan</div><div class="stat-value" id="borrowedUnits">—</div><div class="stat-foot">units currently disbursed or on loan</div></article><article class="stat"><div class="stat-label">Item types</div><div class="stat-value" id="resourceCount">—</div><div class="stat-foot">items in your catalog</div></article></section>
    <section class="layout"><div class="panel"><div class="panel-head"><div><h2>Shop inventory</h2><div class="subtle">Stocked, disbursed, remaining quantities, and product details</div></div><div class="actions"><input class="search" id="search" type="search" placeholder="Search items…" aria-label="Search items"><select class="category-filter" id="categoryFilter" aria-label="Filter by category"><option value="">All categories</option></select><button class="btn secondary" id="restockButton">＋ Stock in</button><button class="btn" id="addButton">＋ Add item</button></div></div><div class="table-wrap"><table><thead><tr><th>Product</th><th>Category</th><th>Stocked</th><th>Disbursed</th><th>Remaining</th><th>Unit price</th><th>Status</th><th></th></tr></thead><tbody id="resourceRows"></tbody></table></div></div>
      <aside class="side"><section class="panel form-panel"><h2>Quick transaction</h2><div class="subtle">Record a sale, loan, or return.</div><div class="tabs"><button class="tab active" data-mode="borrow">Borrow</button><button class="tab" data-mode="return">Return</button><button class="tab" data-mode="sale">Sale</button></div><form id="transactionForm"><div class="field" id="fellowField"><label for="fellow">Fellow</label><select id="fellow" required></select></div><div class="field" id="customerField" hidden><label for="customer">Customer (optional)</label><input id="customer" maxlength="100" placeholder="Customer name"></div><div class="field"><label for="resource">Shop item</label><select id="resource" required></select></div><div class="field"><label for="quantity">Quantity</label><input id="quantity" type="number" min="1" step="1" value="1" required></div><button class="btn full" id="transactionButton" type="submit">Record loan</button><div class="form-message" id="transactionMessage" role="status"></div></form></section>
      <section class="panel"><div class="panel-head"><div><h2>Currently on loan</h2><div class="subtle">Outstanding equipment by fellow</div></div></div><div class="loan-list" id="loanList"></div></section><section class="panel"><div class="panel-head"><div><h2>Recent sales</h2><div class="subtle">Latest recorded item disbursements</div></div></div><div class="loan-list" id="salesList"></div></section><section class="panel"><div class="panel-head"><div><h2>Restock watch</h2><div class="subtle">Items with fewer than 3 remaining</div></div></div><div class="watch-list" id="stockWatch"></div></section></aside>
    </section>
    <section class="panel" style="margin-top:20px"><div class="panel-head"><div><h2>Transaction history</h2><div class="subtle">Search sales, stock changes, loans, and returns</div></div><div class="actions"><input class="search" id="historySearch" type="search" placeholder="Search item or customer…" aria-label="Search transaction history"><select class="category-filter" id="historyType" aria-label="Filter transaction type"><option value="all">All transactions</option><option value="sale">Sales</option><option value="opening_stock">Opening stock</option><option value="restock">Stock-ins</option><option value="borrow">Loans</option><option value="return">Returns</option><option value="damaged">Damaged / lost</option></select></div></div><div class="table-wrap"><table><thead><tr><th>Date and time</th><th>Type</th><th>Item</th><th>Quantity</th><th>Customer / fellow</th></tr></thead><tbody id="historyRows"><tr><td colspan="5" class="empty">Loading transaction history…</td></tr></tbody></table></div><div class="form-message" id="historyMessage" role="status"></div></section>
    <section class="panel" id="reportsPanel" style="margin-top:20px"><div class="panel-head"><div><h2>Reports</h2><div class="subtle" id="reportPeriod">Sales, top items, and current stock value</div></div><div class="actions"><button class="btn secondary" id="exportReport" type="button">↓ Export CSV</button><button class="btn secondary" id="backupButton" type="button">↓ Download backup</button></div></div><div class="stats" style="margin:0 0 18px"><article class="stat"><div class="stat-label">Sales this month</div><div class="stat-value" id="monthRevenue">—</div><div class="stat-foot">recorded sales value</div></article><article class="stat"><div class="stat-label">Units sold this month</div><div class="stat-value" id="monthUnits">—</div><div class="stat-foot">items sold</div></article><article class="stat"><div class="stat-label">All-time sales</div><div class="stat-value" id="lifetimeRevenue">—</div><div class="stat-foot" id="lifetimeUnits">— units sold</div></article><article class="stat"><div class="stat-label">Stock value</div><div class="stat-value" id="stockValue">—</div><div class="stat-foot">remaining units × current unit price</div></article></div><div class="panel-head" style="margin-bottom:8px"><div><h2 style="font-size:15px">Best sellers</h2></div></div><div class="table-wrap"><table><thead><tr><th>Product</th><th>Units sold</th><th>Sales value</th></tr></thead><tbody id="bestSellerRows"><tr><td colspan="3" class="empty">Loading reports…</td></tr></tbody></table></div><div class="subtle" style="margin-top:10px">Revenue uses the price recorded when each sale was made. Sales recorded before price tracking was added may have no revenue value.</div><div class="form-message" id="reportMessage" role="status"></div></section>
    <div class="footnote"><span id="footerBrandName">ResourceHub</span> · Vendor stock management</div>
  </main><div class="toast" id="toast" role="status"></div>
  <dialog id="addDialog" style="border:0;border-radius:15px;padding:24px;width:min(440px,calc(100% - 28px));box-shadow:0 25px 80px #101b2d33"><form id="addForm"><h2 style="margin:0 0 5px">Add shop item</h2><p class="subtle" style="margin:0 0 16px">Create a product and add its starting stock.</p><div class="field"><label for="newId">Item ID</label><input id="newId" placeholder="e.g. P004" required></div><div class="field"><label for="newName">Product name</label><input id="newName" placeholder="e.g. Headphones" required></div><div class="field"><label for="newCategory">Category</label><input id="newCategory" placeholder="e.g. Electronics" required></div><div class="field"><label for="newTotal">Starting stock</label><input id="newTotal" type="number" min="1" step="1" value="1" required></div><div class="field"><label for="newSku">SKU / barcode</label><input id="newSku" maxlength="80"></div><div class="field"><label for="newPrice">Unit price</label><input id="newPrice" type="number" min="0" step="0.01" value="0" required></div><div class="field"><label for="newSupplier">Supplier</label><input id="newSupplier" maxlength="100"></div><div class="field"><label for="newThreshold">Restock alert at</label><input id="newThreshold" type="number" min="0" step="1" value="3" required></div><div style="display:flex;gap:9px;justify-content:flex-end;margin-top:18px"><button type="button" class="btn secondary" id="cancelAdd">Cancel</button><button class="btn" type="submit">Add item</button></div><div id="addMessage" class="form-message" role="status"></div></form></dialog>
  <dialog id="detailsDialog" style="border:0;border-radius:15px;padding:24px;width:min(440px,calc(100% - 28px));box-shadow:0 25px 80px #101b2d33"><form id="detailsForm"><h2 style="margin:0 0 5px">Product details</h2><p class="subtle" id="detailsItemName" style="margin:0 0 16px"></p><input id="detailsId" type="hidden"><div class="field"><label for="detailsSku">SKU / barcode</label><input id="detailsSku" maxlength="80"></div><div class="field"><label for="detailsPrice">Unit price</label><input id="detailsPrice" type="number" min="0" step="0.01" required></div><div class="field"><label for="detailsSupplier">Supplier</label><input id="detailsSupplier" maxlength="100"></div><div class="field"><label for="detailsThreshold">Restock alert at</label><input id="detailsThreshold" type="number" min="0" step="1" required></div><div style="display:flex;gap:9px;justify-content:flex-end;margin-top:18px"><button type="button" class="btn secondary" id="cancelDetails">Cancel</button><button class="btn" type="submit">Save details</button></div><div id="detailsMessage" class="form-message" role="status"></div></form></dialog>
  <dialog id="restockDialog" style="border:0;border-radius:15px;padding:24px;width:min(440px,calc(100% - 28px));box-shadow:0 25px 80px #101b2d33"><form id="restockForm"><h2 style="margin:0 0 5px">Stock in items</h2><p class="subtle" style="margin:0 0 16px">Record newly received units and update remaining stock.</p><div class="field"><label for="restockItem">Shop item</label><select id="restockItem" required></select></div><div class="field"><label for="restockQuantity">Quantity received</label><input id="restockQuantity" type="number" min="1" step="1" value="1" required></div><div style="display:flex;gap:9px;justify-content:flex-end;margin-top:18px"><button type="button" class="btn secondary" id="cancelRestock">Cancel</button><button class="btn" type="submit">Update stock</button></div><div id="restockMessage" class="form-message" role="status"></div></form></dialog>
  <dialog id="staffDialog" style="border:0;border-radius:15px;padding:24px;width:min(500px,calc(100% - 28px));box-shadow:0 25px 80px #101b2d33"><h2 style="margin:0 0 5px">Staff accounts</h2><p class="subtle" style="margin:0 0 16px">Create staff logins for sales, loans, and returns.</p><div class="table-wrap"><table><thead><tr><th>Name</th><th>Username</th><th>Role</th><th>Manage</th></tr></thead><tbody id="staffRows"></tbody></table></div><form id="staffForm" style="margin-top:22px"><h3 style="font-size:14px">Add staff member</h3><div class="field"><label for="staffName">Name</label><input id="staffName" maxlength="80" required></div><div class="field"><label for="staffUsername">Username</label><input id="staffUsername" minlength="3" maxlength="40" required></div><div class="field"><label for="staffPassword">Initial password</label><input id="staffPassword" type="password" minlength="10" autocomplete="new-password" required></div><div style="display:flex;gap:9px;justify-content:flex-end;margin-top:18px"><button type="button" class="btn secondary" id="closeStaff">Close</button><button class="btn" type="submit">Create staff account</button></div><div id="staffMessage" class="form-message" role="status"></div></form></dialog>
  <dialog id="shopSettingsDialog" style="border:0;border-radius:15px;padding:24px;width:min(500px,calc(100% - 28px));box-shadow:0 25px 80px #101b2d33"><form id="shopSettingsForm"><h2 style="margin:0 0 5px">Shop settings</h2><p class="subtle" style="margin:0 0 16px">Set the identity customers and staff see throughout this site.</p><div class="field"><label for="shopName">Shop name</label><input id="shopName" maxlength="60" required></div><div class="field"><label for="shopLogo">Logo image (PNG, JPEG, or WebP · max 250 KB)</label><input id="shopLogo" type="file" accept="image/png,image/jpeg,image/webp"><div style="display:flex;align-items:center;gap:12px;margin-top:10px"><img id="shopLogoPreview" alt="Logo preview" hidden style="width:48px;height:48px;object-fit:contain;border:1px solid var(--line);border-radius:10px;padding:3px"><button type="button" class="btn secondary" id="removeShopLogo">Remove logo</button></div></div><div class="field"><label for="shopColor">Brand color</label><div style="display:flex;align-items:center;gap:10px"><input id="shopColor" type="color" style="width:54px;height:42px;padding:4px"><span class="subtle">Used for buttons, highlights, and the logo tile.</span></div></div><div class="field"><label for="shopCurrency">Currency code (ISO 4217)</label><input id="shopCurrency" maxlength="3" minlength="3" pattern="[A-Za-z]{3}" placeholder="NGN" required></div><div style="display:flex;gap:9px;justify-content:flex-end;margin-top:18px"><button type="button" class="btn secondary" id="cancelShopSettings">Cancel</button><button class="btn" type="submit">Save shop settings</button></div><div id="shopSettingsMessage" class="form-message" role="status"></div></form></dialog>
  <dialog id="confirmDialog" style="border:0;border-radius:15px;padding:24px;width:min(420px,calc(100% - 28px));box-shadow:0 25px 80px #101b2d33"><h2 id="confirmTitle" style="margin:0 0 6px">Confirm inventory change</h2><p id="confirmDetail" class="subtle" style="margin:0 0 20px"></p><div style="display:flex;gap:9px;justify-content:flex-end"><button type="button" class="btn secondary" id="confirmCancel">Cancel</button><button type="button" class="btn" id="confirmOk">Confirm</button></div></dialog>
  <script>
    const $ = selector => document.querySelector(selector);
    let state = null, mode = 'borrow', currentRole = 'staff', registrationOpen = false, toastTimer, historyTimer, pendingLogo = null, turnstileSiteKey = '';
    $('#today').textContent = new Intl.DateTimeFormat(undefined,{weekday:'long',month:'long',day:'numeric',year:'numeric'}).format(new Date());
    function esc(value){return String(value).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
    async function api(path, body){const response=await fetch(path,{method:body?'POST':'GET',headers:body?{'Content-Type':'application/json'}:{},body:body?JSON.stringify(body):undefined});const data=await response.json();if(!response.ok)throw new Error(data.error||'Something went wrong.');return data}
    function money(value){const currency=state?.settings?.currency||'NGN';try{return new Intl.NumberFormat(undefined,{style:'currency',currency}).format(Number(value)||0)}catch(error){return Number(value||0).toFixed(2)+' '+currency}}
    function applyShopSettings(settings){if(!settings)return;const name=settings.shop_name||'ResourceHub',color=settings.brand_color||'#536dfe';document.documentElement.style.setProperty('--blue',color);document.title=`${name} · Vendor stock desk`;['landingBrandName','landingCtaBrand','authBrandName','appBrandName','footerBrandName'].forEach(id=>{const element=$('#'+id);if(element)element.textContent=name});document.querySelectorAll('[data-shop-logo]').forEach(mark=>{const image=mark.querySelector('.shop-logo'),symbol=mark.querySelector('svg');mark.style.backgroundColor=settings.logo_data?'transparent':color;image.hidden=!settings.logo_data;symbol.hidden=!!settings.logo_data;if(settings.logo_data)image.src=settings.logo_data;else image.removeAttribute('src')})}
    function confirmAction(title,detail,buttonLabel='Confirm'){const dialog=$('#confirmDialog');$('#confirmTitle').textContent=title;$('#confirmDetail').textContent=detail;$('#confirmOk').textContent=buttonLabel;return new Promise(resolve=>{let accepted=false;const closed=()=>{dialog.removeEventListener('close',closed);resolve(accepted)};$('#confirmCancel').onclick=()=>{accepted=false;dialog.close()};$('#confirmOk').onclick=()=>{accepted=true;dialog.close()};dialog.addEventListener('close',closed,{once:true});dialog.showModal()})}
    function showToast(message){const box=$('#toast');box.textContent=message;box.classList.add('show');clearTimeout(toastTimer);toastTimer=setTimeout(()=>box.classList.remove('show'),2800)}
    function render(){if(!state)return;applyShopSettings(state.settings);const resources=state.resources,filter=$('#search').value.trim().toLowerCase(),categoryControl=$('#categoryFilter'),oldCategory=categoryControl.value,categories=[...new Set(resources.map(r=>r.category))].sort((a,b)=>a.localeCompare(b));categoryControl.innerHTML='<option value="">All categories</option>'+categories.map(name=>`<option value="${esc(name)}">${esc(name)}</option>`).join('');if(categories.includes(oldCategory))categoryControl.value=oldCategory;const category=categoryControl.value.toLowerCase();const shown=resources.filter(r=>(r.name+' '+r.category+' '+r.id).toLowerCase().includes(filter)&&(!category||r.category.toLowerCase()===category));
      $('#totalUnits').textContent=state.summary.total;$('#availableUnits').textContent=state.summary.available;$('#borrowedUnits').textContent=state.summary.borrowed;$('#resourceCount').textContent=resources.length;
      $('#resourceRows').innerHTML=shown.length?shown.map(r=>{const status=r.available===0?['Out of stock','out']:r.available<=r.reorder_level?['Restock soon','low']:['In stock',''];return `<tr><td><div class="resource-name">${esc(r.name)}</div><div class="resource-id">${esc(r.id)}${r.sku?' · '+esc(r.sku):''}</div></td><td>${esc(r.category)}</td><td>${r.total}</td><td>${r.total-r.available}</td><td><strong>${r.available}</strong></td><td>${money(r.price)}</td><td><span class="badge ${status[1]}">${status[0]}</span></td><td>${currentRole==='owner'?`<button class="btn secondary edit-details" data-id="${esc(r.id)}" type="button">Details</button> <button class="btn secondary delete-resource" data-id="${esc(r.id)}" type="button">Delete</button>`:''}</td></tr>`}).join(''):`<tr><td colspan="8" class="empty">${resources.length?'No items match your search.':'No items have been added yet.'}</td></tr>`;
      const lowStock=resources.filter(r=>r.available<=r.reorder_level);$('#stockWatch').innerHTML=lowStock.length?lowStock.map(r=>`<div class="watch-row"><div><div class="watch-name">${esc(r.name)}</div><div class="watch-detail">${esc(r.id)} · alert at ${r.reorder_level}</div></div><span class="watch-count ${r.available===0?'out':''}">${r.available} remaining</span></div>`).join(''):'<div class="empty" style="padding:14px">All items are above their restock thresholds.</div>';
      const fellowSelect=$('#fellow'), previousFellow=fellowSelect.value;fellowSelect.innerHTML=state.fellows.map(f=>`<option value="${esc(f.id)}">${esc(f.name)}</option>`).join('');if(state.fellows.some(f=>f.id===previousFellow))fellowSelect.value=previousFellow;
      const select=$('#resource'), previous=select.value, choices=mode==='return'?state.loans.filter(l=>l.fellow_id===fellowSelect.value):resources.filter(r=>r.available>0);select.innerHTML=choices.map(r=>{const id=r.id||r.resource_id;const item=resources.find(x=>x.id===id);const count=mode==='return'?r.quantity+' on loan':r.available+' remaining';return `<option value="${esc(id)}">${esc(item?.name||r.name)} (${count})</option>`}).join('');if(choices.some(r=>(r.id||r.resource_id)===previous))select.value=previous;
      $('#fellowField').hidden=mode==='sale';$('#fellow').required=mode!=='sale';$('#customerField').hidden=mode!=='sale';$('#resource').disabled=!choices.length;$('#transactionButton').disabled=!choices.length;$('#transactionButton').textContent=mode==='borrow'?'Record loan':mode==='return'?'Record return':'Record sale';
      const loans=state.loans;$('#loanList').innerHTML=loans.length?loans.map(l=>`<div class="loan-row"><div><div class="loan-who">${esc(l.fellow_name)}</div><div class="loan-what">${esc(l.resource_name)}</div></div><div class="loan-count">${l.quantity} unit${l.quantity===1?'':'s'}</div></div>`).join(''):'<div class="empty" style="padding:15px 0">Nothing is currently on loan.</div>';
      $('#salesList').innerHTML=state.sales.length?state.sales.map(s=>`<div class="loan-row"><div><div class="loan-who">${esc(s.resource_name)} × ${s.quantity}</div><div class="loan-what">${esc(s.customer||'Walk-in customer')} · ${esc(new Date(s.created_at).toLocaleString())}</div></div><div class="loan-count">Sale</div></div>`).join(''):'<div class="empty" style="padding:15px 0">No sales recorded yet.</div>';
    }
    async function refresh(){state=await api('/api/state');render();const loads=[loadHistory()];if(currentRole==='owner')loads.push(loadReports());await Promise.all(loads)}
    async function loadHistory(){const query=new URLSearchParams({q:$('#historySearch').value,action:$('#historyType').value});try{const result=await api('/api/history?'+query.toString());$('#historyMessage').textContent='';const labels={sale:'Sale',opening_stock:'Opening stock',restock:'Stock-in',borrow:'Loan',return:'Return',damaged:'Damaged / lost'};$('#historyRows').innerHTML=result.history.length?result.history.map(row=>`<tr><td>${esc(new Date(row.created_at).toLocaleString())}</td><td>${esc(labels[row.action]||row.action)}</td><td><div class="resource-name">${esc(row.item)}</div><div class="resource-id">${esc(row.resource_id)}</div></td><td>${row.quantity}</td><td>${esc(row.party||'—')}</td></tr>`).join(''):'<tr><td colspan="5" class="empty">No transactions match your search.</td></tr>'}catch(error){$('#historyMessage').className='form-message error';$('#historyMessage').textContent=error.message}}
    function scheduleHistory(){clearTimeout(historyTimer);historyTimer=setTimeout(loadHistory,180)}
    $('#historySearch').addEventListener('input',scheduleHistory);$('#historyType').addEventListener('change',loadHistory);
    async function loadReports(){try{const report=await api('/api/reports');$('#reportMessage').textContent='';$('#reportPeriod').textContent=`Sales, best sellers, and stock value · ${report.period}`;$('#monthRevenue').textContent=money(report.month_revenue);$('#monthUnits').textContent=report.month_units;$('#lifetimeRevenue').textContent=money(report.lifetime_revenue);$('#lifetimeUnits').textContent=`${report.lifetime_units} units sold`;$('#stockValue').textContent=money(report.stock_value);$('#bestSellerRows').innerHTML=report.best_sellers.length?report.best_sellers.map(item=>`<tr><td><div class="resource-name">${esc(item.name)}</div><div class="resource-id">${esc(item.id)}</div></td><td>${item.units}</td><td>${money(item.revenue)}</td></tr>`).join(''):'<tr><td colspan="3" class="empty">No sales recorded yet.</td></tr>'}catch(error){$('#reportMessage').className='form-message error';$('#reportMessage').textContent=error.message}}
    $('#exportReport').addEventListener('click',async()=>{try{const response=await fetch('/api/reports/export.csv');if(!response.ok){const data=await response.json();throw new Error(data.error||'Could not export report.')}const file=URL.createObjectURL(await response.blob()),link=document.createElement('a');link.href=file;link.download='resourcehub-report.csv';link.click();URL.revokeObjectURL(file)}catch(error){$('#reportMessage').className='form-message error';$('#reportMessage').textContent=error.message}});
    $('#backupButton').addEventListener('click',async()=>{try{const response=await fetch('/api/backup');if(!response.ok){const data=await response.json();throw new Error(data.error||'Could not download the backup.')}const file=URL.createObjectURL(await response.blob()),link=document.createElement('a');link.href=file;link.download='resourcehub-backup.zip';link.click();setTimeout(()=>URL.revokeObjectURL(file),1000)}catch(error){$('#reportMessage').className='form-message error';$('#reportMessage').textContent=error.message}});
    $('#search').addEventListener('input',render);$('#categoryFilter').addEventListener('change',render);document.querySelectorAll('.tab[data-mode]').forEach(tab=>tab.addEventListener('click',()=>{mode=tab.dataset.mode;document.querySelectorAll('.tab[data-mode]').forEach(t=>t.classList.toggle('active',t===tab));$('#transactionMessage').textContent='';render()}));$('#fellow').addEventListener('change',render);
    $('#resourceRows').addEventListener('click',async event=>{const button=event.target.closest('.delete-resource');if(!button)return;const item=state.resources.find(r=>r.id===button.dataset.id);if(!item)return;try{const accepted=await confirmAction('Delete product?',`Delete ${item.name} (${item.id})? Products with loan or sales history cannot be deleted.`,'Delete product');if(!accepted)return;const result=await api('/api/delete-resource',{resource_id:item.id});await refresh();showToast(result.message)}catch(error){showToast(error.message)}});
    $('#resourceRows').addEventListener('click',event=>{const button=event.target.closest('.edit-details');if(!button)return;const item=state.resources.find(r=>r.id===button.dataset.id);if(!item)return;$('#detailsId').value=item.id;$('#detailsItemName').textContent=`${item.name} · ${item.id}`;$('#detailsSku').value=item.sku||'';$('#detailsPrice').value=Number(item.price).toFixed(2);$('#detailsSupplier').value=item.supplier||'';$('#detailsThreshold').value=item.reorder_level;$('#detailsMessage').textContent='';$('#detailsDialog').showModal()});$('#cancelDetails').addEventListener('click',()=>$('#detailsDialog').close());$('#detailsForm').addEventListener('submit',async event=>{event.preventDefault();const message=$('#detailsMessage');message.className='form-message';message.textContent='';try{const result=await api('/api/resource-details',{resource_id:$('#detailsId').value,sku:$('#detailsSku').value,price:Number($('#detailsPrice').value),supplier:$('#detailsSupplier').value,reorder_level:Number($('#detailsThreshold').value)});$('#detailsDialog').close();await refresh();showToast(result.message)}catch(error){message.classList.add('error');message.textContent=error.message}});
    $('#transactionForm').addEventListener('submit',async event=>{event.preventDefault();const message=$('#transactionMessage');message.className='form-message';message.textContent='';try{const payload={resource_id:$('#resource').value,quantity:Number($('#quantity').value)};if(mode==='sale'){payload.customer=$('#customer').value;const item=state.resources.find(r=>r.id===payload.resource_id);if(!item)return;const amount=(Number(item.price)||0)*payload.quantity;const accepted=await confirmAction('Confirm sale?',`Sell ${payload.quantity} × ${item.name}? Remaining stock will decrease from ${item.available} to ${item.available-payload.quantity}. Sale value: ${money(amount)}.`,'Record sale');if(!accepted)return}else payload.fellow_id=$('#fellow').value;const result=await api('/api/'+mode,payload);message.textContent=result.message;await refresh();if(mode==='sale')$('#customer').value='';showToast(result.message)}catch(error){message.classList.add('error');message.textContent=error.message}});
    $('#restockButton').addEventListener('click',()=>{if(!state.resources.length){showToast('Add an item before stocking in.');return}$('#restockItem').innerHTML=state.resources.map(r=>`<option value="${esc(r.id)}">${esc(r.name)} (${esc(r.id)} · ${r.available} remaining)</option>`).join('');$('#restockMessage').textContent='';$('#restockDialog').showModal()});$('#cancelRestock').addEventListener('click',()=>$('#restockDialog').close());$('#restockForm').addEventListener('submit',async event=>{event.preventDefault();const message=$('#restockMessage');message.className='form-message';message.textContent='';try{const id=$('#restockItem').value,quantity=Number($('#restockQuantity').value),item=state.resources.find(r=>r.id===id);if(!item)return;const accepted=await confirmAction('Confirm stock-in?',`Add ${quantity} unit${quantity===1?'':'s'} of ${item.name}. Remaining stock will increase from ${item.available} to ${item.available+quantity}.`,'Update stock');if(!accepted)return;const result=await api('/api/restock',{resource_id:id,quantity});$('#restockDialog').close();event.target.reset();$('#restockQuantity').value='1';await refresh();showToast(result.message)}catch(error){message.classList.add('error');message.textContent=error.message}});$('#addButton').addEventListener('click',()=>{$('#addMessage').textContent='';$('#addDialog').showModal()});$('#cancelAdd').addEventListener('click',()=>$('#addDialog').close());$('#addForm').addEventListener('submit',async event=>{event.preventDefault();const message=$('#addMessage');message.className='form-message';message.textContent='';try{const result=await api('/api/resources',{id:$('#newId').value,name:$('#newName').value,category:$('#newCategory').value,total:Number($('#newTotal').value),sku:$('#newSku').value,price:Number($('#newPrice').value),supplier:$('#newSupplier').value,reorder_level:Number($('#newThreshold').value)});$('#addDialog').close();event.target.reset();$('#newTotal').value='1';$('#newPrice').value='0';$('#newThreshold').value='3';await refresh();showToast(result.message)}catch(error){message.classList.add('error');message.textContent=error.message}});
    let authMode='login';
    function renderTurnstileWidget(){const target=$('#turnstileContainer');if(target&&turnstileSiteKey&&window.turnstile&&!target.dataset.widget){window.turnstile.render(target,{sitekey:turnstileSiteKey});target.dataset.widget='1'}}
    function setAuthMode(next){if(next==='register'&&!registrationOpen)next='login';authMode=next;const registering=next==='register';if(registering&&!$('#registrationFields'))$('#nameField').insertAdjacentHTML('afterend','<div id="registrationFields"><div class="field"><label for="companyName">Company name</label><input id="companyName" maxlength="100" required></div><div class="field"><label for="companyProducts">What do you sell?</label><input id="companyProducts" maxlength="120" placeholder="Products or services"></div><div class="field"><label for="companyPhone">Company phone</label><input id="companyPhone" autocomplete="tel" maxlength="24" required></div><div class="field"><label for="companyEmail">Company email</label><input id="companyEmail" type="email" autocomplete="email" maxlength="254" required></div><div class="field" style="display:flex;gap:8px"><input id="emailCode" inputmode="numeric" maxlength="6" placeholder="Email code" required><button class="btn secondary" id="sendEmailCode" type="button">Send code</button></div><div id="turnstileContainer"></div></div>');if($('#registrationFields')){$('#registrationFields').hidden=!registering;$('#registrationFields').querySelectorAll('input').forEach(input=>input.required=registering)}renderTurnstileWidget();$('#authTitle').textContent=registering?'Register your company':'Welcome back';$('#authSubtitle').textContent=registering?'Create a shop account. Verify your email and complete the bot check.':'Sign in to manage your shop and staff.';if(registering&&!turnstileSiteKey)$('#authSubtitle').textContent='Signup setup is incomplete: the site owner must configure Cloudflare Turnstile.';$('#nameField').hidden=!registering;$('#authName').required=registering;$('#authPassword').autocomplete=registering?'new-password':'current-password';$('#authSubmit').textContent=registering?'Create shop account':'Sign in';$('#registerTab').hidden=!registrationOpen;document.querySelectorAll('[data-auth-mode]').forEach(tab=>tab.classList.toggle('active',tab.dataset.authMode===next));$('#authError').textContent=''}
    function showApp(name,role){currentRole=role||'staff';$('#accountName').textContent=`${name} · ${currentRole}`;$('#staffButton').hidden=currentRole!=='owner';$('#shopSettingsButton').hidden=currentRole!=='owner';$('#restockButton').hidden=currentRole!=='owner';$('#addButton').hidden=currentRole!=='owner';$('#exportReport').hidden=currentRole!=='owner';$('#backupButton').hidden=currentRole!=='owner';$('#reportsPanel').hidden=currentRole!=='owner';$('#landingScreen').hidden=true;$('#authScreen').hidden=true;$('#appScreen').hidden=false;refresh().catch(error=>showToast(error.message))}
    document.querySelectorAll('[data-open-auth]').forEach(button=>button.addEventListener('click',()=>{$('#landingScreen').hidden=true;$('#authScreen').hidden=false;setAuthMode('login')}));
    $('#backLanding').addEventListener('click',()=>{$('#authScreen').hidden=true;$('#landingScreen').hidden=false});
    document.querySelectorAll('[data-auth-mode]').forEach(tab=>tab.addEventListener('click',()=>setAuthMode(tab.dataset.authMode)));
    $('#authForm').addEventListener('submit',async event=>{event.preventDefault();const errorBox=$('#authError');errorBox.textContent='';const payload={username:$('#authUsername').value,password:$('#authPassword').value};if(authMode==='register'){payload.name=$('#authName').value;payload.company_name=$('#companyName').value;payload.products=$('#companyProducts').value;payload.phone=$('#companyPhone').value;payload.email=$('#companyEmail').value;payload.email_code=$('#emailCode').value;payload.turnstile_token=document.querySelector('[name="cf-turnstile-response"]')?.value||''}try{const result=await api('/api/'+(authMode==='register'?'register':'login'),payload);event.target.reset();showApp(result.name,result.role);registrationOpen=true}catch(error){errorBox.textContent=error.message}});
    $('#authForm').addEventListener('click',async event=>{if(event.target.id!=='sendEmailCode')return;const errorBox=$('#authError');errorBox.textContent='';try{const result=await api('/api/send-verification',{email:$('#companyEmail').value});errorBox.textContent=result.message;errorBox.style.color='var(--green)'}catch(error){errorBox.textContent=error.message;errorBox.style.color='var(--red)'}});
    $('#shopSettingsButton').addEventListener('click',()=>{const settings=state.settings;$('#shopName').value=settings.shop_name;$('#shopColor').value=settings.brand_color;$('#shopCurrency').value=settings.currency;$('#shopLogo').value='';pendingLogo=null;$('#shopLogoPreview').src=settings.logo_data||'';$('#shopLogoPreview').hidden=!settings.logo_data;$('#shopSettingsMessage').textContent='';$('#shopSettingsDialog').showModal()});$('#cancelShopSettings').addEventListener('click',()=>$('#shopSettingsDialog').close());$('#shopLogo').addEventListener('change',()=>{const file=$('#shopLogo').files[0];if(!file)return;if(file.size>250000){$('#shopSettingsMessage').className='form-message error';$('#shopSettingsMessage').textContent='Choose a logo no larger than 250 KB.';$('#shopLogo').value='';return}const reader=new FileReader();reader.onload=()=>{pendingLogo=reader.result;$('#shopLogoPreview').src=pendingLogo;$('#shopLogoPreview').hidden=false;$('#shopSettingsMessage').textContent=''};reader.onerror=()=>{$('#shopSettingsMessage').className='form-message error';$('#shopSettingsMessage').textContent='Could not read this image.'};reader.readAsDataURL(file)});$('#removeShopLogo').addEventListener('click',()=>{pendingLogo='';$('#shopLogo').value='';$('#shopLogoPreview').removeAttribute('src');$('#shopLogoPreview').hidden=true});$('#shopSettingsForm').addEventListener('submit',async event=>{event.preventDefault();const message=$('#shopSettingsMessage');message.className='form-message';message.textContent='';try{const result=await api('/api/settings',{shop_name:$('#shopName').value,brand_color:$('#shopColor').value,currency:$('#shopCurrency').value,logo_data:pendingLogo===null?state.settings.logo_data:pendingLogo});pendingLogo=null;$('#shopSettingsDialog').close();await refresh();showToast(result.message)}catch(error){message.classList.add('error');message.textContent=error.message}});$('#staffButton').addEventListener('click',async()=>{try{await loadStaff();$('#staffMessage').textContent='';$('#staffDialog').showModal()}catch(error){showToast(error.message)}});$('#closeStaff').addEventListener('click',()=>$('#staffDialog').close());$('#staffRows').addEventListener('click',async event=>{const button=event.target.closest('.remove-staff');if(!button)return;if(!window.confirm(`Remove staff account ${button.dataset.username}?`))return;try{const result=await api('/api/staff/delete',{username:button.dataset.username});await loadStaff();$('#staffMessage').textContent=result.message}catch(error){$('#staffMessage').className='form-message error';$('#staffMessage').textContent=error.message}});async function loadStaff(){const result=await api('/api/staff');$('#staffRows').innerHTML=result.staff.length?result.staff.map(person=>`<tr><td>${esc(person.name)}</td><td>${esc(person.username)}</td><td>Staff</td><td><button class="btn secondary remove-staff" data-username="${esc(person.username)}" type="button">Remove</button></td></tr>`).join(''):'<tr><td colspan="4" class="empty">No staff accounts yet.</td></tr>'}$('#staffForm').addEventListener('submit',async event=>{event.preventDefault();const message=$('#staffMessage');message.className='form-message';message.textContent='';try{const result=await api('/api/staff',{name:$('#staffName').value,username:$('#staffUsername').value,password:$('#staffPassword').value});event.target.reset();await loadStaff();message.textContent=result.message}catch(error){message.classList.add('error');message.textContent=error.message}});$('#logoutButton').addEventListener('click',async()=>{try{await api('/api/logout',{});$('#appScreen').hidden=true;$('#authScreen').hidden=true;$('#landingScreen').hidden=false;$('#authPassword').value='';$('#staffButton').hidden=true;$('#shopSettingsButton').hidden=true;$('#registerTab').hidden=!registrationOpen;setAuthMode('login')}catch(error){showToast(error.message)}});
    api('/api/me').then(me=>{registrationOpen=me.registration_open;turnstileSiteKey=me.turnstile_site_key||'';applyShopSettings(me.settings);$('#registerTab').hidden=!registrationOpen;if(turnstileSiteKey){const script=document.createElement('script');script.src='https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit';script.async=true;script.defer=true;document.head.appendChild(script);script.onload=renderTurnstileWidget}if(me.authenticated)showApp(me.name,me.role);else setAuthMode('login')}).catch(error=>showToast(error.message));setInterval(()=>{if(!$('#appScreen').hidden)refresh().catch(()=>{})},30000);
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
    return {
        "resources": resources,
        "fellows": [{"id": key, "name": name} for key, name in inventory.fellows.items()],
        "loans": loans,
        "sales": recent_sales,
        "settings": inventory.get_shop_settings(),
        "summary": {"total": total, "available": available, "borrowed": total - available},
    }


def transaction_history(search_text="", action="all"):
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
              SELECT l.type,l.resource_id,r.name,l.quantity,f.name,l.created_at
                FROM loans l JOIN resources r ON r.id=l.resource_id JOIN fellows f ON f.id=l.fellow_id
            ) AS event ORDER BY event.created_at DESC""")]
    term = search_text.strip().casefold()
    if action != "all":
        rows = [row for row in rows if row["action"] == action]
    if term:
        rows = [row for row in rows if term in " ".join(str(row[key] or "") for key in
                ("action", "resource_id", "item", "quantity", "party")).casefold()]
    return rows[:500]


def reports_summary():
    inventory.refresh_state()
    with inventory.connect() as db:
        monthly = db.execute("""SELECT COALESCE(SUM(quantity),0) AS units,
            COALESCE(SUM(quantity*unit_price),0) AS revenue FROM sales
            WHERE substr(created_at,1,7)=?""", (date.today().strftime("%Y-%m"),)).fetchone()
        lifetime = db.execute("""SELECT COALESCE(SUM(quantity),0) AS units,
            COALESCE(SUM(quantity*unit_price),0) AS revenue FROM sales""").fetchone()
        best_sellers = [dict(row) for row in db.execute("""SELECT r.id,r.name,
            SUM(s.quantity) AS units,SUM(s.quantity*s.unit_price) AS revenue
            FROM sales s JOIN resources r ON r.id=s.resource_id
            GROUP BY r.id,r.name ORDER BY units DESC,revenue DESC LIMIT 5""")]
        inventory_rows = [dict(row) for row in db.execute("SELECT id,name,category,sku,supplier,price FROM resources ORDER BY name COLLATE NOCASE")]
    stock_value = sum(item["available"] * item["price"] for item in inventory.resources)
    return {
        "period": date.today().strftime("%B %Y"),
        "month_units": monthly["units"], "month_revenue": monthly["revenue"],
        "lifetime_units": lifetime["units"], "lifetime_revenue": lifetime["revenue"],
        "stock_value": stock_value, "best_sellers": best_sellers,
        "inventory": [{**item, "available": next(r["available"] for r in inventory.resources if r["id"] == item["id"])} for item in inventory_rows],
    }


def reports_csv():
    report = reports_summary()
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    safe = lambda value: "'" + value if isinstance(value, str) and value.startswith(("=", "+", "-", "@", "\t", "\r")) else value
    writer.writerow(["section", "item_id", "item", "category", "sku", "quantity", "unit_price", "value", "details"])
    writer.writerow(["sales_summary", "", "", "", "", report["month_units"], "", f"{report['month_revenue']:.2f}", f"Month to date: {report['period']}"])
    writer.writerow(["sales_lifetime", "", "", "", "", report["lifetime_units"], "", f"{report['lifetime_revenue']:.2f}", "All recorded sales"])
    writer.writerow(["stock_summary", "", "", "", "", "", "", f"{report['stock_value']:.2f}", "Current stock value"])
    for item in report["inventory"]:
        writer.writerow(["inventory", safe(item["id"]), safe(item["name"]), safe(item["category"]), safe(item["sku"]),
                         item["available"], f"{item['price']:.2f}", f"{item['available']*item['price']:.2f}", safe(item["supplier"])])
    for item in report["best_sellers"]:
        writer.writerow(["best_seller", safe(item["id"]), safe(item["name"]), "", "", item["units"], "", f"{item['revenue']:.2f}", "All recorded sales"])
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
            data = json.loads(self.rfile.read(size))
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
        elif path == "/api/history":
            with LOCK:
                if not self.current_user():
                    self.respond(401, {"error": "Please sign in to continue."})
                    return
                query = parse_qs(urlparse(self.path).query)
                search_text = query.get("q", [""])[0][:120]
                action = query.get("action", ["all"])[0]
                if action not in ("all", "sale", "opening_stock", "restock", "borrow", "return", "damaged"):
                    self.respond(400, {"error": "Choose a valid transaction type."})
                    return
                self.respond(200, {"history": transaction_history(search_text, action)})
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
                elif path.endswith(".csv"):
                    self.respond(200, reports_csv(), "text/csv; charset=utf-8",
                                 headers={"Content-Disposition": "attachment; filename=resourcehub-report.csv"})
                else:
                    self.respond(200, reports_summary())
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

    def current_user(self):
        """Return the active username and extend its inactivity timeout."""
        from http.cookies import SimpleCookie
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
            token = cookie[SESSION_COOKIE].value
        except Exception:
            return None
        session = sessions.get(token)
        now = time.time()
        if not session or session["expires"] < now:
            sessions.pop(token, None)
            return None
        session["expires"] = now + SESSION_TTL
        activate_shop(session["shop_id"])
        return session["username"]

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

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/send-verification":
            self.handle_email_verification()
            return
        if path in ("/api/register", "/api/login", "/api/logout"):
            self.handle_auth(path)
            return
        if path == "/api/staff":
            self.handle_staff_creation()
            return
        if path == "/api/staff/delete":
            self.handle_staff_deletion()
            return
        if path == "/api/settings":
            self.handle_shop_settings()
            return
        if path not in ("/api/resources", "/api/resource-details", "/api/delete-resource", "/api/restock", "/api/sale", "/api/borrow", "/api/return"):
            self.respond(404, {"error": "Action not found."})
            return
        try:
            data = self.read_json()
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
            elif path == "/api/sale":
                if not isinstance(data.get("resource_id"), str):
                    raise ValueError("Item ID must be text.")
                if type(data.get("quantity")) is not int:
                    raise ValueError("Quantity must be a whole number.")
                if not isinstance(data.get("customer", ""), str):
                    raise ValueError("Customer name must be text.")
            else:
                if any(not isinstance(data.get(key), str) for key in ("fellow_id", "resource_id")):
                    raise ValueError("Fellow and equipment IDs must be text.")
                if type(data.get("quantity")) is not int:
                    raise ValueError("Quantity must be a whole number.")
            with LOCK:
                if not self.current_user():
                    self.respond(401, {"error": "Your session expired. Please sign in again."})
                    return
                username = self.current_user()
                if path in ("/api/resources", "/api/resource-details", "/api/delete-resource", "/api/restock") and users[username]["role"] != "owner":
                    self.respond(403, {"error": "Only the owner can manage products or stock."})
                    return
                before_resources = deepcopy(inventory.resources)
                before_records = deepcopy(inventory.borrow_records)
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
                elif path == "/api/sale":
                    success, message = inventory.record_sale(
                        data.get("resource_id", ""), data.get("quantity"), data.get("customer", ""),
                    )
                elif path == "/api/borrow":
                    success, message = inventory.borrow(
                        data.get("fellow_id", ""), data.get("resource_id", ""), data.get("quantity"),
                    )
                else:
                    success, message = inventory.return_item(
                        data.get("fellow_id", ""), data.get("resource_id", ""), data.get("quantity"),
                    )
                if not success:
                    self.respond(400, {"error": message})
                    return
                try:
                    inventory.save_data()
                except (OSError, TypeError, ValueError) as error:
                    inventory.resources[:] = before_resources
                    inventory.borrow_records[:] = before_records
                    self.respond(500, {"error": f"Could not save this change: {error}"})
                    return
            self.respond(200, {"message": message})
        except ValueError as error:
            self.respond(400, {"error": str(error)})
        except (OSError, sqlite3.Error):
            self.respond(500, {"error": "Could not create a safety backup, so no change was recorded."})

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
            ip = self.client_address[0]
            with LOCK:
                previous = email_challenges.get(email)
                if ((previous and previous.get("sent", 0) > time.time() - 60)
                        or email_ip_sends.get(ip, 0) > time.time() - 60):
                    self.respond(429, {"error": "Wait one minute before requesting another code."})
                    return
                send_email_code(email)
                email_challenges[email]["sent"] = time.time()
                email_ip_sends[ip] = time.time()
            self.respond(200, {"message": "Verification code sent. Check your email."})
        except ValueError as error:
            self.respond(400, {"error": str(error)})
        except (OSError, RuntimeError, smtplib.SMTPException) as error:
            self.respond(503, {"error": str(error) or "Could not send the verification email."})

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
            self.respond(200, {"message": f"Removed staff account {target}."})
        except ValueError as error:
            self.respond(400, {"error": str(error)})

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
            with LOCK:
                if path == "/api/logout":
                    cookie = self.clear_session()
                    self.respond(200, {"message": "You have been signed out."}, headers={"Set-Cookie": cookie})
                    return

                username = data.get("username", "")
                password = data.get("password", "")
                if not isinstance(username, str) or not isinstance(password, str):
                    raise ValueError("Enter a username and password.")
                username = username.strip().lower()
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
                            or challenge["attempts"] >= 5 or not hmac.compare_digest(email_code, challenge["code"])):
                        if challenge:
                            challenge["attempts"] += 1
                        raise ValueError("Enter the valid email verification code sent to your company email.")
                    if not verify_turnstile(data.get("turnstile_token", ""), self.client_address[0]):
                        raise ValueError("Complete the bot verification check and try again.")
                    if username in users:
                        self.respond(409, {"error": "That username is already registered."})
                        return
                    salt = secrets.token_bytes(16)
                    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PASSWORD_ROUNDS)
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
                        self.respond(401, {"error": "Username or password is incorrect."})
                        return
                    expected = bytes.fromhex(user["password_hash"])
                    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                                  bytes.fromhex(user["salt"]), PASSWORD_ROUNDS)
                    if not hmac.compare_digest(actual, expected):
                        self.respond(401, {"error": "Username or password is incorrect."})
                        return
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
