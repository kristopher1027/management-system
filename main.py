# Learn2Earn Campus Resource Management System (standard library only)
import json

resources = [
    {"id": "R001", "name": "Laptop", "category": "Electronics", "total": 10, "available": 10},
    {"id": "R002", "name": "Keyboard", "category": "Accessories", "total": 5, "available": 5},
    {"id": "R003", "name": "Headset", "category": "Accessories", "total": 3, "available": 3},
]
fellows = {"F001": "Ada", "F002": "John", "F003": "Grace"}
borrow_records = []

DATA_FILE = "data.json"


def save_data():
    """Write inventory and loan records to a JSON file."""
    data = {"resources": resources, "borrow_records": borrow_records}
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)


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
    if not isinstance(data, dict) or not isinstance(data.get("resources"), list) \
            or not isinstance(data.get("borrow_records"), list):
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
    if not isinstance(total, int) or total <= 0:
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

    if not isinstance(quantity, int) or quantity <= 0:
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

    if not isinstance(quantity, int) or quantity <= 0:
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


def run_demo():
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
        choice = input("Choose an option: ").strip()

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
                save_data()
        elif choice == "3":
            fid = read_text("Fellow ID: ").upper()
            rid = read_text("Resource ID: ").upper()
            qty = read_positive_int("Quantity: ")
            success, message = borrow(fid, rid, qty)
            print(message)
            if success:
                save_data()
        elif choice == "4":
            fid = read_text("Fellow ID: ").upper()
            rid = read_text("Resource ID: ").upper()
            qty = read_positive_int("Quantity: ")
            success, message = return_item(fid, rid, qty)
            print(message)
            if success:
                save_data()
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
