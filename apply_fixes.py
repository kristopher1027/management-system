"""Fix the reviewed bugs in web_app.py in one step.

Put this file next to web_app.py and run:  python3 apply_fixes.py

All edits are checked first; if any spot cannot be found, nothing is changed.
The patched file must compile before it is written. Your original is saved once
as web_app.py.fixes.bak. Running the script twice is safe.
"""
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TARGET = os.path.join(HERE, "web_app.py")

EMAIL_SECTION = '''def smtp_settings():
    """Read and validate the SMTP settings from the environment."""
    host = os.environ.get("RESOURCEHUB_SMTP_HOST", "smtp.gmail.com")
    sender = os.environ.get("RESOURCEHUB_SMTP_FROM", "")
    password = os.environ.get("RESOURCEHUB_SMTP_PASSWORD", "").replace(" ", "")
    username = os.environ.get("RESOURCEHUB_SMTP_USERNAME", sender)
    port = int(os.environ.get("RESOURCEHUB_SMTP_PORT", "587"))
    if (not sender or not password or sender.lower() == "your-sender@gmail.com"
            or password.lower() == "your-google-app-password"):
        raise RuntimeError("Replace the example sender and password in .env with your sending Gmail address and its Google App Password.")
    if len(password) != 16:
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
    with smtplib.SMTP(host, port, timeout=15) as smtp:
        smtp.starttls()
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


'''

HELPERS = '''PROXY_HOPS = int(os.environ.get("RESOURCEHUB_PROXY_HOPS", "0") or 0)
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


'''

EMAIL_HANDLER = '''    def handle_email_verification(self):
        try:
            data = self.read_json()
            email = data.get("email", "")
            if not isinstance(email, str) or len(email) > 254 or not re.fullmatch(r"[^@\\s]+@[^@\\s]+\\.[^@\\s]+", email):
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

'''

CLIENT_IP = '''    def client_ip(self):
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

'''

AUTH_HEAD = '''            data = self.read_json()
            hashed = prehash_password(path, data, self.client_ip())
            with LOCK:
                purge_expired()
                if path == "/api/logout":
'''

REGISTER_HASH = '''                    if hashed is None:
                        salt = secrets.token_bytes(16)
                        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PASSWORD_ROUNDS)
                    else:
                        salt, digest = hashed["salt"], hashed["digest"]
                    shop_id = secrets.token_hex(8)
'''

LOGIN_COMPARE = '''                    expected = bytes.fromhex(user["password_hash"])
                    user_salt = bytes.fromhex(user["salt"])
                    if hashed is not None and hashed["salt"] == user_salt:
                        actual = hashed["digest"]
                    else:
                        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), user_salt, PASSWORD_ROUNDS)
'''


def ins_after(name, done, anchor, text):
    return {"name": name, "done": done, "kind": "after", "a": anchor, "b": text}


def ins_before(name, done, anchor, text):
    return {"name": name, "done": done, "kind": "before", "a": anchor, "b": text}


def rep(name, done, old, new):
    return {"name": name, "done": done, "kind": "replace", "a": old, "b": new}


def region(name, done, start, end, text):
    return {"name": name, "done": done, "kind": "region", "a": start, "c": end, "b": text}


def rep_all(name, done, old, new):
    return {"name": name, "done": done, "kind": "all", "a": old, "b": new}


