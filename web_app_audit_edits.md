# Wiring the audit log into `web_app.py`

Copy `audit.py` and `test_audit.py` next to `web_app.py`. Then make these edits in order. No change to `main.py` is needed: the `audit_log` table is created automatically inside each shop's own database the first time it is used, including existing shops.

## 1. Import and helpers (top of the file)

After `import main as inventory`, add `import audit`.

After the `TURNSTILE_SITE_KEY = ...` block (before `def activate_shop`), add:

```python
AUDIT_ACTIONS = {
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
```

## 2. Inventory actions (`do_POST`)

Find the `inventory.save_data()` block near the end of the `with LOCK:` section. Directly after its `except ... return`, still inside `with LOCK:` and indented to match `try:`, add:

```python
                audit_log(self, username, AUDIT_ACTIONS[path],
                          data.get("resource_id") or data.get("id", ""),
                          audit_detail(path, data, message, before_resources))
```

## 3. Staff and settings

In `handle_staff_creation`, after the `try: save_users() ... return` block, add:

```python
                audit_log(self, actor, "staff_created", username, f"name={name}")
```

In `handle_staff_deletion`, after the `for token, session in list(sessions.items()):` loop (inside `with LOCK:`), add:

```python
                audit_log(self, actor, "staff_removed", target)
```

In `handle_shop_settings`, directly after the `success, message = inventory.update_shop_settings(...)` call (inside `with LOCK:`), add:

```python
                if success:
                    audit_log(self, actor, "settings_changed", "",
                              f"name={data.get('shop_name')}; currency={data.get('currency')}; "
                              f"color={data.get('brand_color')}")
```

## 4. Owner-only API routes (`do_GET`)

Add this branch just before `elif path in ("/api/reports", "/api/reports/export.csv", "/api/backup"):`

```python
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
```

## 5. The page (inside the `PAGE` string)

**HTML.** Add this directly before `<div class="footnote">`:

```html
<section class="panel" id="auditPanel" style="margin-top:20px"><div class="panel-head"><div><h2>Audit log</h2><div class="subtle">Who changed what, and when</div></div><div class="actions"><input class="search" id="auditSearch" type="search" placeholder="Search person, action, item…" aria-label="Search audit log"><button class="btn secondary" id="exportAudit" type="button">↓ Export CSV</button></div></div><div class="table-wrap"><table><thead><tr><th>Time</th><th>Who</th><th>Action</th><th>Item / target</th><th>Details</th></tr></thead><tbody id="auditRows"><tr><td colspan="5" class="empty">Loading audit log…</td></tr></tbody></table></div><div class="form-message" id="auditMessage" role="status"></div></section>
```

**JavaScript.** Add this right after the `loadReports` function and its `exportReport` listener:

```js
let auditTimer;
async function loadAudit(){try{const result=await api('/api/audit?q='+encodeURIComponent($('#auditSearch').value));$('#auditMessage').textContent='';const labels={item_added:'Item added',item_updated:'Item updated',item_deleted:'Item deleted',stock_in:'Stock-in',sale:'Sale',loan:'Loan',return:'Return',staff_created:'Staff added',staff_removed:'Staff removed',settings_changed:'Settings changed'};$('#auditRows').innerHTML=result.audit.length?result.audit.map(row=>`<tr><td>${esc(new Date(row.created_at).toLocaleString())}</td><td>${esc(row.actor)}<div class="resource-id">${esc(row.role)}</div></td><td>${esc(labels[row.action]||row.action)}</td><td>${esc(row.target||'—')}</td><td style="white-space:normal;min-width:240px">${esc(row.detail)}</td></tr>`).join(''):'<tr><td colspan="5" class="empty">No audit entries match.</td></tr>'}catch(error){$('#auditMessage').className='form-message error';$('#auditMessage').textContent=error.message}}
$('#auditSearch').addEventListener('input',()=>{clearTimeout(auditTimer);auditTimer=setTimeout(loadAudit,180)});
$('#exportAudit').addEventListener('click',async()=>{try{const response=await fetch('/api/audit/export.csv?q='+encodeURIComponent($('#auditSearch').value));if(!response.ok){const data=await response.json();throw new Error(data.error||'Could not export the audit log.')}const file=URL.createObjectURL(await response.blob()),link=document.createElement('a');link.href=file;link.download='resourcehub-audit-log.csv';link.click();setTimeout(()=>URL.revokeObjectURL(file),1000)}catch(error){$('#auditMessage').className='form-message error';$('#auditMessage').textContent=error.message}});
```

Two one-line changes to existing JavaScript:

- In `refresh()`, change `if(currentRole==='owner')loads.push(loadReports());` to `if(currentRole==='owner'){loads.push(loadReports());loads.push(loadAudit())}`
- In `showApp()`, add `$('#auditPanel').hidden=currentRole!=='owner';` next to the `$('#reportsPanel').hidden=...` statement.

## 6. Check it

```bash
python3 -m unittest test_audit -v
python3 -m py_compile main.py web_app.py audit.py
python3 web_app.py
```

Sign in as owner, add an item, record a sale, change a price in Details, and add a staff member. Each should appear in the Audit log panel. Sign in as staff and confirm the panel is hidden and `/api/audit` returns 403.

## Known limits

- Only successful actions are logged. Rejected requests and logins are not yet recorded.
- The audit write happens right after the business change is saved, not in the same database transaction. If it fails, the failure is printed to the server log and the action still stands.
- The IP column shows the proxy's address when the site runs behind a reverse proxy such as Render's.
