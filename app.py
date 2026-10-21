"""Flask pages for the ResourceHub equipment manager."""
from collections import defaultdict
from datetime import date, timedelta
import hmac
import os
import secrets
import tempfile

from flask import Flask, abort, redirect, render_template, request, send_file, session, url_for

import main as inventory
import audit


def load_local_environment():
    """Load optional local settings without printing private values."""
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

app = Flask(__name__)
app.secret_key = os.environ.get("RESOURCEHUB_SECRET_KEY") or secrets.token_hex(32)


def configured_accounts():
    """Return only explicitly configured Flask accounts and their roles."""
    accounts = []
    for role in ("admin", "staff"):
        username = os.environ.get(f"RESOURCEHUB_{role.upper()}_USERNAME", "").strip()
        password = os.environ.get(f"RESOURCEHUB_{role.upper()}_PASSWORD", "")
        if username and password and not username.startswith("your-") and not password.startswith("replace-"):
            accounts.append((username, password, role))
    return accounts


def authenticate(username, password):
    username_bytes = username.encode("utf-8")
    password_bytes = password.encode("utf-8")
    for configured_username, configured_password, role in configured_accounts():
        username_ok = hmac.compare_digest(username_bytes, configured_username.encode("utf-8"))
        password_ok = hmac.compare_digest(password_bytes, configured_password.encode("utf-8"))
        if username_ok and password_ok:
            return configured_username, role
    return None


ADMIN_ENDPOINTS = {
    "add_item_page", "reports_page", "reports_export", "reports_backup",
    "audit_log_page", "audit_log_export",
}


@app.context_processor
def provide_csrf_token():
    def csrf_token():
        if "_csrf_token" not in session:
            session["_csrf_token"] = secrets.token_urlsafe(32)
        return session["_csrf_token"]

    return {"csrf_token": csrf_token}


@app.before_request
def protect_post_requests():
    if request.method == "POST":
        expected = session.get("_csrf_token", "")
        submitted = request.form.get("csrf_token", "")
        if not expected or not hmac.compare_digest(
            expected.encode("utf-8"), submitted.encode("utf-8")
        ):
            abort(400, description="The form security token is missing or expired. Reload the page and try again.")


@app.before_request
def require_login_and_role():
    endpoint = request.endpoint
    if endpoint in (None, "static", "login_page"):
        return
    if not session.get("username"):
        return redirect(url_for("login_page", next=request.full_path))
    if endpoint in ADMIN_ENDPOINTS and session.get("role") != "admin":
        abort(403)


@app.route("/login", methods=["GET", "POST"])
def login_page():
    message = ""
    next_path = request.values.get("next", "")
    if request.method == "POST":
        result = authenticate(request.form.get("username", "").strip(), request.form.get("password", ""))
        if result:
            username, role = result
            session.clear()
            session["username"] = username
            session["role"] = role
            if not next_path.startswith("/") or next_path.startswith("//"):
                next_path = url_for("dashboard_page")
            return redirect(next_path)
        message = "Sign-in failed. Check your username and password."
    return render_template("login.html", message=message, next_path=next_path,
                           accounts_configured=bool(configured_accounts()))


@app.route("/logout", methods=["POST"])
def logout_page():
    session.clear()
    return redirect(url_for("login_page"))


@app.route("/")
def home():
    return redirect(url_for("dashboard_page"))


@app.route("/inventory")
def inventory_page():
    inventory.load_data()
    search_text = request.args.get("q", "").strip()
    selected_category = request.args.get("category", "").strip()

    # Reuse the CLI's search and category functions; this route only prepares
    # the selected results for display.
    matches = inventory.search_by_name(search_text) if search_text else inventory.resources
    if selected_category:
        category_ids = {item["id"] for item in inventory.filter_by_category(selected_category)}
        matches = [item for item in matches if item["id"] in category_ids]

    rows = []
    for resource in matches:
        row = dict(resource)
        if row["available"] == 0:
            row.update(stock_class="out", stock_label="Out of stock")
        elif row["available"] < 3:
            row.update(stock_class="low", stock_label="Low stock")
        else:
            row.update(stock_class="plenty", stock_label="In stock")
        rows.append(row)

    categories = sorted({item["category"] for item in inventory.resources}, key=str.lower)
    return render_template(
        "inventory.html",
        resources=rows,
        categories=categories,
        search_text=search_text,
        selected_category=selected_category,
        notice=request.args.get("notice", ""),
    )