EDITS = [
    rep("import math", "\nimport math\n", "\nimport json\n", "\nimport json\nimport math\n"),
    rep("import ipaddress", "\nimport ipaddress\n", "\nimport io\n", "\nimport io\nimport ipaddress\n"),
    ins_after("import EmailMessage", "from email.message import EmailMessage",
              "\nimport smtplib\n", "from email.message import EmailMessage\n"),
    region("email sending (fixes broken test-email headers)", "def deliver_email(",
           "def send_email_code(email):", "def verify_turnstile(token, remote_ip):", EMAIL_SECTION),
    ins_before("helper functions", "def prehash_password(", "PAGE = r'''<!doctype html>", HELPERS),
    region("send-code endpoint (bot check, no SMTP inside LOCK)", "email_sending.add(email)",
           "    def handle_email_verification(self):", "    def handle_staff_deletion(self):", EMAIL_HANDLER),
    rep_all("use client_ip() in handlers", "self.client_ip()",
            "self.client_address[0]", "self.client_ip()"),
    rep_all("use client_ip() in audit log", "handler.client_ip()",
            "handler.client_address[0]", "handler.client_ip()"),
    ins_before("client_ip method", "def client_ip(self)",
               "    def current_user(self):", CLIENT_IP),
    rep("hash passwords outside LOCK (sign-in)", "hashed = prehash_password(",
        '            data = self.read_json()\n            with LOCK:\n                if path == "/api/logout":\n',
        AUTH_HEAD),
    rep("hash passwords outside LOCK (register)", 'salt, digest = hashed["salt"], hashed["digest"]',
        '                    salt = secrets.token_bytes(16)\n'
        '                    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PASSWORD_ROUNDS)\n'
        '                    shop_id = secrets.token_hex(8)\n', REGISTER_HASH),
    region("hash passwords outside LOCK (login)", "user_salt = bytes.fromhex",
           '                    expected = bytes.fromhex(user["password_hash"])\n',
           "                    if not hmac.compare_digest(actual, expected):", LOGIN_COMPARE),
    rep("reject NaN and Infinity in request data", "parse_constant=reject_json_constant",
        "json.loads(self.rfile.read(size))",
        "json.loads(self.rfile.read(size), parse_constant=reject_json_constant,\n"
        "                              parse_float=parse_finite_float)"),
    rep("verification code compare with non-ASCII text", 'email_code.encode("utf-8")',
        'not hmac.compare_digest(email_code, challenge["code"])',
        'not hmac.compare_digest(email_code.encode("utf-8"), challenge["code"].encode("utf-8"))'),
    ins_after("security headers", "X-Frame-Options",
              '        self.send_header("X-Content-Type-Options", "nosniff")\n',
              '        self.send_header("X-Frame-Options", "DENY")\n'
              '        self.send_header("Referrer-Policy", "same-origin")\n'
              '        self.send_header("Content-Security-Policy", "frame-ancestors \'none\'; base-uri \'none\'; form-action \'self\'")\n'),
    rep("send bot-check token with the code request", "turnstile_token:document.querySelector('[name=\"cf-turnstile-response\"]')?.value||''})",
        "await api('/api/send-verification',{email:$('#companyEmail').value})",
        "await api('/api/send-verification',{email:$('#companyEmail').value,turnstile_token:document.querySelector('[name=\"cf-turnstile-response\"]')?.value||''})"),
    rep("reset bot check after sending a code", "window.turnstile.reset()",
        "errorBox.style.color='var(--red)'}});",
        "errorBox.style.color='var(--red)'}finally{if(window.turnstile)window.turnstile.reset()}});"),
]


def fail(message):
    print("Nothing was changed. " + message)
    sys.exit(1)


def apply_edit(source, edit):
    name, kind, anchor = edit["name"], edit["kind"], edit["a"]
    if kind == "all":
        # Bulk replacements are done when no old text is left.
        if source.count(anchor) == 0:
            return source, False
        return source.replace(anchor, edit["b"]), True
    if edit["done"] in source:
        return source, False
    if source.count(anchor) != 1:
        fail(f'Could not find a unique spot for "{name}" in web_app.py. '
             "The file may have been edited since this script was written; "
             "send me this message and I will adjust it.")
    if kind == "after":
        return source.replace(anchor, anchor + edit["b"], 1), True
    if kind == "before":
        return source.replace(anchor, edit["b"] + anchor, 1), True
    if kind == "replace":
        return source.replace(anchor, edit["b"], 1), True
    end = edit["c"]
    start_at = source.index(anchor)
    if source.count(end) != 1 or source.index(end) < start_at:
        fail(f'Could not find the end of the section for "{name}" in web_app.py.')
    return source[:start_at] + edit["b"] + source[source.index(end):], True


def main():
    if not os.path.isfile(TARGET):
        fail(f"Could not find web_app.py in {HERE}.")
    with open(TARGET, encoding="utf-8", newline="") as handle:
        raw = handle.read()
    newline = "\r\n" if "\r\n" in raw else "\n"
    source = raw.replace("\r\n", "\n")

    applied = []
    for edit in EDITS:
        source, changed = apply_edit(source, edit)
        if changed:
            applied.append(edit["name"])

    if not applied:
        print("All fixes are already applied. Nothing to do.")
        return
    try:
        compile(source, TARGET, "exec")
    except SyntaxError as error:
        fail(f"The patched file would not compile ({error}).")

    backup = TARGET + ".fixes.bak"
    if not os.path.exists(backup):
        shutil.copy2(TARGET, backup)
    with open(TARGET, "w", encoding="utf-8", newline="") as handle:
        handle.write(source.replace("\n", newline))

    print(f"Done. Applied {len(applied)} fixes:")
    for name in applied:
        print("  - " + name)
    print("Your previous file is saved as web_app.py.fixes.bak.")
    print("If the site runs behind a reverse proxy such as Render, also set")
    print("RESOURCEHUB_PROXY_HOPS=1 so sign-in limits apply to each visitor separately.")


if __name__ == "__main__":
    main()
