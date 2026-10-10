"""Step 1: the ResourceHub Flask starter page."""
from datetime import date, timedelta
import hmac
import os
import secrets

from flask import Flask, abort, render_template, request, session

import main as inventory

app = Flask(__name__)
app.secret_key = os.environ.get("RESOURCEHUB_SECRET_KEY") or secrets.token_hex(32)


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


@app.route("/")
def home():
    return render_template("hello.html", project_name="ResourceHub")


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
    )


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
        return transaction_form(mode, "Fellow ID cannot be empty.", False, values)
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


@app.route("/dashboard")
def dashboard_page():
    inventory.load_data()
    summary = inventory.report_data()
    return render_template("dashboard.html", summary=summary)


@app.errorhandler(404)
def page_not_found(error):
    return render_template("404.html"), 404


@app.errorhandler(400)
def bad_request(error):
    return render_template("400.html", message=error.description), 400


if __name__ == "__main__":
    app.run(debug=True)