@app.route("/inventory/add", methods=["POST"])
def add_item_page():
    inventory.load_data()
    form = request.form
    try:
        total = int(form.get("total", ""))
        price = float(form.get("price", "0") or 0)
        reorder_level = int(form.get("reorder_level", "3") or 3)
    except ValueError:
        return redirect(url_for("inventory_page", notice="Enter valid numbers for stock, price, and reorder level."))
    success, message = inventory.add_resource(
        form.get("resource_id", ""), form.get("name", ""), form.get("category", ""),
        total, form.get("sku", ""), price, form.get("supplier", ""), reorder_level,
    )
    return redirect(url_for("inventory_page", notice=message))


def transaction_form(mode, message=None, succeeded=None, values=None):
    inventory.load_data()
    return render_template(
        "transactions.html",
        fellows=[{"id": fid, "name": name} for fid, name in inventory.fellows.items()],
        resources=inventory.resources,
        default_due_date=(date.today() + timedelta(days=inventory.LOAN_DAYS)).isoformat(),
        mode=mode,
        message_mode=mode if message else None,
        message=message,
        succeeded=succeeded,
        values=values or {},
    )


def handle_transaction(mode):
    values = {
        "fellow_id": request.form.get("fellow_id", "").strip(),
        "resource_id": request.form.get("resource_id", "").strip(),
        "quantity": request.form.get("quantity", "").strip(),
        "due_date": request.form.get("due_date", "").strip(),
    }

    # Match the CLI: required IDs and a positive whole-number quantity.
    if not values["fellow_id"]:
        return transaction_form(mode, "Choose a customer.", False, values)
    if not values["resource_id"]:
        return transaction_form(mode, "Resource ID cannot be empty.", False, values)
    if not values["quantity"].isdigit() or int(values["quantity"]) <= 0:
        return transaction_form(mode, "Quantity must be a positive whole number.", False, values)

    quantity = int(values["quantity"])
    if mode == "borrow":
        success, message = inventory.borrow(
            values["fellow_id"], values["resource_id"], quantity,
            values["due_date"] or None,
        )
    else:
        success, message = inventory.return_item(
            values["fellow_id"], values["resource_id"], quantity,
        )
    return transaction_form(mode, message, success, values)


@app.route("/borrow", methods=["GET", "POST"])
def borrow_route():
    if request.method == "POST":
        return handle_transaction("borrow")
    return transaction_form("borrow")


@app.route("/return", methods=["GET", "POST"])
def return_route():
    if request.method == "POST":
        return handle_transaction("return")
    return transaction_form("return")


@app.route("/sales/credit", methods=["POST"])
def credit_sale_page():
    inventory.load_data()
    quantity_text = request.form.get("quantity", "").strip()
    if not quantity_text.isdigit() or int(quantity_text) <= 0:
        message = "Quantity must be a positive whole number."
        succeeded = False
    else:
        succeeded, message = inventory.record_credit_sale(
            request.form.get("customer_id", ""),
            request.form.get("resource_id", ""),
            int(quantity_text),
            request.form.get("amount_paid", "0"),
        )
    return redirect(url_for("dashboard_page", notice=message, success="1" if succeeded else "0"))


@app.route("/dashboard")
def dashboard_page():
    inventory.load_data()
    summary = inventory.report_data()
    with inventory.connect() as db:
        recent_sales = [dict(row) for row in db.execute("""SELECT s.created_at,s.quantity,s.unit_price,s.customer,
            r.id AS resource_id,r.name AS resource_name FROM sales s JOIN resources r ON r.id=s.resource_id
            ORDER BY s.id DESC LIMIT 5""")]
    return render_template("dashboard.html", summary=summary, recent_sales=recent_sales,
                           fellows=[{"id": fid, "name": name} for fid, name in inventory.fellows.items()],
                           resources=inventory.resources,
                           notice=request.args.get("notice", ""),
                           succeeded=request.args.get("success") == "1")


