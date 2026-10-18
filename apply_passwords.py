"""Add password change and password reset to web_app.py in one step.

What you get:
  - "Change password" button for every signed-in user (signs out their other devices)
  - "Forgot your password?" on the sign-in screen: owners get a code by email
  - "Reset password" next to each staff member: the owner gets a temporary password

Needs apply_fixes.py to have been applied first. Put this file next to web_app.py
and run:  python3 apply_passwords.py

All edits are checked first; if any spot cannot be found, nothing is changed.
The patched file must compile before it is written. Your original is saved once
as web_app.py.passwords.bak. Running the script twice is safe.
"""
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TARGET = os.path.join(HERE, "web_app.py")

HELPERS = '''PASSWORD_MIN_LENGTH = 10
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


'''

SESSION_TOKEN = '''    def session_token(self):
        from http.cookies import SimpleCookie
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
            return cookie[SESSION_COOKIE].value
        except (KeyError, TypeError, ValueError):
            return None

'''

ROUTES = '''        if path == "/api/change-password":
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
'''

HANDLERS = '''    def handle_change_password(self):
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
#AUDIT:change#
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
#AUDIT:staff#
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
#AUDIT:reset#
            self.respond(200, {"message": "Password updated. Sign in with your new password."})
        except ValueError as error:
            self.respond(400, {"error": str(error)})

'''

AUDIT_LINES = {
    "#AUDIT:change#\n": '                audit_log(self, username, "password_changed", "", "changed by the user")\n',
    "#AUDIT:staff#\n": '                audit_log(self, actor, "staff_password_reset", target)\n',
    "#AUDIT:reset#\n": (
        "                try:\n"
        "                    activate_shop(user[\"shop_id\"])\n"
        "                    audit_log(self, username, \"password_reset\", \"\", \"reset with an emailed code\")\n"
        "                except (RuntimeError, OSError, sqlite3.Error):\n"
        "                    pass\n"),
}

RESET_FORM = '''<button class="auth-back" type="button" id="forgotLink" style="margin:14px 0 0">Forgot your password?</button><form id="resetForm" hidden><h2 style="font-size:17px;margin:0 0 4px">Reset your password</h2><p class="subtle" style="margin:0 0 14px">Owners get a code at the email address used to register. Staff: ask your shop owner to reset your password.</p><div class="field"><label for="resetUsername">Username</label><input id="resetUsername" autocomplete="username" maxlength="40" required></div><button class="btn secondary full" type="button" id="resetSend">Email me a code</button><div class="field" style="margin-top:14px"><label for="resetCode">Code from the email</label><input id="resetCode" inputmode="numeric" maxlength="6" autocomplete="one-time-code" required></div><div class="field"><label for="resetPassword">New password (at least 10 characters)</label><input id="resetPassword" type="password" minlength="10" autocomplete="new-password" required></div><button class="btn full" type="submit">Set new password</button><div class="auth-error" id="resetMessage" role="status"></div><button class="auth-back" type="button" id="resetBack" style="margin:0">Back to sign in</button></form>'''

HEADER_BUTTON = '''<button class="logout" id="passwordButton" type="button">Change password</button>'''

PASSWORD_DIALOG = '''<dialog id="passwordDialog" style="border:0;border-radius:15px;padding:24px;width:min(440px,calc(100% - 28px));box-shadow:0 25px 80px #101b2d33"><form id="passwordForm"><h2 style="margin:0 0 5px">Change password</h2><p class="subtle" style="margin:0 0 16px">You stay signed in on this device. Your other devices are signed out.</p><div class="field"><label for="currentPassword">Current password</label><input id="currentPassword" type="password" autocomplete="current-password" required></div><div class="field"><label for="newPassword">New password (at least 10 characters)</label><input id="newPassword" type="password" minlength="10" autocomplete="new-password" required></div><div style="display:flex;gap:9px;justify-content:flex-end;margin-top:18px"><button type="button" class="btn secondary" id="cancelPassword">Cancel</button><button class="btn" type="submit">Save new password</button></div><div id="passwordMessage" class="form-message" role="status"></div></form></dialog>
  '''

SCRIPT = '''    const baseSetAuthMode=setAuthMode;
    setAuthMode=function(next){baseSetAuthMode(next);$('#resetForm').hidden=true;$('#authForm').hidden=false;document.querySelector('.auth-tabs').hidden=false;$('#forgotLink').hidden=authMode!=='login'};
    $('#forgotLink').addEventListener('click',()=>{$('#authForm').hidden=true;document.querySelector('.auth-tabs').hidden=true;$('#forgotLink').hidden=true;$('#resetForm').hidden=false;$('#resetUsername').value=$('#authUsername').value;$('#authTitle').textContent='Reset your password';$('#authSubtitle').textContent='We email a code to the address on your owner account.';$('#resetMessage').textContent='';$('#authError').textContent=''});
    $('#resetBack').addEventListener('click',()=>setAuthMode('login'));
    $('#resetSend').addEventListener('click',async()=>{const box=$('#resetMessage');box.style.color='var(--red)';box.textContent='';try{const result=await api('/api/password-reset/request',{username:$('#resetUsername').value});box.textContent=result.message;box.style.color='var(--green)'}catch(error){box.textContent=error.message}});
    $('#resetForm').addEventListener('submit',async event=>{event.preventDefault();const box=$('#resetMessage');box.style.color='var(--red)';box.textContent='';try{const result=await api('/api/password-reset/confirm',{username:$('#resetUsername').value,code:$('#resetCode').value,new_password:$('#resetPassword').value});event.target.reset();setAuthMode('login');$('#authError').style.color='var(--green)';$('#authError').textContent=result.message}catch(error){box.textContent=error.message}});
    $('#passwordButton').addEventListener('click',()=>{$('#passwordForm').reset();$('#passwordMessage').className='form-message';$('#passwordMessage').textContent='';$('#passwordDialog').showModal()});
    $('#cancelPassword').addEventListener('click',()=>$('#passwordDialog').close());
    $('#passwordForm').addEventListener('submit',async event=>{event.preventDefault();const box=$('#passwordMessage');box.className='form-message';box.textContent='';try{const result=await api('/api/change-password',{current_password:$('#currentPassword').value,new_password:$('#newPassword').value});$('#passwordDialog').close();event.target.reset();showToast(result.message)}catch(error){box.classList.add('error');box.textContent=error.message}});
    $('#staffRows').addEventListener('click',async event=>{const button=event.target.closest('.reset-staff');if(!button)return;if(!window.confirm(`Reset the password for ${button.dataset.username}? A temporary password will be shown once.`))return;const message=$('#staffMessage');try{const result=await api('/api/staff/reset-password',{username:button.dataset.username});message.className='form-message';message.textContent=result.message}catch(error){message.className='form-message error';message.textContent=error.message}});
'''

