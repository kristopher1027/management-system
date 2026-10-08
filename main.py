# Learn2Earn Campus Resource Management System (standard library only)
import json
import os
import tempfile

resources = [
    {"id": "R001", "name": "Laptop", "category": "Electronics", "total": 10, "available": 10},
    {"id": "R002", "name": "Keyboard", "category": "Accessories", "total": 5, "available": 5},
    {"id": "R003", "name": "Headset", "category": "Accessories", "total": 3, "available": 3},
]
fellows = {"F001": "Ada", "F002": "John", "F003": "Grace"}
borrow_records = []

DATA_FILE = "data.json"


def save_data():
    """Write inventory and loan records safely, without leaving half a file."""
    data = {"resources": resources, "borrow_records": borrow_records}
    directory = os.path.dirname(os.path.abspath(DATA_FILE))
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=directory,
                                         delete=False) as f:
            temp_path = f.name
            json.dump(data, f, indent=2)
            f.write("\n")
        os.replace(temp_path, DATA_FILE)
    finally:
        if temp_path and os.path.exists(temp_path):
            os.remove(temp_path)


def valid_saved_data(data):
    """Check saved values before allowing them to replace the live inventory."""
    if not isinstance(data, dict):
        return False
    saved_resources = data.get("resources")
    records = data.get("borrow_records")
    if not isinstance(saved_resources, list) or not isinstance(records, list):
        return False

    ids = set()
    balances = {}
    for item in saved_resources:
        if not isinstance(item, dict) or not all(
                key in item for key in ("id", "name", "category", "total", "available")):
            return False
        rid = item["id"]
        if (not isinstance(rid, str) or not rid or rid != rid.upper()
                or rid in ids or not isinstance(item["name"], str)
                or not isinstance(item["category"], str)
                or type(item["total"]) is not int or item["total"] <= 0
                or type(item["available"]) is not int
                or not 0 <= item["available"] <= item["total"]):
            return False
        ids.add(rid)
        balances[rid] = {}

    for record in records:
        if not isinstance(record, dict) or not all(
                key in record for key in ("fellow_id", "resource_id", "quantity", "type")):
            return False
        fid, rid = record["fellow_id"], record["resource_id"]
        qty, kind = record["quantity"], record["type"]
        if (not isinstance(fid, str) or not isinstance(rid, str)
                or fid not in fellows or rid not in ids
                or type(qty) is not int or qty <= 0
                or kind not in ("borrow", "return")):
            return False
        person_balances = balances[rid]
        person_balances[fid] = person_balances.get(fid, 0) + (qty if kind == "borrow" else -qty)
        if person_balances[fid] < 0:
            return False

    return all(item["available"] == item["total"] - sum(balances[item["id"]].values())
               for item in saved_resources)


def load_data():
    """Load saved data if the file exists; otherwise keep the starting data."""
    try:
        with open(DATA_FILE, "r") as f:
            data = json.load(f)
    except FileNotFoundError:
        return  # first run: nothing saved yet
    except json.JSONDecodeError:
        print("Saved file is damaged. Starting with default data.")
        return
    # Validate the top-level shape before replacing the defaults. A malformed
    # file should not leave the application in a broken state.
    if not valid_saved_data(data):
        print("Saved file has an invalid format. Starting with default data.")
        return
    resources[:] = data["resources"]
    borrow_records[:] = data["borrow_records"]

def find_resource(resource_id):
    """Return the resource dict with this ID, or None if not found."""
    resource_id = resource_id.strip().upper()
    for r in resources:
        if r["id"] == resource_id:
            return r
    return None


def outstanding(fellow_id, resource_id):
    """Units of this resource the fellow currently holds (borrowed minus returned)."""
    total = 0
    for rec in borrow_records:
        if rec["fellow_id"] == fellow_id and rec["resource_id"] == resource_id:
            if rec["type"] == "borrow":
                total += rec["quantity"]
            else:
                total -= rec["quantity"]
    return total