@app.route("/transactions")
def transactions_page():
    inventory.load_data()
    search_text = request.args.get("q", "").strip()[:120]
    selected_type = request.args.get("type", "all").strip().lower()
    valid_types = {"all", "sale", "borrow", "return", "damaged", "opening_stock", "restock"}
    if selected_type not in valid_types:
        selected_type = "all"
    with inventory.connect() as db:
        rows = [dict(row) for row in db.execute("""SELECT created_at,type,resource_name,quantity,person FROM (
            SELECT l.created_at,l.type,r.name AS resource_name,l.quantity,f.name AS person
              FROM loans l JOIN resources r ON r.id=l.resource_id JOIN fellows f ON f.id=l.fellow_id
            UNION ALL
            SELECT s.created_at,'sale',r.name,s.quantity,s.customer FROM sales s JOIN resources r ON r.id=s.resource_id
            UNION ALL
            SELECT m.created_at,m.movement_type,r.name,m.quantity,'' FROM stock_movements m
              JOIN resources r ON r.id=m.resource_id
        ) ORDER BY created_at DESC""")]
    if selected_type != "all":
        rows = [row for row in rows if row["type"] == selected_type]
    if search_text:
        needle = search_text.casefold()
        rows = [row for row in rows if needle in row["resource_name"].casefold()
                or needle in (row["person"] or "").casefold()]
    return render_template("history.html", rows=rows, search_text=search_text, selected_type=selected_type)


def current_customer_items():
    """Return outstanding item quantities grouped by customer name and ID."""
    with inventory.connect() as db:
        loans = [dict(row) for row in db.execute("""SELECT f.id AS fellow_id,f.name AS fellow_name,
            r.id AS resource_id,r.name AS resource_name,
            SUM(CASE WHEN l.type='borrow' THEN l.quantity ELSE -l.quantity END) AS quantity
            FROM loans l JOIN fellows f ON f.id=l.fellow_id JOIN resources r ON r.id=l.resource_id
            GROUP BY f.id,r.id HAVING quantity>0 ORDER BY f.name COLLATE NOCASE,r.name COLLATE NOCASE""")]
    grouped = defaultdict(list)
    for loan in loans:
        grouped[(loan["fellow_id"], loan["fellow_name"])].append(loan)
    return list(grouped.items())


def customer_purchase_history():
    """Return recorded sales, credit sales, and customer returns by customer ID."""
    history = {}
    with inventory.connect() as db:
        for customer_id, customer_name in inventory.fellows.items():
            rows = db.execute("""SELECT * FROM (
                SELECT s.id AS sale_id, s.created_at AS created_at,
                       CASE WHEN s.sale_type='credit' THEN 'Credit sale' ELSE 'Sale' END AS transaction_type,
                       r.name AS item_name, s.quantity AS quantity,
                       s.unit_price AS unit_price, s.sale_type AS sale_type,
                       ROUND(s.quantity*s.unit_price,2) AS sale_total,
                       ROUND(COALESCE((SELECT SUM(p.amount) FROM sale_payments p WHERE p.sale_id=s.id),0),2) AS amount_paid,
                       ROUND(MAX(0,s.quantity*s.unit_price-COALESCE((SELECT SUM(p.amount) FROM sale_payments p WHERE p.sale_id=s.id),0)),2) AS amount_owed
                FROM sales s JOIN resources r ON r.id=s.resource_id
                WHERE s.customer_id=? OR (s.customer_id IS NULL AND LOWER(TRIM(s.customer))=LOWER(TRIM(?)))
                UNION ALL
                SELECT NULL AS sale_id, l.created_at AS created_at,
                       CASE WHEN l.type='borrow' THEN 'Credit sale' ELSE 'Customer return' END AS transaction_type,
                       r.name AS item_name, l.quantity AS quantity,
                       NULL AS unit_price, 'legacy' AS sale_type,
                       NULL AS sale_total, NULL AS amount_paid, NULL AS amount_owed
                FROM loans l JOIN resources r ON r.id=l.resource_id
                WHERE l.fellow_id=? AND l.type IN ('borrow','return')
            ) ORDER BY created_at DESC""", (customer_id, customer_name, customer_id)).fetchall()
            history[customer_id] = [dict(row) for row in rows]
    return history


