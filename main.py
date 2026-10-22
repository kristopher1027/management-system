"""ResourceHub campus equipment manager backed by Python's standard library."""
import csv
import base64
import json
import math
import os
import re
import sqlite3
import tempfile
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

DATA_DIR = os.path.abspath(os.environ.get("RESOURCEHUB_DATA_DIR", os.getcwd()))
os.makedirs(DATA_DIR, exist_ok=True)
DB_FILE = os.path.join(DATA_DIR, "campus_resources.db")
BACKUP_DIR = os.path.join(DATA_DIR, "backups")
LOAN_DAYS = 14
BORROW_LIMIT = 3


def current_timestamp():
    """Return a readable local date and time for a new loan log record."""
    return datetime.now().isoformat(timespec="seconds")

# Compatibility views used by the small existing browser interface.
resources = []
fellows = {}
borrow_records = []


def connect():
    db = sqlite3.connect(DB_FILE, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    return db


def backup_database():
    """Keep an atomic daily SQLite snapshot, refreshing it before later changes."""
    source_path = Path(DB_FILE)
    if not source_path.is_file():
        return None
    backup_dir = Path(BACKUP_DIR)
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_path = backup_dir / f"resourcehub-{date.today().isoformat()}.sqlite3"
    if backup_path.exists() and backup_path.stat().st_mtime_ns >= source_path.stat().st_mtime_ns:
        return backup_path
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(dir=backup_dir, prefix=".resourcehub-", suffix=".tmp", delete=False) as temp:
            temporary_path = temp.name
        with sqlite3.connect(str(source_path)) as source, sqlite3.connect(temporary_path) as target:
            source.backup(target)
        os.replace(temporary_path, backup_path)
        temporary_path = None
        return backup_path
    finally:
        if temporary_path and os.path.exists(temporary_path):
            os.remove(temporary_path)


def init_db(seed_defaults=True):
    database_was_present = Path(DB_FILE).exists()
    if database_was_present:
        backup_database()
    with connect() as db:
        # Rebuild the old loan table once so it can also store damaged returns.
        db.execute("PRAGMA foreign_keys = OFF")
        db.executescript("""
            CREATE TABLE IF NOT EXISTS resources (
              id TEXT PRIMARY KEY, name TEXT NOT NULL, category TEXT NOT NULL,
              total INTEGER NOT NULL CHECK(total > 0), sku TEXT NOT NULL DEFAULT '',
              price REAL NOT NULL DEFAULT 0, cost_price REAL NOT NULL DEFAULT 0 CHECK(cost_price >= 0),
              supplier TEXT NOT NULL DEFAULT '',
              reorder_level INTEGER NOT NULL DEFAULT 3 CHECK(reorder_level >= 0));
            CREATE TABLE IF NOT EXISTS fellows (
              id TEXT PRIMARY KEY, name TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS loans (
              id INTEGER PRIMARY KEY AUTOINCREMENT, fellow_id TEXT NOT NULL REFERENCES fellows(id),
              resource_id TEXT NOT NULL REFERENCES resources(id), quantity INTEGER NOT NULL CHECK(quantity > 0),
              type TEXT NOT NULL CHECK(type IN ('borrow','return','damaged')),
              due_date TEXT, loan_id INTEGER REFERENCES loans(id),
              created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
            CREATE INDEX IF NOT EXISTS loan_resource_type ON loans(resource_id,type);
            CREATE INDEX IF NOT EXISTS loan_fellow_resource ON loans(fellow_id,resource_id);
            CREATE TABLE IF NOT EXISTS sales (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              resource_id TEXT NOT NULL REFERENCES resources(id),
              quantity INTEGER NOT NULL CHECK(quantity > 0),
              unit_price REAL NOT NULL DEFAULT 0 CHECK(unit_price >= 0),
              unit_cost REAL CHECK(unit_cost >= 0),
              customer TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
            CREATE INDEX IF NOT EXISTS sale_resource ON sales(resource_id);
            CREATE TABLE IF NOT EXISTS stock_movements (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              resource_id TEXT NOT NULL REFERENCES resources(id),
              quantity INTEGER NOT NULL CHECK(quantity > 0),
              movement_type TEXT NOT NULL DEFAULT 'restock',
              created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
            CREATE INDEX IF NOT EXISTS stock_movement_resource ON stock_movements(resource_id);
            CREATE TABLE IF NOT EXISTS stock_adjustments (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              resource_id TEXT NOT NULL REFERENCES resources(id),
              quantity INTEGER NOT NULL CHECK(quantity != 0),
              note TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
            CREATE INDEX IF NOT EXISTS stock_adjustment_resource ON stock_adjustments(resource_id);
            CREATE TABLE IF NOT EXISTS expenses (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              category TEXT NOT NULL,
              description TEXT NOT NULL DEFAULT '',
              amount REAL NOT NULL CHECK(amount > 0),
              created_at TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS expense_created_at ON expenses(created_at);
            CREATE TABLE IF NOT EXISTS shop_settings (
              id INTEGER PRIMARY KEY CHECK(id=1),
              shop_name TEXT NOT NULL DEFAULT 'ResourceHub',
              brand_color TEXT NOT NULL DEFAULT '#536dfe',
              currency TEXT NOT NULL DEFAULT 'NGN',
              logo_data TEXT NOT NULL DEFAULT '');
        """)
        db.execute("INSERT OR IGNORE INTO shop_settings(id) VALUES(1)")
        resource_columns = {row["name"] for row in db.execute("PRAGMA table_info(resources)")}
        for column, declaration in (
            ("sku", "TEXT NOT NULL DEFAULT ''"),
            ("price", "REAL NOT NULL DEFAULT 0"),
            ("cost_price", "REAL NOT NULL DEFAULT 0 CHECK(cost_price >= 0)"),
            ("supplier", "TEXT NOT NULL DEFAULT ''"),
            ("reorder_level", "INTEGER NOT NULL DEFAULT 3"),
        ):
            if column not in resource_columns:
                db.execute(f"ALTER TABLE resources ADD COLUMN {column} {declaration}")
        movement_columns = {row["name"] for row in db.execute("PRAGMA table_info(stock_movements)")}
        if "movement_type" not in movement_columns:
            db.execute("ALTER TABLE stock_movements ADD COLUMN movement_type TEXT NOT NULL DEFAULT 'restock'")
        sale_columns = {row["name"] for row in db.execute("PRAGMA table_info(sales)")}
        if "unit_price" not in sale_columns:
            db.execute("ALTER TABLE sales ADD COLUMN unit_price REAL NOT NULL DEFAULT 0")
            sale_columns.add("unit_price")
        if "unit_cost" not in sale_columns:
            db.execute("ALTER TABLE sales ADD COLUMN unit_cost REAL CHECK(unit_cost >= 0)")
            sale_columns.add("unit_cost")
        if "customer_id" not in sale_columns:
            db.execute("ALTER TABLE sales ADD COLUMN customer_id TEXT REFERENCES fellows(id)")
            db.execute("""UPDATE sales SET customer_id=(
                SELECT CASE WHEN COUNT(*)=1 THEN MIN(f.id) END FROM fellows f
                WHERE LOWER(TRIM(f.name))=LOWER(TRIM(sales.customer))
            ) WHERE TRIM(customer)<>''""")
            sale_columns.add("customer_id")
        if "sale_type" not in sale_columns:
            db.execute("ALTER TABLE sales ADD COLUMN sale_type TEXT NOT NULL DEFAULT 'cash'")
            sale_columns.add("sale_type")
        db.execute("""CREATE TABLE IF NOT EXISTS sale_payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sale_id INTEGER NOT NULL REFERENCES sales(id) ON DELETE CASCADE,
            amount REAL NOT NULL CHECK(amount > 0),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""")
        db.execute("CREATE INDEX IF NOT EXISTS sale_payment_sale ON sale_payments(sale_id)")
        loan_schema = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='loans'").fetchone()[0]
        if "damaged" not in loan_schema:
            db.executescript("""
                CREATE TABLE loans_new (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  fellow_id TEXT NOT NULL REFERENCES fellows(id),
                  resource_id TEXT NOT NULL REFERENCES resources(id),
                  quantity INTEGER NOT NULL CHECK(quantity > 0),
                  type TEXT NOT NULL CHECK(type IN ('borrow','return','damaged')),
                  due_date TEXT, loan_id INTEGER REFERENCES loans_new(id),
                  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
                INSERT INTO loans_new SELECT * FROM loans;
                DROP TABLE loans;
                ALTER TABLE loans_new RENAME TO loans;
                CREATE INDEX loan_resource_type ON loans(resource_id,type);
                CREATE INDEX loan_fellow_resource ON loans(fellow_id,resource_id);
            """)
        if seed_defaults and db.execute("SELECT COUNT(*) FROM resources").fetchone()[0] == 0:
            db.executemany("INSERT INTO resources(id,name,category,total) VALUES(?,?,?,?)", [
                ("R001", "Laptop", "Electronics", 10),
                ("R002", "Keyboard", "Accessories", 5),
                ("R003", "Headset", "Accessories", 3)])
        if seed_defaults and db.execute("SELECT COUNT(*) FROM fellows").fetchone()[0] == 0:
            db.executemany("INSERT INTO fellows VALUES(?,?)", [("F001", "Ada"), ("F002", "John"), ("F003", "Grace")])
        # Import the previous JSON store once. Leave an invalid or unrecognized
        # file untouched and use the clean starter data instead.
        legacy_path = Path("data.json")
        if not database_was_present and legacy_path.is_file():
            try:
                legacy = json.loads(legacy_path.read_text(encoding="utf-8"))
                saved_resources, records = legacy["resources"], legacy["borrow_records"]
                known_fellows = {"F001", "F002", "F003"}
                ids = set()
                valid = isinstance(saved_resources, list) and isinstance(records, list)
                if valid:
                    for item in saved_resources:
                        valid = (isinstance(item, dict) and all(k in item for k in ("id", "name", "category", "total", "available"))
                                 and isinstance(item["id"], str) and item["id"] not in ids
                                 and type(item["total"]) is int and item["total"] > 0
                                 and type(item["available"]) is int and 0 <= item["available"] <= item["total"])
                        if not valid:
                            break
                        ids.add(item["id"])
                balances = {}
                if valid:
                    for record in records:
                        valid = (isinstance(record, dict) and record.get("fellow_id") in known_fellows
                                 and record.get("resource_id") in ids and type(record.get("quantity")) is int
                                 and record["quantity"] > 0 and record.get("type") in ("borrow", "return"))
                        if not valid:
                            break
                        key = (record["fellow_id"], record["resource_id"])
                        balances[key] = balances.get(key, 0) + record["quantity"] * (1 if record["type"] == "borrow" else -1)
                        if balances[key] < 0:
                            valid = False
                            break
                    if valid:
                        for item in saved_resources:
                            outstanding_units = sum(qty for (fid, rid), qty in balances.items() if rid == item["id"])
                            if item["available"] != item["total"] - outstanding_units:
                                valid = False
                                break
                if valid:
                    db.execute("DELETE FROM stock_adjustments")
                    db.execute("DELETE FROM resources")
                    db.executemany("INSERT INTO resources(id,name,category,total) VALUES(?,?,?,?)", [(r["id"], r["name"], r["category"], r["total"]) for r in saved_resources])
                    borrow_ids = {}
                    for record in records:
                        due = (date.today() + timedelta(days=LOAN_DAYS)).isoformat() if record["type"] == "borrow" else None
                        timestamp = record.get("timestamp") or record.get("created_at")
                        if not isinstance(timestamp, str) or not timestamp.strip():
                            timestamp = current_timestamp()
                        cursor = db.execute("""INSERT INTO loans
                            (fellow_id,resource_id,quantity,type,due_date,created_at)
                            VALUES(?,?,?,?,?,?)""",
                            (record["fellow_id"], record["resource_id"], record["quantity"],
                             record["type"], due, timestamp))
                        key = (record["fellow_id"], record["resource_id"])
                        if record["type"] == "borrow":
                            borrow_ids.setdefault(key, []).append([cursor.lastrowid, record["quantity"]])
                        else:
                            remaining = record["quantity"]
                            for item_id in borrow_ids.get(key, []):
                                returned = min(remaining, item_id[1])
                                if returned:
                                    db.execute("UPDATE loans SET loan_id=? WHERE id=?", (item_id[0], cursor.lastrowid))
                                    item_id[1] -= returned
                                    remaining -= returned
                                if not remaining:
                                    break
            except (OSError, ValueError, KeyError, TypeError, sqlite3.IntegrityError):
                pass
        db.commit()
        db.execute("PRAGMA foreign_keys = ON")
    refresh_state()


def get_shop_settings():
    with connect() as db:
        row = db.execute("SELECT shop_name,brand_color,currency,logo_data FROM shop_settings WHERE id=1").fetchone()
    return dict(row) if row else {
        "shop_name": "ResourceHub", "brand_color": "#536dfe", "currency": "NGN", "logo_data": ""
    }


def update_shop_settings(shop_name, brand_color, currency, logo_data=""):
    shop_name = shop_name.strip() if isinstance(shop_name, str) else ""
    brand_color = brand_color.strip() if isinstance(brand_color, str) else ""
    currency = currency.strip().upper() if isinstance(currency, str) else ""
    if not shop_name or len(shop_name) > 60:
        return False, "Shop name must be between 1 and 60 characters."
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", brand_color):
        return False, "Choose a valid six-digit brand color."
    if not re.fullmatch(r"[A-Z]{3}", currency):
        return False, "Currency must be a three-letter code, such as MAD, USD, or EUR."
    if not isinstance(logo_data, str):
        return False, "Choose a PNG, JPEG, or WebP logo image."
    if logo_data:
        try:
            mime, encoded = logo_data.split(";base64,", 1)
            mime = mime.removeprefix("data:")
            content = base64.b64decode(encoded, validate=True)
        except (ValueError, base64.binascii.Error):
            return False, "The logo image could not be read."
        valid_image = (
            (mime == "image/png" and content.startswith(b"\x89PNG\r\n\x1a\n"))
            or (mime == "image/jpeg" and content.startswith(b"\xff\xd8\xff"))
            or (mime == "image/webp" and content.startswith(b"RIFF") and content[8:12] == b"WEBP")
        )
        if len(content) > 250_000 or not valid_image:
            return False, "Use a PNG, JPEG, or WebP logo no larger than 250 KB."
    backup_database()
    with connect() as db:
        db.execute("""UPDATE shop_settings SET shop_name=?,brand_color=?,currency=?,logo_data=? WHERE id=1""",
                   (shop_name, brand_color.lower(), currency, logo_data))
    return True, "Shop settings saved."


def refresh_state():
    with connect() as db:
        rows = db.execute("""SELECT r.*, r.total - COALESCE((SELECT SUM(l.quantity) FROM loans l
          WHERE l.resource_id=r.id AND l.type='borrow'),0) + COALESCE((SELECT SUM(l.quantity)
          FROM loans l WHERE l.resource_id=r.id AND l.type='return'),0) - COALESCE((SELECT SUM(s.quantity)
          FROM sales s WHERE s.resource_id=r.id),0) + COALESCE((SELECT SUM(a.quantity)
          FROM stock_adjustments a WHERE a.resource_id=r.id),0) AS available
          FROM resources r ORDER BY r.id""").fetchall()
        resources[:] = [dict(row) for row in rows]
        fellows.clear()
        fellows.update({row["id"]: row["name"] for row in db.execute("SELECT * FROM fellows ORDER BY id")})
        borrow_records[:] = [dict(row) for row in db.execute("SELECT fellow_id,resource_id,quantity,type,due_date FROM loans ORDER BY id")]


def save_data():
    """Compatibility hook; SQLite commits every operation directly."""
    refresh_state()


def _available_quantity(db, resource_id, total):
    loan_balance = db.execute("""SELECT COALESCE(SUM(CASE
      WHEN type='borrow' THEN quantity WHEN type='return' THEN -quantity ELSE 0 END),0)
      FROM loans WHERE resource_id=?""", (resource_id,)).fetchone()[0]
    sold = db.execute("SELECT COALESCE(SUM(quantity),0) FROM sales WHERE resource_id=?",
                      (resource_id,)).fetchone()[0]
    adjusted = db.execute("SELECT COALESCE(SUM(quantity),0) FROM stock_adjustments WHERE resource_id=?",
                          (resource_id,)).fetchone()[0]
    return total - loan_balance - sold + adjusted


def apply_stocktake(counts, note):
    """Reconcile counted on-shelf stock to available units, keeping a signed ledger."""
    if not isinstance(counts, list) or not counts or len(counts) > 500:
        return False, "Count at least one item before saving the stocktake.", []
    if not isinstance(note, str) or not note.strip() or len(note.strip()) > 200:
        return False, "Add a stocktake note of 1 to 200 characters.", []

    normalized = []
    seen = set()
    for entry in counts:
        if not isinstance(entry, dict) or not isinstance(entry.get("resource_id"), str):
            return False, "Each stocktake row needs a valid item ID.", []
        rid = entry["resource_id"].strip().upper()
        expected, counted = entry.get("expected"), entry.get("counted")
        if (not rid or type(expected) is not int
                or type(counted) is not int or counted < 0):
            return False, "Counted quantities must be non-negative whole numbers.", []
        if rid in seen:
            return False, "An item can appear only once in a stocktake.", []
        seen.add(rid)
        normalized.append((rid, expected, counted))

    backup_database()
    changes = []
    stocktake_note = note.strip()
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        rows = []
        for rid, expected, counted in normalized:
            resource = db.execute("SELECT id,name,total FROM resources WHERE id=?", (rid,)).fetchone()
            if not resource:
                return False, f"Unknown item ID: {rid}", []
            current = _available_quantity(db, rid, resource["total"])
            if current != expected:
                return False, (f"Inventory changed for {resource['name']} while the stocktake was open. "
                               "Refresh and recount before saving."), []
            rows.append((resource, expected, counted))

        now = current_timestamp()
        for resource, expected, counted in rows:
            delta = counted - expected
            if delta:
                db.execute("""INSERT INTO stock_adjustments(resource_id,quantity,note,created_at)
                    VALUES(?,?,?,?)""", (resource["id"], delta, stocktake_note, now))
                changes.append({"resource_id": resource["id"], "name": resource["name"],
                                "expected": expected, "counted": counted, "delta": delta})

    refresh_state()
    if changes:
        return True, f"Stocktake saved with adjustments for {len(changes)} item(s).", changes
    return True, "Stocktake saved. No stock adjustments were needed.", changes


def load_data():
    init_db()


def find_resource(resource_id):
    rid = str(resource_id).strip().upper()
    return next((item for item in resources if item["id"] == rid), None)


def outstanding(fellow_id, resource_id):
    with connect() as db:
        return _outstanding(db, fellow_id.strip().upper(), resource_id.strip().upper())


def _outstanding(db, fellow_id, resource_id):
    row = db.execute("""SELECT COALESCE(SUM(CASE WHEN type='borrow' THEN quantity ELSE -quantity END),0)
        FROM loans WHERE fellow_id=? AND resource_id=?""", (fellow_id, resource_id)).fetchone()
    return row[0]


def add_resource(resource_id, name, category, total, sku="", price=0, supplier="", reorder_level=3, cost_price=0):
    rid, name, category = resource_id.strip().upper(), name.strip(), category.strip()
    if not rid or not name or not category:
        return False, "ID, name and category cannot be empty."
    if type(total) is not int or total <= 0:
        return False, "Total units must be a positive whole number."
    if not isinstance(sku, str) or not isinstance(supplier, str):
        return False, "SKU and supplier must be text."
    if type(price) not in (int, float) or not math.isfinite(price) or price < 0:
        return False, "Selling price must be a non-negative number."
    if type(cost_price) not in (int, float) or not math.isfinite(cost_price) or cost_price < 0:
        return False, "Cost price must be a non-negative number."
    if type(reorder_level) is not int or reorder_level < 0:
        return False, "Restock threshold must be a non-negative whole number."
    backup_warning = ""
    try:
        backup_database()
    except (OSError, sqlite3.Error):
        # A backup failure should be reported, but must not prevent the owner
        # from adding stock when the shop database itself is still writable.
        backup_warning = " The safety backup could not be created."
    try:
        with connect() as db:
            db.execute("""INSERT INTO resources(id,name,category,total,sku,price,cost_price,supplier,reorder_level)
                VALUES(?,?,?,?,?,?,?,?,?)""",
                (rid, name, category, total, sku.strip(), float(price), float(cost_price), supplier.strip(), reorder_level))
            db.execute("""INSERT INTO stock_movements(resource_id,quantity,movement_type,created_at)
                VALUES(?,?,'opening_stock',?)""", (rid, total, current_timestamp()))
    except sqlite3.IntegrityError:
        return False, f"Resource ID {rid} already exists."
    refresh_state()
    return True, f"Added {name} ({rid}) with {total} unit(s).{backup_warning}"


def update_resource_cost_price(resource_id, cost_price):
    """Update the purchase cost used for profit calculations."""
    rid = str(resource_id).strip().upper()
    if type(cost_price) not in (int, float) or not math.isfinite(cost_price) or cost_price < 0:
        return False, "Cost price must be a non-negative number."
    with connect() as db:
        if not db.execute("SELECT 1 FROM resources WHERE id=?", (rid,)).fetchone():
            return False, f"Unknown item ID: {rid}"
    backup_database()
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("UPDATE resources SET cost_price=? WHERE id=?", (float(cost_price), rid))
    refresh_state()
    return True, f"Updated cost price for {rid}."


def update_resource_details(resource_id, sku, price, supplier, reorder_level):
    rid = str(resource_id).strip().upper()
    if not isinstance(sku, str) or not isinstance(supplier, str):
        return False, "SKU and supplier must be text."
    if type(price) not in (int, float) or not math.isfinite(price) or price < 0:
        return False, "Price must be a non-negative number."
    if type(reorder_level) is not int or reorder_level < 0:
        return False, "Restock threshold must be a non-negative whole number."
    backup_database()
    with connect() as db:
        if not db.execute("SELECT 1 FROM resources WHERE id=?", (rid,)).fetchone():
            return False, f"Unknown resource ID: {rid}"
        db.execute("""UPDATE resources SET sku=?,price=?,supplier=?,reorder_level=? WHERE id=?""",
                   (sku.strip(), float(price), supplier.strip(), reorder_level, rid))
    refresh_state()
    return True, f"Updated product details for {rid}."


def delete_resource(resource_id):
    """Delete a product only when it has no loan or sales history."""
    rid = str(resource_id).strip().upper()
    with connect() as db:
        resource = db.execute("SELECT name FROM resources WHERE id=?", (rid,)).fetchone()
        if not resource:
            return False, f"Unknown resource ID: {rid}"
        has_history = db.execute(
            "SELECT EXISTS(SELECT 1 FROM loans WHERE resource_id=?) "
            "OR EXISTS(SELECT 1 FROM sales WHERE resource_id=?) "
            "OR EXISTS(SELECT 1 FROM stock_adjustments WHERE resource_id=?)",
            (rid, rid, rid),
        ).fetchone()[0]
        if has_history:
            return False, "This product has loan, sales, or stocktake history and cannot be deleted."
        backup_database()
        db.execute("DELETE FROM stock_movements WHERE resource_id=?", (rid,))
        db.execute("DELETE FROM stock_adjustments WHERE resource_id=?", (rid,))
        db.execute("DELETE FROM resources WHERE id=?", (rid,))
    refresh_state()
    return True, f"Deleted {resource['name']} ({rid})."


def restock_resource(resource_id, quantity):
    """Add newly received units to an existing resource's stock."""
    rid = str(resource_id).strip().upper()
    if type(quantity) is not int or quantity <= 0:
        return False, "Restock quantity must be a positive whole number."
    backup_database()
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        resource = db.execute("SELECT name FROM resources WHERE id=?", (rid,)).fetchone()
        if not resource:
            return False, f"Unknown resource ID: {rid}"
        db.execute("UPDATE resources SET total=total+? WHERE id=?", (quantity, rid))
        db.execute("INSERT INTO stock_movements(resource_id,quantity,created_at) VALUES(?,?,?)",
                   (rid, quantity, current_timestamp()))
    refresh_state()
    return True, f"Added {quantity} unit(s) of {resource['name']} to stock."


def record_sale(resource_id, quantity, customer=""):
    """Record a sale that permanently reduces the item's available stock."""
    rid = str(resource_id).strip().upper()
    customer = str(customer).strip()
    if type(quantity) is not int or quantity <= 0:
        return False, "Quantity must be a positive whole number."
    if len(customer) > 100:
        return False, "Customer name must be 100 characters or fewer."
    backup_database()
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        resource = db.execute("SELECT name,total,price,cost_price FROM resources WHERE id=?", (rid,)).fetchone()
        if not resource:
            return False, f"Unknown resource ID: {rid}"
        available = _available_quantity(db, rid, resource["total"])
        if quantity > available:
            return False, f"Only {available} unit(s) of {resource['name']} remain in stock; you asked for {quantity}."
        unit_cost = resource["cost_price"] if resource["cost_price"] > 0 else None
        db.execute("""INSERT INTO sales(resource_id,quantity,unit_price,unit_cost,customer,created_at)
                   VALUES(?,?,?,?,?,?)""",
                   (rid, quantity, resource["price"], unit_cost, customer, current_timestamp()))
    refresh_state()
    who = f" for {customer}" if customer else ""
    return True, f"Recorded sale of {quantity} x {resource['name']}{who}."


def record_credit_sale(customer_id, resource_id, quantity, amount_paid):
    """Record a customer sale and its first payment as one atomic change."""
    fid = str(customer_id).strip().upper()
    rid = str(resource_id).strip().upper()
    if type(quantity) is not int or quantity <= 0:
        return False, "Quantity must be a positive whole number."
    try:
        paid = Decimal(str(amount_paid))
        if not paid.is_finite() or paid < 0 or paid != paid.quantize(Decimal("0.01")):
            raise InvalidOperation
    except (InvalidOperation, ValueError):
        return False, "Amount received must be a non-negative amount with up to 2 decimal places."

    backup_database()
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        customer = db.execute("SELECT name FROM fellows WHERE id=?", (fid,)).fetchone()
        if not customer:
            return False, "Choose an existing customer."
        resource = db.execute("SELECT name,total,price,cost_price FROM resources WHERE id=?", (rid,)).fetchone()
        if not resource:
            return False, f"Unknown item ID: {rid}"
        available = _available_quantity(db, rid, resource["total"])
        if quantity > available:
            return False, f"Only {available} unit(s) of {resource['name']} remain in stock; you asked for {quantity}."

        total = (Decimal(str(resource["price"])) * quantity).quantize(Decimal("0.01"))
        if paid > total:
            return False, f"Amount received cannot exceed the sale total of NGN {total:.2f}."
        now = current_timestamp()
        unit_cost = resource["cost_price"] if resource["cost_price"] > 0 else None
        cursor = db.execute("""INSERT INTO sales
          (resource_id,quantity,unit_price,unit_cost,customer,customer_id,sale_type,created_at)
          VALUES(?,?,?,?,?,?,'credit',?)""",
          (rid, quantity, resource["price"], unit_cost, customer["name"], fid, now))
        if paid > 0:
            db.execute("INSERT INTO sale_payments(sale_id,amount,created_at) VALUES(?,?,?)",
                       (cursor.lastrowid, float(paid), now))

    refresh_state()
    due = total - paid
    status = "paid in full" if due == 0 else ("part-paid" if paid > 0 else "unpaid")
    return True, f"Recorded {status} sale of {quantity} x {resource['name']} for {customer['name']}; NGN {due:.2f} remains owed."


def record_sale_payment(sale_id, amount):
    """Add a payment to an unpaid credit sale without allowing overpayment."""
    try:
        sale_id = int(sale_id)
        payment = Decimal(str(amount))
        if sale_id <= 0 or not payment.is_finite() or payment <= 0 or payment != payment.quantize(Decimal("0.01")):
            raise InvalidOperation
    except (InvalidOperation, ValueError, TypeError):
        return False, "Enter a valid payment amount greater than zero."

    backup_database()
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        sale = db.execute("""SELECT quantity,unit_price,sale_type FROM sales WHERE id=?""", (sale_id,)).fetchone()
        if not sale or sale["sale_type"] != "credit":
            return False, "That credit sale was not found."
        total = (Decimal(str(sale["unit_price"])) * sale["quantity"]).quantize(Decimal("0.01"))
        paid = Decimal(str(db.execute(
            "SELECT COALESCE(SUM(amount),0) FROM sale_payments WHERE sale_id=?", (sale_id,)
        ).fetchone()[0])).quantize(Decimal("0.01"))
        due = total - paid
        if payment > due:
            return False, f"Payment is more than the NGN {due:.2f} still owed."
        db.execute("INSERT INTO sale_payments(sale_id,amount,created_at) VALUES(?,?,?)",
                   (sale_id, float(payment), current_timestamp()))

    refresh_state()
    remaining = due - payment
    return True, f"Payment recorded. NGN {remaining:.2f} remains owed."


def record_expense(category, description, amount):
    """Save one positive shop expense with a short category and description."""
    category = str(category).strip()
    description = str(description).strip()
    if not category or len(category) > 60:
        return False, "Choose an expense category."
    if len(description) > 200:
        return False, "Expense note must be 200 characters or fewer."
    try:
        value = Decimal(str(amount))
        if not value.is_finite() or value <= 0 or value != value.quantize(Decimal("0.01")):
            raise InvalidOperation
    except (InvalidOperation, ValueError):
        return False, "Enter an expense amount greater than zero with up to 2 decimal places."

    backup_database()
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("INSERT INTO expenses(category,description,amount,created_at) VALUES(?,?,?,?)",
                   (category, description, float(value), current_timestamp()))
    refresh_state()
    return True, f"Recorded expense of NGN {value:.2f}."


def add_fellow(fellow_id, name):
    fid, name = fellow_id.strip().upper(), name.strip()
    if not fid or not name:
        return False, "Customer ID and name cannot be empty."
    backup_database()
    try:
        with connect() as db:
            db.execute("INSERT INTO fellows VALUES(?,?)", (fid, name))
    except sqlite3.IntegrityError:
        return False, f"Customer ID {fid} already exists."
    refresh_state()
    return True, f"Added customer {name} ({fid})."


def remove_fellow(fellow_id):
    fid = fellow_id.strip().upper()
    backup_database()
    with connect() as db:
        if not db.execute("SELECT 1 FROM fellows WHERE id=?", (fid,)).fetchone():
            return False, f"Unknown fellow ID: {fid}"
        if db.execute("SELECT 1 FROM loans WHERE fellow_id=? LIMIT 1", (fid,)).fetchone():
            return False, "Cannot remove a fellow with loan history."
        db.execute("DELETE FROM fellows WHERE id=?", (fid,))
    refresh_state()
    return True, f"Removed fellow {fid}."


def _fellow_has_loans(db, fid):
    return db.execute("""SELECT 1 FROM loans WHERE fellow_id=? GROUP BY resource_id
      HAVING SUM(CASE WHEN type='borrow' THEN quantity ELSE -quantity END)>0 LIMIT 1""", (fid,)).fetchone()


def list_resources():
    print(f"{'ID':<6}{'Name':<14}{'Category':<14}{'Total':<7}{'Available'}")
    if not resources:
        print("No resources found.")
    for r in resources:
        print(f"{r['id']:<6}{r['name']:<14}{r['category']:<14}{r['total']:<7}{r['available']}")


def list_fellows():
    print("Fellows:")
    if not fellows:
        print("  None")
    for fid, name in fellows.items():
        print(f"  {fid}: {name}")


def borrow_history(fellow_id):
    """Print every recorded borrow and return for one fellow, oldest first."""
    fid = fellow_id.strip().upper()
    with connect() as db:
        fellow = db.execute("SELECT name FROM fellows WHERE id=?", (fid,)).fetchone()
        if fellow is None:
            print(f"Unknown fellow ID: {fid}")
            return
        records = db.execute("""SELECT l.type, l.quantity, r.name AS resource_name,
            l.due_date, l.created_at FROM loans l JOIN resources r ON r.id=l.resource_id
            WHERE l.fellow_id=? ORDER BY l.id""", (fid,)).fetchall()

    print(f"Borrow history for {fellow['name']} ({fid}):")
    if not records:
        print("  No borrow or return records.")
        return
    for record in records:
        action = ("Borrowed" if record["type"] == "borrow" else
                  "Marked damaged/lost" if record["type"] == "damaged" else "Returned")
        detail = f"  {action} {record['quantity']} x {record['resource_name']}"
        if record["due_date"]:
            detail += f" (due {record['due_date']})"
        detail += f" — {record['created_at']}"
        print(detail)


def borrow(fellow_id, resource_id, quantity, due_date=None):
    fid, rid = fellow_id.strip().upper(), resource_id.strip().upper()
    if type(quantity) is not int or quantity <= 0:
        return False, "Quantity must be a positive whole number."
    try:
        due = date.fromisoformat(due_date) if due_date else date.today() + timedelta(days=LOAN_DAYS)
    except (TypeError, ValueError):
        return False, "Due date must be a valid date in YYYY-MM-DD format."
    if due < date.today():
        return False, "Due date cannot be in the past."
    backup_database()
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        fellow = db.execute("SELECT name FROM fellows WHERE id=?", (fid,)).fetchone()
        if not fellow:
            return False, f"Unknown fellow ID: {fid}"
        held_units = db.execute("""SELECT COALESCE(SUM(CASE WHEN type='borrow' THEN quantity ELSE -quantity END),0)
            FROM loans WHERE fellow_id=?""", (fid,)).fetchone()[0]
        if held_units + quantity > BORROW_LIMIT:
            remaining = BORROW_LIMIT - held_units
            if remaining <= 0:
                return False, f"{fellow['name']} already holds the maximum of {BORROW_LIMIT} units. Return an item before borrowing more."
            return False, (f"{fellow['name']} may borrow at most {BORROW_LIMIT} units total; they hold "
                           f"{held_units} and can borrow {remaining} more.")
        resource = db.execute("SELECT name,total FROM resources WHERE id=?", (rid,)).fetchone()
        if not resource:
            return False, f"Unknown resource ID: {rid}"
        available = _available_quantity(db, rid, resource["total"])
        if quantity > available:
            return False, (f"Only {available} unit(s) of {resource['name']} available; you asked for {quantity}.")
        db.execute("""INSERT INTO loans
            (fellow_id,resource_id,quantity,type,due_date,created_at)
            VALUES(?,?,?,'borrow',?,?)""",
            (fid, rid, quantity, due.isoformat(), current_timestamp()))
    refresh_state()
    return True, f"{fellow['name']} borrowed {quantity} x {resource['name']} (due {due.isoformat()})."


def update_loan_due_date(loan_id, due_date):
    if type(loan_id) is not int or loan_id <= 0:
        return False, "Loan ID must be a positive whole number."
    try:
        due = date.fromisoformat(due_date)
    except (TypeError, ValueError):
        return False, "Due date must be a valid date in YYYY-MM-DD format."
    if due < date.today():
        return False, "Due date cannot be in the past."
    backup_database()
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        loan = db.execute("""SELECT b.id,b.fellow_id,f.name AS fellow_name,b.resource_id,
            r.name AS resource_name,b.due_date,
            b.quantity-COALESCE(SUM(ret.quantity),0) AS outstanding_quantity
            FROM loans b JOIN fellows f ON f.id=b.fellow_id
            JOIN resources r ON r.id=b.resource_id
            LEFT JOIN loans ret ON ret.loan_id=b.id AND ret.type IN ('return','damaged')
            WHERE b.id=? AND b.type='borrow' GROUP BY b.id
            HAVING outstanding_quantity>0""", (loan_id,)).fetchone()
        if not loan:
            return False, "That loan is no longer outstanding. Refresh the page and try again."
        previous_due = loan["due_date"] or "not set"
        db.execute("UPDATE loans SET due_date=? WHERE id=? AND type='borrow'", (due.isoformat(), loan_id))
    refresh_state()
    return True, (f"Due date for {loan['fellow_name']}'s {loan['resource_name']} updated "
                  f"from {previous_due} to {due.isoformat()}.")


def return_item(fellow_id, resource_id, quantity, damaged=False, loan_id=None):
    fid, rid = fellow_id.strip().upper(), resource_id.strip().upper()
    if type(quantity) is not int or quantity <= 0:
        return False, "Quantity must be a positive whole number."
    if type(damaged) is not bool:
        return False, "Damaged status must be yes or no."
    if loan_id is not None and (type(loan_id) is not int or loan_id <= 0):
        return False, "Loan ID must be a positive whole number."
    backup_database()
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        fellow = db.execute("SELECT name FROM fellows WHERE id=?", (fid,)).fetchone()
        if not fellow:
            return False, f"Unknown fellow ID: {fid}"
        resource = db.execute("SELECT name FROM resources WHERE id=?", (rid,)).fetchone()
        if not resource:
            return False, f"Unknown resource ID: {rid}"
        held = _outstanding(db, fid, rid)
        if quantity > held:
            return False, f"{fellow['name']} holds only {held} unit(s) of {resource['name']}; you asked to return {quantity}."
        # Allocate returns against the oldest open borrow records; partial returns retain their original due date.
        open_loans = db.execute("""SELECT b.id,b.quantity-COALESCE(SUM(r.quantity),0) AS remaining
          FROM loans b LEFT JOIN loans r ON r.loan_id=b.id AND r.type IN ('return','damaged')
          WHERE b.type='borrow' AND b.fellow_id=? AND b.resource_id=?
            AND (? IS NULL OR b.id=?) GROUP BY b.id
          HAVING remaining>0 ORDER BY b.id""", (fid, rid, loan_id, loan_id)).fetchall()
        if not open_loans:
            return False, "That loan is no longer outstanding. Refresh the page and try again."
        if loan_id is not None and quantity > open_loans[0]["remaining"]:
            remaining_in_loan = open_loans[0]["remaining"]
            return False, f"That loan has only {remaining_in_loan} unit(s) outstanding; you asked to return {quantity}."
        remaining = quantity
        for loan in open_loans:
            returned = min(remaining, loan["remaining"])
            event_type = "damaged" if damaged else "return"
            db.execute("""INSERT INTO loans
                (fellow_id,resource_id,quantity,type,loan_id,created_at)
                VALUES(?,?,?,?,?,?)""",
                (fid, rid, returned, event_type, loan["id"], current_timestamp()))
            remaining -= returned
            if not remaining:
                break
    refresh_state()
    if damaged:
        return True, f"Marked {quantity} x {resource['name']} as damaged or lost; they remain unavailable."
    return True, f"{fellow['name']} returned {quantity} x {resource['name']}."


def search_by_name(term):
    term = term.strip().lower()
    return [r for r in resources if term in r["name"].lower()]


def filter_by_category(category):
    category = category.strip().lower()
    return [r for r in resources if r["category"].lower() == category]


def report_data():
    total = sum(r["total"] for r in resources)
    available = sum(r["available"] for r in resources)
    with connect() as db:
        damaged_rows = db.execute("SELECT resource_id,SUM(quantity) AS quantity FROM loans WHERE type='damaged' GROUP BY resource_id").fetchall()
        loan_rows = db.execute("""SELECT resource_id,SUM(CASE WHEN type='borrow' THEN quantity
            WHEN type IN ('return','damaged') THEN -quantity ELSE 0 END) AS quantity
            FROM loans GROUP BY resource_id""").fetchall()
    damaged_by_resource = {row["resource_id"]: row["quantity"] for row in damaged_rows}
    damaged = sum(damaged_by_resource.values())
    on_loan_by_resource = {row["resource_id"]: row["quantity"] for row in loan_rows}
    on_loan = {r["id"]: on_loan_by_resource.get(r["id"], 0) for r in resources}
    highest = max(on_loan.values(), default=0)
    return {"total_units": total, "available_units": available, "borrowed_units": sum(on_loan.values()),
            "damaged_units": damaged,
            "low_stock": [r for r in resources
                          if r["available"] == 0 or r["available"] <= r["reorder_level"]],
            "most_borrowed": [dict(r, borrowed=on_loan[r["id"]])
                              for r in resources if highest and on_loan[r["id"]] == highest]}


def overdue_items(as_of=None):
    current_day = as_of or date.today()
    today = current_day.isoformat()
    with connect() as db:
        items = [dict(row) for row in db.execute("""SELECT b.fellow_id,f.name AS fellow_name,b.resource_id,
          r.name AS resource_name,b.due_date,b.quantity-COALESCE(SUM(ret.quantity),0) AS quantity
          FROM loans b JOIN fellows f ON f.id=b.fellow_id JOIN resources r ON r.id=b.resource_id
          LEFT JOIN loans ret ON ret.loan_id=b.id AND ret.type IN ('return','damaged')
          WHERE b.type='borrow' AND b.due_date<? GROUP BY b.id HAVING quantity>0
          ORDER BY b.due_date,b.fellow_id,b.resource_id""", (today,))]
    for item in items:
        due = date.fromisoformat(item["due_date"])
        item["days_late"] = (current_day - due).days
    return items


def report(as_of=None):
    data = report_data()
    print(f"Total units: {data['total_units']}")
    print(f"Available units: {data['available_units']}")
    print(f"Borrowed units: {data['borrowed_units']}")
    print(f"Damaged or lost units: {data['damaged_units']}")
    print("Low stock (at or below each item's restock threshold):")
    for r in data["low_stock"] or [None]:
        print(f"  {r['name']} ({r['available']})" if r else "  None")
    print("Most borrowed:")
    for r in data["most_borrowed"] or [None]:
        print(f"  {r['name']} ({r['borrowed']})" if r else "  Nothing is currently borrowed")
    print("Overdue items:")
    for loan in overdue_items(as_of=as_of) or [None]:
        print(f"  {loan['resource_name']} x{loan['quantity']} — {loan['fellow_name']} ({loan['fellow_id']}), due {loan['due_date']} ({loan['days_late']} days late)" if loan else "  None")


def export_report_csv(path="report.csv"):
    data = report_data()
    overdue = overdue_items()
    with open(path, "w", newline="", encoding="utf-8") as output:
        writer = csv.writer(output)
        writer.writerow(["section", "resource_id", "resource_name", "total", "available", "quantity", "fellow_id", "fellow_name", "due_date"])
        for section, rows in (("inventory", resources), ("low_stock", data["low_stock"]), ("most_borrowed", data["most_borrowed"])):
            for r in rows:
                writer.writerow([section, r["id"], r["name"], r["total"], r["available"], r["total"]-r["available"], "", "", ""])
        for loan in overdue:
            writer.writerow(["overdue", loan["resource_id"], loan["resource_name"], "", "", loan["quantity"], loan["fellow_id"], loan["fellow_name"], loan["due_date"]])
    return path


def read_positive_int(prompt):
    while True:
        text = input(prompt).strip()
        if text.isdigit() and int(text) > 0:
            return int(text)
        print("Please enter a whole number greater than 0.")


def read_text(prompt):
    while True:
        text = input(prompt).strip()
        if text:
            return text
        print("This cannot be empty.")


def persist_changes():
    refresh_state()


def menu():
    while True:
        print("\n=== ResourceHub ===")
        for option, label in enumerate(("List resources", "Add resource", "Borrow", "Return", "Search by name", "Filter by category", "Report", "Add fellow", "Remove fellow", "List fellows", "Export report to CSV", "Borrow history by fellow", "Return damaged/lost items"), 1):
            print(f"{option}. {label}")
        print("0. Exit")
        try:
            choice = input("Choose an option: ").strip()
        except EOFError:
            print("\nGoodbye!")
            break
        if choice == "1": list_resources()
        elif choice == "2":
            ok, msg = add_resource(read_text("Resource ID: "), read_text("Name: "), read_text("Category: "), read_positive_int("Total units: ")); print(msg)
        elif choice in ("3", "4"):
            fid, rid, qty = read_text("Fellow ID: "), read_text("Resource ID: "), read_positive_int("Quantity: ")
            result = borrow(fid, rid, qty, input(f"Due date YYYY-MM-DD (default {date.today()+timedelta(days=LOAN_DAYS)}): ").strip() or None) if choice == "3" else return_item(fid, rid, qty)
            print(result[1])
        elif choice == "5":
            for r in search_by_name(read_text("Name to search: ")): print(r["id"], r["name"], r["category"], r["available"])
        elif choice == "6":
            for r in filter_by_category(read_text("Category: ")): print(r["id"], r["name"], r["category"], r["available"])
        elif choice == "7": report()
        elif choice == "8": print(add_fellow(read_text("Fellow ID: "), read_text("Name: "))[1])
        elif choice == "9": print(remove_fellow(read_text("Fellow ID: "))[1])
        elif choice == "10": list_fellows()
        elif choice == "11":
            path = input("CSV path (default report.csv): ").strip() or "report.csv"
            print(f"Report exported to {export_report_csv(path)}")
        elif choice == "12": borrow_history(read_text("Fellow ID: "))
        elif choice == "13":
            fid, rid, qty = read_text("Fellow ID: "), read_text("Resource ID: "), read_positive_int("Quantity: ")
            print(return_item(fid, rid, qty, damaged=True)[1])
        elif choice == "0": print("Goodbye!"); break
        else: print("Invalid option. Please choose 0-13.")


if __name__ == "__main__":
    load_data()
    menu()
