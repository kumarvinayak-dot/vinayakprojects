"""
mock_crm_app.py - Mock CRM Order Management app for demoing the AI test pipeline.

Implements the rules in crm_requirements.txt (order creation, modification,
cancellation) with stable element IDs so generated Playwright scripts can drive it.

Run:   py -3.12 mock_crm_app.py          -> http://127.0.0.1:5050/login
UI map (pages + selectors for the test generator): http://127.0.0.1:5050/ui-map

Each browser session gets its own fresh copy of the demo data, so every
Playwright run (new browser context) starts from the same known state.
"""

import argparse
import re
import uuid
from datetime import datetime

from flask import (Flask, Response, abort, g, redirect, render_template_string,
                   request, session, url_for)

app = Flask(__name__)
app.secret_key = "mock-crm-demo-only"

USERS = {
    "csr1": {"password": "Password123!", "role": "CSR", "name": "Asha (CSR)"},
    "sup1": {"password": "Super123!", "role": "Supervisor", "name": "Ravi (Supervisor)"},
}

CUSTOMERS = {
    "ACC-1001": {"name": "Acme Corp", "status": "Active"},
    "ACC-1002": {"name": "Delta Retail", "status": "Active"},
    "ACC-2001": {"name": "Beta LLC", "status": "Suspended"},
    "ACC-3001": {"name": "Gamma Inc", "status": "Closed"},
}

PRODUCTS = {
    "FIBER-100": {"name": "Fiber Broadband 100 Mbps", "price": 40.00},
    "FIBER-500": {"name": "Fiber Broadband 500 Mbps", "price": 60.00},
    "TV-BASIC": {"name": "TV Basic Package", "price": 15.00},
    "ROUTER": {"name": "Wi-Fi Router", "price": 5.00},
}


def _seed_orders():
    def order(oid, acc, status, lines, charges=0.0):
        return {"id": oid, "customer": acc, "status": status, "lines": lines,
                "delivery": "12 MG Road, Bengaluru", "charges": charges,
                "cancel_requested": False, "log": []}
    return {
        "ORD-0001": order("ORD-0001", "ACC-1001", "New", [{"product": "FIBER-100", "qty": 1}]),
        "ORD-0002": order("ORD-0002", "ACC-1002", "Pending", [{"product": "TV-BASIC", "qty": 2}]),
        "ORD-0003": order("ORD-0003", "ACC-1001", "Provisioning", [{"product": "FIBER-500", "qty": 1}]),
        "ORD-0004": order("ORD-0004", "ACC-1002", "Completed", [{"product": "ROUTER", "qty": 1}], 5.00),
        "ORD-0005": order("ORD-0005", "ACC-1001", "Cancelled", [{"product": "TV-BASIC", "qty": 1}]),
    }


# per-browser-session demo data: {sid: {"orders": {...}, "next_id": int}}
_STORES = {}


@app.before_request
def _load_store():
    sid = session.get("sid")
    if not sid or sid not in _STORES:
        sid = uuid.uuid4().hex
        session["sid"] = sid
        _STORES[sid] = {"orders": _seed_orders(), "next_id": 1001}
    g.store = _STORES[sid]
    g.user = USERS.get(session.get("user"))
    g.user_id = session.get("user")


def _login_required():
    if not g.user:
        return redirect(url_for("login", next=request.path))
    return None