@app.route("/customers")
def customers_page():
    inventory.load_data()
    customers = [{"id": fid, "name": name} for fid, name in inventory.fellows.items()]
    return render_template(
        "customers.html",
        customer_groups=current_customer_items(),
        customers=customers,
        purchase_history=customer_purchase_history(),
        notice=request.args.get("notice", ""),
        notice_success=request.args.get("added") == "1",
    )


@app.route("/customers/add", methods=["POST"])
def add_customer_page():
    inventory.load_data()
    customer_id = request.form.get("customer_id", "").strip()
    customer_name = request.form.get("customer_name", "").strip()
    success, message = inventory.add_fellow(customer_id, customer_name)
    return redirect(url_for(
        "customers_page", notice=message, added="1" if success else "0"
    ))


@app.route("/sales/<int:sale_id>/payments", methods=["POST"])
def sale_payment_page(sale_id):
    success, message = inventory.record_sale_payment(
        sale_id, request.form.get("amount", "")
    )
    return redirect(url_for(
        "customers_page", notice=message, added="1" if success else "0"
    ))


@app.route("/loans")
def loans_page():
    return redirect(url_for("customers_page"))


@app.route("/reports")
def reports_page():
    inventory.load_data()
    summary = inventory.report_data()
    with inventory.connect() as db:
        sales = [dict(row) for row in db.execute("""SELECT r.id,r.name,r.category,r.price,
            COALESCE(SUM(s.quantity),0) AS units_sold,
            COALESCE(SUM(s.quantity*s.unit_price),0) AS sales_total
            FROM resources r LEFT JOIN sales s ON s.resource_id=r.id
            GROUP BY r.id ORDER BY sales_total DESC,r.name COLLATE NOCASE""")]
    available_by_id = {item["id"]: item["available"] for item in inventory.resources}
    for item in sales:
        item["available"] = available_by_id.get(item["id"], 0)
    sales_total = sum(item["sales_total"] for item in sales)
    stock_value = sum(item["available"] * item["price"] for item in inventory.resources)
    return render_template("reports.html", summary=summary, sales=sales,
                           sales_total=sales_total, stock_value=stock_value)


@app.route("/reports/export.csv")
def reports_export():
    inventory.load_data()
    with tempfile.NamedTemporaryFile(prefix="resourcehub-report-", suffix=".csv", delete=False) as temp:
        path = temp.name
    try:
        inventory.export_report_csv(path)
        response = send_file(path, as_attachment=True, download_name="resourcehub-report.csv", mimetype="text/csv")
        response.call_on_close(lambda: os.path.exists(path) and os.remove(path))
        return response
    except Exception:
        if os.path.exists(path):
            os.remove(path)
        raise


@app.route("/reports/backup")
def reports_backup():
    inventory.load_data()
    path = inventory.backup_database()
    if path is None:
        abort(404)
    return send_file(path, as_attachment=True, download_name="resourcehub-backup.sqlite3")


@app.route("/audit-log")
def audit_log_page():
    inventory.load_data()
    search_text = request.args.get("q", "").strip()[:120]
    return render_template("audit_log.html", entries=audit.entries(inventory, search_text), search_text=search_text)


@app.route("/audit-log/export.csv")
def audit_log_export():
    inventory.load_data()
    from io import BytesIO
    output = BytesIO(audit.to_csv(inventory, request.args.get("q", "").strip()[:120]).encode("utf-8"))
    return send_file(output, as_attachment=True, download_name="resourcehub-audit.csv", mimetype="text/csv")


@app.errorhandler(404)
def page_not_found(error):
    return render_template("404.html"), 404


@app.errorhandler(403)
def access_forbidden(error):
    return render_template("403.html"), 403


@app.errorhandler(400)
def bad_request(error):
    return render_template("400.html", message=error.description), 400


if __name__ == "__main__":
    app.run(debug=True)
