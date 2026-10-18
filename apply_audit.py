"""Wire the audit log into web_app.py in one step.

Put this file next to web_app.py and audit.py, then run:  python3 apply_audit.py

All edits are checked first. If any spot in web_app.py cannot be found, nothing
is changed. The patched file must also compile before it is written. Your
original is saved once as web_app.py.audit.bak. Running the script twice is safe.
"""
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TARGET = os.path.join(HERE, "web_app.py")

HELPERS = '''AUDIT_ACTIONS = {
    "/api/resources": "item_added", "/api/resource-details": "item_updated",
    "/api/delete-resource": "item_deleted", "/api/restock": "stock_in",
    "/api/sale": "sale", "/api/borrow": "loan", "/api/return": "return",
}


def audit_log(handler, username, action, target="", detail=""):
    """Record an audit entry for a signed-in user. Call while holding LOCK."""
    audit.record(inventory, username, users[username]["role"], action,
                 target, detail, handler.client_address[0])


def audit_detail(path, data, message, before_resources):
    parts = [message]
    if path == "/api/resource-details":
        parts.append(audit.describe_item_changes(before_resources, data) or "no field changed")
    for key in ("quantity", "fellow_id", "customer"):
        if data.get(key) not in (None, ""):
            parts.append(f"{key}={data[key]}")
    return " | ".join(parts)


'''

POST_HOOK = '''                audit_log(self, username, AUDIT_ACTIONS[path],
                          data.get("resource_id") or data.get("id", ""),
                          audit_detail(path, data, message, before_resources))
'''

STAFF_ADDED = '''                audit_log(self, actor, "staff_created", username, f"name={name}")
'''

STAFF_REMOVED = '''                audit_log(self, actor, "staff_removed", target)
'''

SETTINGS_HOOK = '''                if success:
                    audit_log(self, actor, "settings_changed", "",
                              f"name={data.get('shop_name')}; currency={data.get('currency')}; "
                              f"color={data.get('brand_color')}")
'''

GET_ROUTE = '''        elif path in ("/api/audit", "/api/audit/export.csv"):
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
'''

PANEL = '''<section class="panel" id="auditPanel" style="margin-top:20px"><div class="panel-head"><div><h2>Audit log</h2><div class="subtle">Who changed what, and when</div></div><div class="actions"><input class="search" id="auditSearch" type="search" placeholder="Search person, action, item…" aria-label="Search audit log"><button class="btn secondary" id="exportAudit" type="button">↓ Export CSV</button></div></div><div class="table-wrap"><table><thead><tr><th>Time</th><th>Who</th><th>Action</th><th>Item / target</th><th>Details</th></tr></thead><tbody id="auditRows"><tr><td colspan="5" class="empty">Loading audit log…</td></tr></tbody></table></div><div class="form-message" id="auditMessage" role="status"></div></section>
    '''

SCRIPT = '''    let auditTimer;
    async function loadAudit(){try{const result=await api('/api/audit?q='+encodeURIComponent($('#auditSearch').value));$('#auditMessage').textContent='';const labels={item_added:'Item added',item_updated:'Item updated',item_deleted:'Item deleted',stock_in:'Stock-in',sale:'Sale',loan:'Loan',return:'Return',staff_created:'Staff added',staff_removed:'Staff removed',settings_changed:'Settings changed'};$('#auditRows').innerHTML=result.audit.length?result.audit.map(row=>`<tr><td>${esc(new Date(row.created_at).toLocaleString())}</td><td>${esc(row.actor)}<div class="resource-id">${esc(row.role)}</div></td><td>${esc(labels[row.action]||row.action)}</td><td>${esc(row.target||'—')}</td><td style="white-space:normal;min-width:240px">${esc(row.detail)}</td></tr>`).join(''):'<tr><td colspan="5" class="empty">No audit entries match.</td></tr>'}catch(error){$('#auditMessage').className='form-message error';$('#auditMessage').textContent=error.message}}
    $('#auditSearch').addEventListener('input',()=>{clearTimeout(auditTimer);auditTimer=setTimeout(loadAudit,180)});
    $('#exportAudit').addEventListener('click',async()=>{try{const response=await fetch('/api/audit/export.csv?q='+encodeURIComponent($('#auditSearch').value));if(!response.ok){const data=await response.json();throw new Error(data.error||'Could not export the audit log.')}const file=URL.createObjectURL(await response.blob()),link=document.createElement('a');link.href=file;link.download='resourcehub-audit-log.csv';link.click();setTimeout(()=>URL.revokeObjectURL(file),1000)}catch(error){$('#auditMessage').className='form-message error';$('#auditMessage').textContent=error.message}});
'''