def _log(order, action):
    order["log"].append({"at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                         "user": g.user_id, "action": action})


def _total(order):
    return sum(PRODUCTS[l["product"]]["price"] * l["qty"] for l in order["lines"])


BASE = """<!doctype html><html><head><meta charset="utf-8"><title>{{ title }} - Mock CRM</title>
<style>
 body{font-family:system-ui,Arial,sans-serif;margin:0;background:#f5f7fb;color:#0f172a}
 header{background:#1e3a8a;color:#fff;padding:12px 20px;display:flex;justify-content:space-between;align-items:center}
 header a{color:#fff;margin-left:14px} main{max-width:900px;margin:20px auto;padding:0 16px}
 .card{background:#fff;border:1px solid #d0d7de;border-radius:10px;padding:18px;margin-bottom:16px}
 label{display:block;font-weight:600;margin:10px 0 4px} input,select,textarea{padding:8px;border:1px solid #94a3b8;border-radius:6px;font-size:14px}
 button{background:#1d4ed8;color:#fff;border:none;padding:9px 16px;border-radius:6px;cursor:pointer;font-size:14px;margin-top:12px}
 button.danger{background:#dc2626} button.ok{background:#059669}
 table{border-collapse:collapse;width:100%} th,td{border:1px solid #d0d7de;padding:7px;text-align:left;font-size:14px}
 .msg{padding:10px;border-radius:6px;margin-bottom:12px} .success{background:#dcfce7;border:1px solid #16a34a}
 .error{background:#fee2e2;border:1px solid #dc2626} .info{background:#fef3c7;border:1px solid #d97706}
</style></head><body>
<header><strong>Mock CRM - Order Management</strong>
<span>{% if user %}<span id="current-user">{{ user.name }}</span>
<a id="nav-orders" href="/orders">Orders</a><a id="nav-create-order" href="/orders/new">Create Order</a>
<a id="logout-link" href="/logout">Logout</a>{% endif %}</span></header>
<main>
{% if error %}<div class="msg error" id="error-message">{{ error }}</div>{% endif %}
{% if success %}<div class="msg success" id="success-message">{{ success }}</div>{% endif %}
{% if info %}<div class="msg info" id="info-message">{{ info }}</div>{% endif %}
{{ body|safe }}
</main></body></html>"""


def page(title, body, **kw):
    inner = render_template_string(body, **kw, user=g.user, products=PRODUCTS,
                                   customers=CUSTOMERS, total=_total)
    return render_template_string(BASE, title=title, body=inner, user=g.user,
                                  error=kw.get("error"), success=kw.get("success"),
                                  info=kw.get("info"))


@app.route("/")
def home():
    return redirect(url_for("orders") if g.user else url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        u = request.form.get("username", "").strip()
        p = request.form.get("password", "")
        if u in USERS and USERS[u]["password"] == p:
            session["user"] = u
            return redirect(request.args.get("next") or url_for("orders"))
        error = "Invalid username or password."
    return page("Login", """
<div class="card"><h2>Sign in</h2>
<form id="login-form" method="post">
 <label for="username">Username</label><input id="username" name="username">
 <label for="password">Password</label><input id="password" name="password" type="password">
 <br><button id="login-button" type="submit">Log in</button>
</form>
<p><small>Demo users: csr1 / Password123! (CSR), sup1 / Super123! (Supervisor)</small></p></div>""",
                error=error)


@app.route("/logout")
def logout():
    session.pop("user", None)
    return redirect(url_for("login"))


@app.route("/orders")
def orders():
    if (r := _login_required()):
        return r
    return page("Orders", """
<div class="card"><h2>Orders</h2>
<a id="create-order-link" href="/orders/new">+ Create New Order</a><br><br>
<table id="orders-table"><tr><th>Order ID</th><th>Customer</th><th>Status</th><th>Total</th></tr>
{% for o in orders %}<tr id="row-{{ o.id }}">
 <td><a id="order-link-{{ o.id }}" href="/orders/{{ o.id }}">{{ o.id }}</a></td>
 <td>{{ o.customer }} - {{ customers[o.customer].name }} ({{ customers[o.customer].status }})</td>
 <td class="order-status">{{ o.status }}</td><td>${{ '%.2f' % total(o) }}</td></tr>{% endfor %}
</table></div>""", orders=list(g.store["orders"].values()))


@app.route("/orders/new", methods=["GET", "POST"])
def new_order():
    if (r := _login_required()):
        return r
    error, form = None, request.form
    if request.method == "POST":
        acc = form.get("customer_account", "")
        lines = []
        for i in (1, 2, 3):
            prod, qty = form.get(f"product_{i}", ""), form.get(f"quantity_{i}", "").strip()
            if prod:
                try:
                    q = int(qty or "1")
                except ValueError:
                    q = 0
                if q < 1:
                    error = f"Line {i}: quantity must be a whole number of at least 1."
                    break
                lines.append({"product": prod, "qty": q})
        oid = form.get("order_id", "").strip()
        if not error and oid:
            if not re.fullmatch(r"[A-Za-z0-9-]{3,30}", oid):
                error = "Order ID may contain only letters, digits and hyphens (3-30 characters)."
            elif oid.upper() in (k.upper() for k in g.store["orders"]):
                error = f"Order ID {oid} already exists. Please enter a unique Order ID."
        if not error:
            if acc not in CUSTOMERS:
                error = "Please select a customer account."
            elif CUSTOMERS[acc]["status"] != "Active":
                error = (f"Order rejected: customer account {acc} is "
                         f"{CUSTOMERS[acc]['status'].lower()}. Orders can only be created for active accounts.")
            elif not lines:
                error = "Order must contain at least one product line item."
        if not error:
            while not oid or oid in g.store["orders"]:  # auto-generate when left blank
                oid = f"ORD-{g.store['next_id']}"
                g.store["next_id"] += 1
            order = {"id": oid, "customer": acc, "status": "New", "lines": lines,
                     "delivery": form.get("delivery_address", "").strip(), "charges": 0.0,
                     "cancel_requested": False, "log": []}
            _log(order, "Order created")
            g.store["orders"][oid] = order
            return redirect(url_for("order_detail", oid=oid, created=1))
    return page("Create Order", """
<div class="card"><h2>Create New Order</h2>
<form id="order-form" method="post">
 <label for="order-id-input">Order ID (optional - auto-generated if blank)</label>
 <input id="order-id-input" name="order_id" placeholder="e.g. ORD-TEST-001">
 <label for="customer-account">Customer account</label>
 <select id="customer-account" name="customer_account">
  <option value="">-- Select customer --</option>
  {% for acc, c in customers.items() %}<option value="{{ acc }}">{{ acc }} - {{ c.name }} ({{ c.status }})</option>{% endfor %}
 </select>
 {% for i in [1, 2, 3] %}
 <label for="product-{{ i }}">Product line {{ i }}{% if i > 1 %} (optional){% endif %}</label>
 <select id="product-{{ i }}" name="product_{{ i }}"><option value="">-- None --</option>
  {% for code, p in products.items() %}<option value="{{ code }}">{{ code }} - {{ p.name }} (${{ '%.2f' % p.price }})</option>{% endfor %}
 </select>
 <input id="quantity-{{ i }}" name="quantity_{{ i }}" type="number" min="1" placeholder="Qty" style="width:80px">
 {% endfor %}
 <label for="delivery-address">Delivery address</label>
 <textarea id="delivery-address" name="delivery_address" rows="2" cols="50"></textarea>
 <br><button id="submit-order" type="submit">Create Order</button>
</form></div>""", error=error)


@app.route("/orders/<oid>")
def order_detail(oid):
    if (r := _login_required()):
        return r
    o = g.store["orders"].get(oid) or abort(404)
    success = f"Order {oid} created successfully with status New." if request.args.get("created") else None
    success = success or request.args.get("ok")
    return page(f"Order {oid}", """
<div class="card"><h2>Order <span id="order-id">{{ o.id }}</span></h2>
<p>Status: <strong id="order-status">{{ o.status }}</strong></p>
<p>Customer: <span id="order-customer">{{ o.customer }} - {{ customers[o.customer].name }}</span></p>
<p>Charges applied: <span id="order-charges">${{ '%.2f' % o.charges }}</span></p>
<p>Delivery: <span id="order-delivery">{{ o.delivery or '-' }}</span></p>
<table id="line-items"><tr><th>Product</th><th>Qty</th><th>Price</th></tr>
{% for l in o.lines %}<tr class="line-item"><td>{{ l.product }} - {{ products[l.product].name }}</td><td class="line-qty">{{ l.qty }}</td><td>${{ '%.2f' % (products[l.product].price * l.qty) }}</td></tr>{% endfor %}
</table>
<p>Order total: <strong id="order-total">${{ '%.2f' % total(o) }}</strong></p>
{% if o.status in ['New', 'Pending'] %}<a id="edit-order-link" href="/orders/{{ o.id }}/edit">Edit order</a>{% endif %}
<form method="post" action="/orders/{{ o.id }}/cancel" style="display:inline">
 <button id="cancel-order-button" class="danger" type="submit">Cancel order</button></form>
{% if o.cancel_requested and o.status == 'Provisioning' %}
 <p id="approval-status">Cancellation pending supervisor approval.</p>
 {% if user and user.role == 'Supervisor' %}
 <form method="post" action="/orders/{{ o.id }}/approve-cancel" style="display:inline">
  <button id="approve-cancel-button" class="ok" type="submit">Approve cancellation</button></form>
 {% endif %}
{% endif %}
</div>
<div class="card"><h3>Audit log</h3><table id="audit-log"><tr><th>Timestamp</th><th>User ID</th><th>Action</th></tr>
{% for e in o.log %}<tr class="audit-entry"><td>{{ e.at }}</td><td class="audit-user">{{ e.user }}</td><td class="audit-action">{{ e.action }}</td></tr>{% endfor %}
</table></div>""", o=o, success=success, error=request.args.get("err"),
                info=request.args.get("info"))


@app.route("/orders/<oid>/edit", methods=["GET", "POST"])
def edit_order(oid):
    if (r := _login_required()):
        return r
    o = g.store["orders"].get(oid) or abort(404)
    if o["status"] not in ("New", "Pending"):
        return redirect(url_for("order_detail", oid=oid,
                                err=f"Order {oid} is in {o['status']} status and cannot be modified."))
    error = None
    if request.method == "POST":
        new_lines = []
        for i, l in enumerate(o["lines"], 1):
            if request.form.get(f"remove_{i}"):
                continue
            try:
                q = int(request.form.get(f"quantity_{i}", l["qty"]))
            except ValueError:
                q = 0
            if q < 1:
                error = f"Line {i}: quantity must be at least 1."
                break
            new_lines.append({"product": l["product"], "qty": q})
        add = request.form.get("add_product", "")
        if not error and add:
            new_lines.append({"product": add, "qty": 1})
        if not error and not new_lines:
            error = "Order must contain at least one product line item."
        if not error:
            o["lines"] = new_lines
            o["delivery"] = request.form.get("delivery_address", o["delivery"]).strip()
            _log(o, "Order modified")
            return redirect(url_for("order_detail", oid=oid, ok=f"Order {oid} updated successfully."))
    return page(f"Edit {oid}", """
<div class="card"><h2>Edit Order {{ o.id }}</h2>
<form id="edit-form" method="post">
{% for l in o.lines %}{% set i = loop.index %}
 <label for="edit-quantity-{{ i }}">{{ l.product }} quantity</label>
 <input id="edit-quantity-{{ i }}" name="quantity_{{ i }}" type="number" value="{{ l.qty }}" style="width:80px">
 <label style="display:inline;font-weight:400"><input id="remove-line-{{ i }}" name="remove_{{ i }}" type="checkbox"> Remove</label>
{% endfor %}
 <label for="add-product">Add product (optional)</label>
 <select id="add-product" name="add_product"><option value="">-- None --</option>
  {% for code, p in products.items() %}<option value="{{ code }}">{{ code }} - {{ p.name }}</option>{% endfor %}</select>
 <label for="edit-delivery-address">Delivery address</label>
 <textarea id="edit-delivery-address" name="delivery_address" rows="2" cols="50">{{ o.delivery }}</textarea>
 <br><button id="save-order" type="submit">Save changes</button>
</form></div>""", o=o, error=error)


@app.route("/orders/<oid>/cancel", methods=["POST"])
def cancel_order(oid):
    if (r := _login_required()):
        return r
    o = g.store["orders"].get(oid) or abort(404)
    st = o["status"]
    if st in ("New", "Pending"):
        o["status"], o["charges"] = "Cancelled", 0.0
        _log(o, "Order cancelled")
        return redirect(url_for("order_detail", oid=oid,
                                ok=f"Order {oid} has been cancelled. No charges were applied."))
    if st == "Provisioning":
        o["cancel_requested"] = True
        _log(o, "Cancellation requested - awaiting supervisor approval")
        return redirect(url_for("order_detail", oid=oid,
                                info=f"Order {oid} is in Provisioning. Cancellation requires supervisor approval."))
    if st == "Completed":
        return redirect(url_for("order_detail", oid=oid,
                                err=f"Order {oid} is Completed and cannot be cancelled. Please follow the returns process."))
    return redirect(url_for("order_detail", oid=oid, err=f"Order {oid} is already cancelled."))


@app.route("/orders/<oid>/approve-cancel", methods=["POST"])
def approve_cancel(oid):
    if (r := _login_required()):
        return r
    o = g.store["orders"].get(oid) or abort(404)
    if g.user["role"] != "Supervisor":
        return redirect(url_for("order_detail", oid=oid, err="Only a supervisor can approve this cancellation."))
    if o["status"] == "Provisioning" and o["cancel_requested"]:
        o["status"], o["charges"], o["cancel_requested"] = "Cancelled", 0.0, False
        _log(o, "Cancellation approved by supervisor - order cancelled")
        return redirect(url_for("order_detail", oid=oid,
                                ok=f"Cancellation of order {oid} approved. Order cancelled with no charges."))
    return redirect(url_for("order_detail", oid=oid, err="There is no pending cancellation to approve."))


UI_MAP = """MOCK CRM UI MAP (base URL {base})
Each new browser context starts with fresh demo data. Use these exact selectors.

LOGIN  {base}/login
  #username, #password, #login-button (submit)
  Users: csr1 / Password123! (role CSR)   sup1 / Super123! (role Supervisor)
  Bad credentials -> #error-message "Invalid username or password."
  Success -> redirects to {base}/orders ; #current-user shows the display name:
             "Asha (CSR)" for csr1, "Ravi (Supervisor)" for sup1 (not the username)
  Header links on EVERY page after login: #nav-orders (orders list), #nav-create-order (create form),
  #logout-link (back to /login). Use #nav-create-order to open the create form from any page,
  or page.goto("{base}/orders/new").

ORDERS LIST  {base}/orders
  #create-order-link -> create form (exists ONLY on this page) ; table #orders-table ; row #row-<ORDER_ID>
  Open an order: #order-link-<ORDER_ID>  e.g. #order-link-ORD-0003

CREATE ORDER  {base}/orders/new
  #order-id-input    text, optional unique Order ID (letters, digits, hyphens; 3-30 chars), auto-generated if blank
  #customer-account  <select> values: ACC-1001 (Active, Acme Corp), ACC-1002 (Active, Delta Retail),
                     ACC-2001 (Suspended, Beta LLC), ACC-3001 (Closed, Gamma Inc)
  #product-1, #product-2, #product-3  <select> values: FIBER-100, FIBER-500, TV-BASIC, ROUTER (line 1 required)
  #quantity-1, #quantity-2, #quantity-3  number inputs (default 1)
  #delivery-address  textarea
  #submit-order  button
  Success -> order page with #success-message "Order ORD-<n> created successfully with status New."
             (the entered Order ID, or an auto ID starting at ORD-1001); #order-id shows it
  Duplicate Order ID (e.g. an existing ORD-0001, or one created earlier in the same session) ->
             #error-message "Order ID <id> already exists. Please enter a unique Order ID."
  Note: the product dropdowns only offer the 4 product codes above - map any other product in a
        test case to one of them.
  Suspended -> #error-message "Order rejected: customer account ACC-2001 is suspended. Orders can only be created for active accounts."
  Closed    -> #error-message "Order rejected: customer account ACC-3001 is closed. ..."
  No product line -> #error-message "Order must contain at least one product line item."

ORDER DETAIL  {base}/orders/<ORDER_ID>
  Exact display text (use to_have_text / to_contain_text accordingly):
    #order-id       the order ID only, e.g. "ORD-1001"
    #order-status   one word: "New", "Pending", "Provisioning", "Completed" or "Cancelled"
    #order-customer "<account> - <customer name>", e.g. "ACC-1001 - Acme Corp" (use to_contain_text for the account)
    #order-charges  dollar amount, e.g. "$0.00"
    #order-total    dollar amount, e.g. "$80.00"
    #order-delivery the delivery address, or "-" when blank
  Line items: table #line-items, rows .line-item, quantity cell .line-qty
  #edit-order-link (only when status New or Pending)
  #cancel-order-button
  #approval-status "Cancellation pending supervisor approval." ; #approve-cancel-button (supervisor only)
  Audit log: <table id="audit-log">, rows <tr class="audit-entry"> (oldest first),
    cells <td class="audit-user"> and <td class="audit-action"> (these are tr/td, not div)
    .audit-user shows the USER ID that acted ("csr1" or "sup1"), never the display name
    .audit-action is exactly one of:
      "Order created" | "Order modified" | "Order cancelled"
      "Cancellation requested - awaiting supervisor approval"
      "Cancellation approved by supervisor - order cancelled"
    Seeded orders ORD-0001..ORD-0005 start with an EMPTY audit log (they have NO "Order created" row).
    Exact audit row count after each scenario (rows oldest first):
      Order created in this session, then cancelled by csr1 -> 2: ("csr1","Order created"), ("csr1","Order cancelled")
      Seeded ORD-0001 (New) or ORD-0002 (Pending) cancelled by csr1 -> 1: ("csr1","Order cancelled")
      Seeded ORD-0003 after csr1 requests cancellation -> 1: ("csr1","Cancellation requested - awaiting supervisor approval")
      Seeded ORD-0003 after sup1 approves -> 2: the request row above + ("sup1","Cancellation approved by supervisor - order cancelled")
      Seeded ORD-0004 (Completed) or ORD-0005 (Cancelled) after a refused cancel -> 0 rows
      Order created in this session, then modified by csr1 -> 2: ("csr1","Order created"), ("csr1","Order modified")
    Check a specific row with e.g. page.locator("#audit-log .audit-entry", has_text="Order cancelled")
    WHO did an action: narrow to that action's row first, then check its user cell:
      expect(page.locator("#audit-log .audit-entry", has_text="Order cancelled").locator(".audit-user")).to_have_text("csr1")
    Do NOT assert on ".audit-user" filtered by a user ID alone - the same user usually has several
    rows (e.g. "Order created" and "Order cancelled"), so that locator matches 2+ cells and fails
    Playwright's strict mode. Row count: expect(page.locator("#audit-log .audit-entry")).to_have_count(n)
  Messages: #success-message, #error-message, #info-message

EDIT ORDER  {base}/orders/<ORDER_ID>/edit
  #edit-quantity-1.., #remove-line-1.. (checkbox), #add-product (select), #edit-delivery-address, #save-order
  Success -> #success-message "Order <id> updated successfully." and an .audit-entry with action "Order modified"
  Provisioning/Completed/Cancelled orders -> redirected to detail with
    #error-message "Order <id> is in <status> status and cannot be modified."

SEEDED ORDERS
  ORD-0001 New (ACC-1001) | ORD-0002 Pending (ACC-1002) | ORD-0003 Provisioning (ACC-1001)
  ORD-0004 Completed (ACC-1002, charges $5.00) | ORD-0005 Cancelled

CANCELLATION RULES
  New/Pending -> status Cancelled, #order-charges "$0.00",
                 #success-message "Order <id> has been cancelled. No charges were applied."
  Provisioning -> #info-message "Order <id> is in Provisioning. Cancellation requires supervisor approval."
                  then log out, log in as sup1, open the order, click #approve-cancel-button ->
                  #success-message "Cancellation of order <id> approved. Order cancelled with no charges."
  Completed -> #error-message "Order <id> is Completed and cannot be cancelled. Please follow the returns process."
               status stays "Completed", charges unchanged
  Already Cancelled (e.g. ORD-0005) -> #error-message "Order <id> is already cancelled." ; status stays "Cancelled"
  There is NO confirmation dialog: clicking #cancel-order-button cancels immediately.
  Provisioning audit trail after approval (2 rows): ("csr1", "Cancellation requested - awaiting supervisor approval"),
               ("sup1", "Cancellation approved by supervisor - order cancelled")
  CSRs never see #approve-cancel-button; only sup1 does.
"""


@app.route("/ui-map")
def ui_map():
    return Response(UI_MAP.format(base=request.host_url.rstrip("/")), mimetype="text/plain")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=5050)
    a = ap.parse_args()
    print(f"Mock CRM running: http://{a.host}:{a.port}/login   (UI map: /ui-map)")
    app.run(host=a.host, port=a.port, threaded=True)