def add_resource(resource_id, name, category, total):
    """Add a new resource. Returns (success, message)."""
    resource_id = resource_id.strip().upper()
    name = name.strip()
    category = category.strip()

    if resource_id == "" or name == "" or category == "":
        return False, "ID, name and category cannot be empty."
    if find_resource(resource_id) is not None:
        return False, f"Resource ID {resource_id} already exists."
    if type(total) is not int or total <= 0:
        return False, "Total units must be a positive whole number."

    resources.append({
        "id": resource_id,
        "name": name,
        "category": category,
        "total": total,
        "available": total,
    })
    return True, f"Added {name} ({resource_id}) with {total} unit(s)."


def list_resources():
    print(f"{'ID':<6}{'Name':<14}{'Category':<14}{'Total':<7}{'Available'}")
    if not resources:
        print("No resources found.")
        return
    for r in resources:
        print(f"{r['id']:<6}{r['name']:<14}{r['category']:<14}{r['total']:<7}{r['available']}")


def borrow(fellow_id, resource_id, quantity):
    """Try to lend units. Returns (success, message). Changes nothing if rejected."""
    fellow_id = fellow_id.strip().upper()
    resource_id = resource_id.strip().upper()
    if fellow_id not in fellows:
        return False, f"Unknown fellow ID: {fellow_id}"

    resource = find_resource(resource_id)
    if resource is None:
        return False, f"Unknown resource ID: {resource_id}"

    if type(quantity) is not int or quantity <= 0:
        return False, "Quantity must be a positive whole number."

    if quantity > resource["available"]:
        return False, (f"Only {resource['available']} unit(s) of {resource['name']} "
                       f"available; you asked for {quantity}.")

    resource["available"] -= quantity
    borrow_records.append({
        "fellow_id": fellow_id,
        "resource_id": resource_id,
        "quantity": quantity,
        "type": "borrow",
    })
    return True, f"{fellows[fellow_id]} borrowed {quantity} x {resource['name']}."


def return_item(fellow_id, resource_id, quantity):
    """Try to take units back. Returns (success, message). Changes nothing if rejected."""
    fellow_id = fellow_id.strip().upper()
    resource_id = resource_id.strip().upper()
    if fellow_id not in fellows:
        return False, f"Unknown fellow ID: {fellow_id}"

    resource = find_resource(resource_id)
    if resource is None:
        return False, f"Unknown resource ID: {resource_id}"

    if type(quantity) is not int or quantity <= 0:
        return False, "Quantity must be a positive whole number."

    held = outstanding(fellow_id, resource_id)
    if quantity > held:
        return False, (f"{fellows[fellow_id]} holds only {held} unit(s) of "
                       f"{resource['name']}; you asked to return {quantity}.")

    resource["available"] += quantity
    borrow_records.append({
        "fellow_id": fellow_id,
        "resource_id": resource_id,
        "quantity": quantity,
        "type": "return",
    })
    return True, f"{fellows[fellow_id]} returned {quantity} x {resource['name']}."


def search_by_name(term):
    """Case-insensitive name search."""
    term = term.strip().lower()
    return [r for r in resources if term in r["name"].lower()]


def filter_by_category(category):
    """Case-insensitive category filter."""
    category = category.strip().lower()
    return [r for r in resources if r["category"].lower() == category]


def report():
    total_units = 0
    available_units = 0
    for r in resources:
        total_units += r["total"]
        available_units += r["available"]
    borrowed_units = total_units - available_units

    print(f"Total units: {total_units}")
    print(f"Available units: {available_units}")
    print(f"Borrowed units: {borrowed_units}")

    print("Low stock (fewer than 3 available):")
    low_found = False
    for r in resources:
        if r["available"] < 3:
            print(f"  {r['name']} ({r['available']})")
            low_found = True
    if not low_found:
        print("  None")

    on_loan = {}
    for r in resources:
        on_loan[r["id"]] = r["total"] - r["available"]
    highest = max(on_loan.values()) if on_loan else 0

    print("Most borrowed:")
    if highest == 0:
        print("  Nothing is currently borrowed")
    else:
        for r in resources:
            if on_loan[r["id"]] == highest:
                print(f"  {r['name']} ({highest})")