# (name, text that proves the step is already applied, anchor, mode, new text)
EDITS = [
    ("import audit", "import audit\n",
     "import main as inventory\n", "after", "import audit\n"),
    ("helper functions", "def audit_log(",
     "def activate_shop(shop_id, allow_create=False):", "before", HELPERS),
    ("inventory actions", 'audit_log(self, username, AUDIT_ACTIONS[path]',
     'self.respond(500, {"error": f"Could not save this change: {error}"})\n                    return\n',
     "after", POST_HOOK),
    ("staff created", '"staff_created"',
     'self.respond(500, {"error": f"Could not save this staff account: {error}"})\n                    return\n',
     "after", STAFF_ADDED),
    ("staff removed", '"staff_removed"',
     'if session["username"] == target:\n                        sessions.pop(token, None)\n',
     "after", STAFF_REMOVED),
    ("settings changed", '"settings_changed"',
     'data.get("currency"), data.get("logo_data", ""),\n                )\n',
     "after", SETTINGS_HOOK),
    ("audit API routes", '"/api/audit/export.csv"',
     '        elif path in ("/api/reports", "/api/reports/export.csv", "/api/backup"):',
     "before", GET_ROUTE),
    ("audit panel", 'id="auditPanel"',
     '<div class="footnote">', "before", PANEL),
    ("audit script", "function loadAudit",
     "    $('#backupButton').addEventListener(", "before", SCRIPT),
    ("refresh hook", "loads.push(loadAudit())",
     "if(currentRole==='owner')loads.push(loadReports());", "replace",
     "if(currentRole==='owner'){loads.push(loadReports());loads.push(loadAudit())}"),
    ("hide panel for staff", "$('#auditPanel').hidden",
     "$('#reportsPanel').hidden=currentRole!=='owner';", "replace",
     "$('#reportsPanel').hidden=currentRole!=='owner';$('#auditPanel').hidden=currentRole!=='owner';"),
]


def fail(message):
    print("Nothing was changed. " + message)
    sys.exit(1)


def main():
    if not os.path.isfile(TARGET):
        fail(f"Could not find web_app.py in {HERE}.")
    if not os.path.isfile(os.path.join(HERE, "audit.py")):
        fail("Could not find audit.py. Put it next to web_app.py first.")

    with open(TARGET, encoding="utf-8", newline="") as handle:
        raw = handle.read()
    newline = "\r\n" if "\r\n" in raw else "\n"
    source = raw.replace("\r\n", "\n")

    applied, skipped = [], []
    for name, marker, anchor, mode, new_text in EDITS:
        if name != "import audit" and marker in source:
            skipped.append(name)
            continue
        if name == "import audit" and "\nimport audit\n" in source:
            skipped.append(name)
            continue
        if source.count(anchor) != 1:
            fail(f'Could not find a unique spot for "{name}" in web_app.py. '
                 "The file may have been edited since this script was written; "
                 "send me this message and I will adjust it.")
        if mode == "after":
            source = source.replace(anchor, anchor + new_text, 1)
        elif mode == "before":
            source = source.replace(anchor, new_text + anchor, 1)
        else:
            source = source.replace(anchor, new_text, 1)
        applied.append(name)

    if not applied:
        print("The audit log is already wired in. Nothing to do.")
        return

    try:
        compile(source, TARGET, "exec")
    except SyntaxError as error:
        fail(f"The patched file would not compile ({error}).")

    backup = TARGET + ".audit.bak"
    if not os.path.exists(backup):
        shutil.copy2(TARGET, backup)
    with open(TARGET, "w", encoding="utf-8", newline="") as handle:
        handle.write(source.replace("\n", newline))

    print(f"Done. Applied {len(applied)} edits" +
          (f" ({len(skipped)} were already in place)." if skipped else "."))
    print("Your previous file is saved as web_app.py.audit.bak.")
    print("Now run:  python3 web_app.py   and sign in as the owner.")


if __name__ == "__main__":
    main()