STAFF_ROW_OLD = '''<td><button class="btn secondary remove-staff" data-username="${esc(person.username)}" type="button">Remove</button></td>'''
STAFF_ROW_NEW = '''<td><button class="btn secondary reset-staff" data-username="${esc(person.username)}" type="button">Reset password</button> <button class="btn secondary remove-staff" data-username="${esc(person.username)}" type="button">Remove</button></td>'''


def build_edits(has_audit):
    handlers = HANDLERS
    for placeholder, line in AUDIT_LINES.items():
        handlers = handlers.replace(placeholder, line if has_audit else "")
    edits = [
        ("import Thread", "RLock, Thread", "replace",
         "from threading import RLock\n", "from threading import RLock, Thread\n", None),
        ("password helpers", "def hash_new_password(", "before",
         "PAGE = r'''<!doctype html>", HELPERS, None),
        ("session token reader", "def session_token(", "before",
         "    def set_session(self, username):", SESSION_TOKEN, None),
        ("password routes", '"/api/change-password"', "before",
         '        if path == "/api/staff":\n            self.handle_staff_creation()\n            return\n',
         ROUTES, None),
        ("password handlers", "def handle_change_password(", "before",
         "    def handle_staff_deletion(self):", handlers, None),
        ("forgot-password form", 'id="resetForm"', "before",
         '<div class="subtle">Your accounts and inventory are stored in the configured data directory.</div>',
         RESET_FORM, None),
        ("change-password button", 'id="passwordButton"', "before",
         '<button class="logout" id="logoutButton">Sign out</button>', HEADER_BUTTON, None),
        ("change-password dialog", 'id="passwordDialog"', "before",
         '<dialog id="confirmDialog"', PASSWORD_DIALOG, None),
        ("staff reset button", "reset-staff", "replace", STAFF_ROW_OLD, STAFF_ROW_NEW, None),
        ("page script", "$('#forgotLink').addEventListener", "before",
         "    document.querySelectorAll('[data-open-auth]')", SCRIPT, None),
    ]
    if has_audit:
        edits.append(("audit labels", "password_changed:'Password changed'", "replace",
                      "settings_changed:'Settings changed'}",
                      "settings_changed:'Settings changed',password_changed:'Password changed',"
                      "staff_password_reset:'Staff password reset',password_reset:'Password reset'}", None))
    return edits


def fail(message):
    print("Nothing was changed. " + message)
    sys.exit(1)


def apply_edit(source, edit):
    name, done, kind, anchor, text, _ = edit
    if done in source:
        return source, False
    if source.count(anchor) != 1:
        fail(f'Could not find a unique spot for "{name}" in web_app.py. '
             "The file may have been edited since this script was written; "
             "send me this message and I will adjust it.")
    if kind == "before":
        return source.replace(anchor, text + anchor, 1), True
    return source.replace(anchor, text, 1), True


def main():
    if not os.path.isfile(TARGET):
        fail(f"Could not find web_app.py in {HERE}.")
    with open(TARGET, encoding="utf-8", newline="") as handle:
        raw = handle.read()
    newline = "\r\n" if "\r\n" in raw else "\n"
    source = raw.replace("\r\n", "\n")

    for needed in ("def deliver_email(", "def client_ip(self)", "def prehash_password("):
        if needed not in source:
            fail("Run apply_fixes.py first. This script builds on it.")

    has_audit = "def audit_log(" in source
    applied = []
    for edit in build_edits(has_audit):
        source, changed = apply_edit(source, edit)
        if changed:
            applied.append(edit[0])
    if not applied:
        print("Password change and reset are already in place. Nothing to do.")
        return
    try:
        compile(source, TARGET, "exec")
    except SyntaxError as error:
        fail(f"The patched file would not compile ({error}).")

    backup = TARGET + ".passwords.bak"
    if not os.path.exists(backup):
        shutil.copy2(TARGET, backup)
    with open(TARGET, "w", encoding="utf-8", newline="") as handle:
        handle.write(source.replace("\n", newline))

    print(f"Done. Applied {len(applied)} edits:")
    for name in applied:
        print("  - " + name)
    if not has_audit:
        print("(The audit log was not found, so password events are not logged.)")
    print("Your previous file is saved as web_app.py.passwords.bak.")
    print("Now run:  python3 web_app.py")


if __name__ == "__main__":
    main()