def read_positive_int(prompt):
    while True:
        text = input(prompt).strip()
        if text.isdigit() and int(text) > 0:
            return int(text)
        print("Please enter a whole number greater than 0.")


def read_text(prompt):
    while True:
        text = input(prompt).strip()
        if text != "":
            return text
        print("This cannot be empty.")


def persist_changes():
    """Report storage errors without abruptly closing the interactive menu."""
    try:
        save_data()
    except OSError as error:
        print(f"Could not save data: {error}")


def _run_demo_steps():
    print("Step 1: F001 borrows 2 laptops")
    print(borrow("F001", "R001", 2)[1])
    print("Laptop available:", find_resource("R001")["available"])
    print("\nStep 2: F002 borrows 3 keyboards")
    print(borrow("F002", "R002", 3)[1])
    print("Keyboard available:", find_resource("R002")["available"])

    print("\nStep 3: F001 returns 1 laptop")
    print(return_item("F001", "R001", 1)[1])
    print("Laptop available:", find_resource("R001")["available"])

    print("\nStep 4: F003 requests 4 headsets")
    print(borrow("F003", "R003", 4)[1])
    print("Headset available:", find_resource("R003")["available"])

    print("\nStep 5: F002 tries to return 4 keyboards")
    print(return_item("F002", "R002", 4)[1])
    print("Keyboard available:", find_resource("R002")["available"])

    print("\nStep 6: search for LAPtop")
    for r in search_by_name("LAPtop"):
        print(r["id"], r["name"], r["category"], r["available"])

    print("\nStep 7: report")
    report()

    print("\nExtra invalid-input test: unknown fellow F999 borrows 1 laptop")
    print(borrow("F999", "R001", 1)[1])
    print("Laptop available:", find_resource("R001")["available"])


def run_demo():
    """Show the sample workflow without changing the real inventory."""
    original_resources = [resource.copy() for resource in resources]
    original_records = [record.copy() for record in borrow_records]
    try:
        _run_demo_steps()
    finally:
        resources[:] = original_resources
        borrow_records[:] = original_records


def menu():
    while True:
        print("\n=== Learn2Earn Resource Manager ===")
        print("1. List resources")
        print("2. Add resource")
        print("3. Borrow")
        print("4. Return")
        print("5. Search by name")
        print("6. Filter by category")
        print("7. Report")
        print("8. Run required demo")
        print("0. Exit")
        try:
            choice = input("Choose an option: ").strip()
        except EOFError:
            print("\nGoodbye!")
            break

        if choice == "1":
            list_resources()
        elif choice == "2":
            rid = read_text("Resource ID: ")
            name = read_text("Name: ")
            category = read_text("Category: ")
            total = read_positive_int("Total units: ")
            success, message = add_resource(rid, name, category, total)
            print(message)
            if success:
                persist_changes()
        elif choice == "3":
            fid = read_text("Fellow ID: ").upper()
            rid = read_text("Resource ID: ").upper()
            qty = read_positive_int("Quantity: ")
            success, message = borrow(fid, rid, qty)
            print(message)
            if success:
                persist_changes()
        elif choice == "4":
            fid = read_text("Fellow ID: ").upper()
            rid = read_text("Resource ID: ").upper()
            qty = read_positive_int("Quantity: ")
            success, message = return_item(fid, rid, qty)
            print(message)
            if success:
                persist_changes()
        elif choice == "5":
            matches = search_by_name(read_text("Name to search: "))
            if not matches:
                print("No matching resources found.")
            for r in matches:
                print(r["id"], r["name"], r["category"], r["available"])
        elif choice == "6":
            matches = filter_by_category(read_text("Category: "))
            if not matches:
                print("No resources found in that category.")
            for r in matches:
                print(r["id"], r["name"], r["category"], r["available"])
        elif choice == "7":
            report()
        elif choice == "8":
            run_demo()
        elif choice == "0":
            print("Goodbye!")
            break
        else:
            print("Invalid option. Please choose 0-8.")


if __name__ == "__main__":
    load_data()
    menu()
