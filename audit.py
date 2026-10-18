"""Per-shop audit trail for ResourceHub.

Every shop has its own SQLite database, so the audit_log table lives beside that
shop's inventory data and can never mix entries between companies. Each function
takes the ``inventory`` module (main.py) and uses whichever shop database
``web_app.activate_shop()`` selected. Callers must hold ``web_app.LOCK``, exactly
like the rest of the web layer.
"""
import csv
import io
import sqlite3

SCHEMA = """CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    actor TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT '',
    action TEXT NOT NULL,
    target TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT '',
    ip TEXT NOT NULL DEFAULT ''
)"""

MAX_ROWS = 300
CSV_MAX_ROWS = 50_000


def record(inventory, actor, role, action, target="", detail="", ip=""):
    """Append one entry to the active shop's audit log.

    Never raises: a logging failure must not undo a business action that has
    already been saved. Returns True when the entry was written.
    """
    try:
        with inventory.connect() as db:
            db.execute(SCHEMA)
            db.execute(
                "INSERT INTO audit_log (actor, role, action, target, detail, ip) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (str(actor)[:80], str(role)[:20], str(action)[:40],
                 str(target)[:100], str(detail)[:400], str(ip)[:64]),
            )
        return True
    except sqlite3.Error as error:
        print(f"Audit log write failed: {error}")
        return False


def _like(term):
    """Escape LIKE wildcards so a search for '%' or '_' matches literally."""
    return "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def entries(inventory, search_text="", limit=MAX_ROWS):
    """Return audit entries, newest first, optionally filtered by a search term."""
    term = str(search_text).strip().casefold()[:120]
    with inventory.connect() as db:
        db.execute(SCHEMA)
        rows = db.execute(
            """SELECT id, created_at, actor, role, action, target, detail, ip
               FROM audit_log
               WHERE ? = '' OR lower(actor || ' ' || action || ' ' || target || ' ' || detail)
                     LIKE ? ESCAPE '\\'
               ORDER BY id DESC LIMIT ?""",
            (term, _like(term), limit),
        ).fetchall()
    return [dict(row) for row in rows]


def _safe(value):
    """Neutralize spreadsheet formulas in exported text."""
    if isinstance(value, str) and value.startswith(("=", "+", "-", "@", "\t", "\r")):
        return "'" + value
    return value


def to_csv(inventory, search_text=""):
    """Render the (optionally filtered) audit log as CSV text."""
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(["id", "time_utc", "actor", "role", "action", "target", "detail", "ip"])
    for row in entries(inventory, search_text, limit=CSV_MAX_ROWS):
        writer.writerow([row["id"], row["created_at"], _safe(row["actor"]), _safe(row["role"]),
                         _safe(row["action"]), _safe(row["target"]), _safe(row["detail"]),
                         _safe(row["ip"])])
    return output.getvalue()


def _same(old, new):
    numeric = (int, float)
    if (isinstance(old, numeric) and isinstance(new, numeric)
            and not isinstance(old, bool) and not isinstance(new, bool)):
        return float(old) == float(new)
    return str(old if old is not None else "").strip() == str(new if new is not None else "").strip()


def describe_item_changes(before_resources, data):
    """Return e.g. 'price: 5.0 → 7.5; supplier: Acme → Zenith' for a details update.

    ``before_resources`` is a copy of the resource list taken before the change;
    ``data`` is the request body. Returns '' when nothing differs or the item is unknown.
    """
    wanted = str(data.get("resource_id", "")).strip().casefold()
    old = next((item for item in before_resources
                if str(item.get("id", "")).casefold() == wanted), None)
    if old is None:
        return ""
    changes = []
    for field in ("price", "sku", "supplier", "reorder_level"):
        if field in data and not _same(old.get(field), data[field]):
            changes.append(f"{field}: {old.get(field)} → {data[field]}")
    return "; ".join(changes)
