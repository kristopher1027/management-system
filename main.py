"""ResourceHub campus equipment manager backed by Python's standard library."""
import csv
import json
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

DB_FILE = "campus_resources.db"
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


def init_db():
    database_was_present = Path(DB_FILE).exists()
    with connect() as db:
        # Rebuild the old loan table once so it can also store damaged returns.
        db.execute("PRAGMA foreign_keys = OFF")
        db.executescript("""
            CREATE TABLE IF NOT EXISTS resources (
              id TEXT PRIMARY KEY, name TEXT NOT NULL, category TEXT NOT NULL,
              total INTEGER NOT NULL CHECK(total > 0));
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
        """)
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
        if db.execute("SELECT COUNT(*) FROM resources").fetchone()[0] == 0:
            db.executemany("INSERT INTO resources VALUES(?,?,?,?)", [
                ("R001", "Laptop", "Electronics", 10),
                ("R002", "Keyboard", "Accessories", 5),
                ("R003", "Headset", "Accessories", 3)])
        if db.execute("SELECT COUNT(*) FROM fellows").fetchone()[0] == 0:
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
                    db.execute("DELETE FROM resources")
                    db.executemany("INSERT INTO resources VALUES(?,?,?,?)", [(r["id"], r["name"], r["category"], r["total"]) for r in saved_resources])
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


def refresh_state():
    with connect() as db:
        rows = db.execute("""SELECT r.*, r.total - COALESCE((SELECT SUM(l.quantity) FROM loans l
          WHERE l.resource_id=r.id AND l.type='borrow'),0) + COALESCE((SELECT SUM(l.quantity)
          FROM loans l WHERE l.resource_id=r.id AND l.type='return'),0) AS available
          FROM resources r ORDER BY r.id""").fetchall()
        resources[:] = [dict(row) for row in rows]
        fellows.clear()
        fellows.update({row["id"]: row["name"] for row in db.execute("SELECT * FROM fellows ORDER BY id")})
        borrow_records[:] = [dict(row) for row in db.execute("SELECT fellow_id,resource_id,quantity,type,due_date FROM loans ORDER BY id")]


def save_data():
    """Compatibility hook; SQLite commits every operation directly."""
    refresh_state()


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


def add_resource(resource_id, name, category, total):
    rid, name, category = resource_id.strip().upper(), name.strip(), category.strip()
    if not rid or not name or not category:
        return False, "ID, name and category cannot be empty."
    if type(total) is not int or total <= 0:
        return False, "Total units must be a positive whole number."
    try:
        with connect() as db:
            db.execute("INSERT INTO resources VALUES(?,?,?,?)", (rid, name, category, total))
    except sqlite3.IntegrityError:
        return False, f"Resource ID {rid} already exists."
    refresh_state()
    return True, f"Added {name} ({rid}) with {total} unit(s)."


def add_fellow(fellow_id, name):
    fid, name = fellow_id.strip().upper(), name.strip()
    if not fid or not name:
        return False, "Fellow ID and name cannot be empty."
    try:
        with connect() as db:
            db.execute("INSERT INTO fellows VALUES(?,?)", (fid, name))
    except sqlite3.IntegrityError:
        return False, f"Fellow ID {fid} already exists."
    refresh_state()
    return True, f"Added fellow {name} ({fid})."


def remove_fellow(fellow_id):
    fid = fellow_id.strip().upper()
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
        available = resource["total"] - db.execute("""SELECT COALESCE(SUM(CASE
          WHEN type='borrow' THEN quantity WHEN type='return' THEN -quantity ELSE 0 END),0)
          FROM loans WHERE resource_id=?""", (rid,)).fetchone()[0]
        if quantity > available:
            return False, (f"Only {available} unit(s) of {resource['name']} available; you asked for {quantity}.")
        db.execute("""INSERT INTO loans
            (fellow_id,resource_id,quantity,type,due_date,created_at)
            VALUES(?,?,?,'borrow',?,?)""",
            (fid, rid, quantity, due.isoformat(), current_timestamp()))
    refresh_state()
    return True, f"{fellow['name']} borrowed {quantity} x {resource['name']} (due {due.isoformat()})."


def return_item(fellow_id, resource_id, quantity, damaged=False):
    fid, rid = fellow_id.strip().upper(), resource_id.strip().upper()
    if type(quantity) is not int or quantity <= 0:
        return False, "Quantity must be a positive whole number."
    if type(damaged) is not bool:
        return False, "Damaged status must be yes or no."
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
          WHERE b.type='borrow' AND b.fellow_id=? AND b.resource_id=? GROUP BY b.id
          HAVING remaining>0 ORDER BY b.id""", (fid, rid)).fetchall()
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
    damaged_by_resource = {row["resource_id"]: row["quantity"] for row in damaged_rows}
    damaged = sum(damaged_by_resource.values())
    on_loan = {r["id"]: r["total"] - r["available"] - damaged_by_resource.get(r["id"], 0) for r in resources}
    highest = max(on_loan.values(), default=0)
    return {"total_units": total, "available_units": available, "borrowed_units": total-available-damaged,
            "damaged_units": damaged,
            "low_stock": [r for r in resources if r["available"] < 3],
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
    print("Low stock (fewer than 3 available):")
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
