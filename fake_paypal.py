#!/usr/bin/env python3
"""
A fake PayPal sandbox for tests and for running the demo without PayPal credentials.

It implements the slice of the REST API that paypal_api.py uses: OAuth client
credentials, Invoicing v2, Orders v2, Payouts v1 and webhook signature
verification, with PayPal-Request-Id idempotency and PayPal-shaped errors.
It also sends webhooks, signed so that only verify-webhook-signature on this
same fake accepts them.

Actions a real payer would take in PayPal's own pages are test hooks under
/__fake/ (pay an invoice, approve an order).

    python3 fake_paypal.py [--port 8791] [--webhook http://localhost:8790/webhooks/paypal]

then start the app with PAYPAL_API_BASE=http://localhost:8791 and any
PAYPAL_CLIENT_ID / PAYPAL_CLIENT_SECRET.
"""
import argparse
import base64
import hashlib
import hmac
import json
import secrets
import threading
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SIGNING_KEY = secrets.token_bytes(32)
PAID_STATES = ("PAID", "MARKED_AS_PAID")


def _id(prefix, n=16):
    return prefix + secrets.token_hex(n // 2).upper()


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _cents(value):
    return int(round(float(value) * 100))


def _money(cents, currency):
    return {"currency_code": currency, "value": "%.2f" % (cents / 100)}


class Fake:
    """All state of the fake PayPal. Reset between tests."""

    def __init__(self):
        self.reset()

    def reset(self, client_id=None, client_secret=None):
        self.client_id, self.client_secret = client_id, client_secret  # None accepts any
        self.tokens = set()
        self.invoices, self.orders, self.batches, self.items = {}, {}, {}, {}
        self.idempotent = {}
        self.reminders, self.cancelled = [], []
        self.webhooks = []          # every event sent, newest last
        self.webhook_url = None
        self.webhook_id = "WH-FAKE-1"
        self.signatures = {}
        self.known_receivers = set()  # payout receivers with a PayPal account claim at once
        self.hold_payouts = False   # keep payouts PENDING, as PayPal does while it processes them
        self.fail_cancel = False    # PayPal's sandbox answers payout cancels with a 500
        self.fail_next = None       # (status, body) returned by the next API call
        self.calls = []

    # ---- invoices --------------------------------------------------------

    def total(self, inv):
        cur = inv["detail"]["currency_code"]
        items = sum(_cents(i["unit_amount"]["value"]) * int(float(i.get("quantity", "1")))
                    for i in inv.get("items", []))
        pct = float(inv.get("amount", {}).get("breakdown", {}).get("discount", {})
                    .get("invoice_discount", {}).get("percent", 0) or 0)
        discount = int(round(items * pct / 100))
        paid = sum(_cents(t["amount"]["value"]) for t in inv.get("payments", {}).get("transactions", []))
        total = items - discount
        inv["amount"] = dict(inv.get("amount", {}), currency_code=cur, value="%.2f" % (total / 100))
        inv["amount"].setdefault("breakdown", {})["item_total"] = _money(items, cur)
        if discount:
            inv["amount"]["breakdown"]["discount"]["invoice_discount"]["amount"] = _money(discount, cur)
        inv["due_amount"] = _money(max(total - paid, 0), cur)
        inv.setdefault("payments", {})["paid_amount"] = _money(paid, cur)
        return total, paid

    def add_payment(self, inv, cents, method, note=None, ptype="EXTERNAL"):
        cur = inv["detail"]["currency_code"]
        pid = ("EXTR-" if ptype == "EXTERNAL" else "") + _id("", 16)
        inv.setdefault("payments", {}).setdefault("transactions", []).append({
            "payment_id": pid, "type": ptype, "method": method, "note": note or "",
            "payment_date": _now()[:10], "amount": _money(cents, cur)})
        total, paid = self.total(inv)
        if paid >= total:
            inv["status"] = "PAID" if ptype == "PAYPAL" else "MARKED_AS_PAID"
        else:
            inv["status"] = "PARTIALLY_PAID"
        return pid

    def settle(self, item):
        if item["transaction_status"] == "PENDING" and item.get("_settles_to") and not self.hold_payouts:
            item["transaction_status"] = item.pop("_settles_to")
            self.emit("PAYMENT.PAYOUTS-ITEM." + ("SUCCEEDED" if item["transaction_status"] == "SUCCESS"
                                                  else "UNCLAIMED"), item)

    # ---- webhooks --------------------------------------------------------

    def emit(self, event_type, resource):
        event = {"id": _id("WH-", 20), "event_version": "1.0", "create_time": _now(),
                 "resource_type": event_type.split(".")[1].lower(), "event_type": event_type,
                 "summary": event_type, "resource": json.loads(json.dumps(resource))}
        tid, ttime = _id("", 16), _now()
        body = json.dumps(event)
        sig = base64.b64encode(hmac.new(SIGNING_KEY, ("%s|%s|%s|%s" % (
            tid, ttime, self.webhook_id, hashlib.sha256(body.encode()).hexdigest())).encode(),
            hashlib.sha256).digest()).decode()
        headers = {"PAYPAL-AUTH-ALGO": "SHA256withRSA", "PAYPAL-CERT-URL": "https://fake.paypal/cert.pem",
                   "PAYPAL-TRANSMISSION-ID": tid, "PAYPAL-TRANSMISSION-SIG": sig,
                   "PAYPAL-TRANSMISSION-TIME": ttime}
        self.signatures[tid] = sig
        self.webhooks.append({"headers": headers, "body": body, "event": event})
        if self.webhook_url:
            threading.Thread(target=self._deliver, args=(headers, body), daemon=True).start()
        return event

    def _deliver(self, headers, body):
        req = urllib.request.Request(self.webhook_url, data=body.encode(), method="POST",
                                     headers=dict(headers, **{"Content-Type": "application/json"}))
        try:
            urllib.request.urlopen(req, timeout=10).read()
        except Exception as e:  # PayPal would retry; the fake just reports it
            print("fake PayPal: webhook delivery failed:", e)

    def verify(self, body):
        event = body.get("webhook_event") or {}
        tid = body.get("transmission_id")
        expected = base64.b64encode(hmac.new(SIGNING_KEY, ("%s|%s|%s|%s" % (
            tid, body.get("transmission_time"), body.get("webhook_id"),
            hashlib.sha256(json.dumps(event).encode()).hexdigest())).encode(),
            hashlib.sha256).digest()).decode()
        ok = (tid in self.signatures and hmac.compare_digest(expected, body.get("transmission_sig") or "")
              and body.get("webhook_id") == self.webhook_id)
        return "SUCCESS" if ok else "FAILURE"


FAKE = Fake()


class ApiError(Exception):
    def __init__(self, status, name, message, issue=None):
        self.status = status
        self.body = {"name": name, "message": message, "debug_id": _id("", 12).lower()}
        if issue:
            self.body["details"] = [{"issue": issue, "description": message}]


def unprocessable(issue, message):
    return ApiError(422, "UNPROCESSABLE_ENTITY", message, issue)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def reply(self, code, obj=None):
        data = json.dumps(obj).encode() if obj is not None else b""
        self.send_response(code)
        if data:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def read_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        if self.headers.get("Content-Type", "").startswith("application/x-www-form-urlencoded"):
            return dict(urllib.parse.parse_qsl(raw.decode()))
        return json.loads(raw) if raw else {}

    def do_GET(self):
        self.route("GET")

    def do_POST(self):
        self.route("POST")

    def do_PUT(self):
        self.route("PUT")

    def route(self, method):
        url = urllib.parse.urlparse(self.path)
        path, query = url.path, dict(urllib.parse.parse_qsl(url.query))
        body = self.read_json()
        FAKE.calls.append((method, path))
        try:
            if path == "/v1/oauth2/token":
                return self.reply(200, self.oauth())
            if path.startswith("/__fake/"):
                return self.reply(200, self.hook(path, body))
            if FAKE.fail_next:
                status, err = FAKE.fail_next
                FAKE.fail_next = None
                return self.reply(status, err)
            token = (self.headers.get("Authorization") or "").replace("Bearer ", "")
            if token not in FAKE.tokens:
                return self.reply(401, {"error": "invalid_token",
                                        "error_description": "Token signature verification failed"})
            rid = self.headers.get("PayPal-Request-Id")
            key = (method, path, rid)
            if rid and key in FAKE.idempotent:
                return self.reply(*FAKE.idempotent[key])
            status, out = self.api(method, path, query, body)
            if rid and status < 400:
                FAKE.idempotent[key] = (status, out)
            return self.reply(status, out)
        except ApiError as e:
            return self.reply(e.status, e.body)

    def oauth(self):
        auth = self.headers.get("Authorization") or ""
        try:
            cid, _, secret = base64.b64decode(auth.replace("Basic ", "")).decode().partition(":")
        except ValueError:
            cid = secret = ""
        if FAKE.client_id and (cid, secret) != (FAKE.client_id, FAKE.client_secret):
            raise ApiError(401, "invalid_client", "Client Authentication failed")
        token = "A21" + secrets.token_urlsafe(24)
        FAKE.tokens.add(token)
        return {"scope": "https://uri.paypal.com/services/invoicing", "access_token": token,
                "token_type": "Bearer", "app_id": "APP-FAKE", "expires_in": 32400}

    def invoice(self, inv_id):
        inv = FAKE.invoices.get(inv_id)
        if not inv:
            raise ApiError(404, "RESOURCE_NOT_FOUND", "The specified resource does not exist.", "INVALID_RESOURCE_ID")
        return inv

    def api(self, method, path, query, body):
        parts = path.strip("/").split("/")
        prefer = "return=representation" in (self.headers.get("Prefer") or "")

        # ---- Invoicing ----
        if parts[:3] == ["v2", "invoicing", "invoices"]:
            if len(parts) == 3 and method == "POST":
                number = body.get("detail", {}).get("invoice_number")
                if any(i["detail"].get("invoice_number") == number for i in FAKE.invoices.values()):
                    raise unprocessable("DUPLICATE_INVOICE_NUMBER", "Invoice number already exists.")
                if not body.get("primary_recipients"):
                    raise ApiError(400, "INVALID_REQUEST", "Recipient is required.", "MISSING_REQUIRED_PARAMETER")
                inv = json.loads(json.dumps(body))
                inv["id"] = "INV2-" + "-".join(secrets.token_hex(2).upper() for _ in range(5))
                inv["status"] = "DRAFT"
                inv["detail"]["metadata"] = {"create_time": _now()}
                FAKE.total(inv)
                FAKE.invoices[inv["id"]] = inv
                href = "%s/v2/invoicing/invoices/%s" % (self.base(), inv["id"])
                return 201, inv if prefer else {"rel": "self", "href": href, "method": "GET"}
            inv = self.invoice(parts[3])
            action = parts[4] if len(parts) > 4 else None
            if action is None and method == "GET":
                return 200, inv
            if action is None and method == "PUT":
                if inv["status"] in PAID_STATES + ("CANCELLED", "REFUNDED"):
                    raise unprocessable("CANNOT_UPDATE_INVOICE", "Invoice in %s state can't be updated." % inv["status"])
                keep = {k: inv[k] for k in ("id", "status", "payments") if k in inv}
                new = json.loads(json.dumps(body))
                new.update(keep)
                new["detail"].setdefault("metadata", inv["detail"].get("metadata", {}))
                stated = (body.get("amount") or {}).get("value")
                total, paid = FAKE.total(new)
                # Like PayPal: a stated total that disagrees with the items and discount is refused.
                if stated is not None and _cents(stated) != total:
                    raise ApiError(400, "INVALID_REQUEST", "Request is not well-formed, syntactically incorrect, "
                                   "or violates schema.", "calculation_error")
                if paid > total:
                    raise unprocessable("INVALID_AMOUNT", "Invoice total can't be lower than the amount paid.")
                FAKE.invoices[inv["id"]] = new
                return 200, new if prefer else {}
            if action == "send":
                if inv["status"] != "DRAFT":
                    raise unprocessable("INVALID_INVOICE_STATUS", "Only a draft can be sent.")
                inv["status"] = "SENT"
                view = "%s/invoice/p/#%s" % (self.base(), inv["id"])
                inv["detail"].setdefault("metadata", {})["recipient_view_url"] = view
                return 200, {"href": view, "rel": "payer-view", "method": "GET"}  # one link, as PayPal sends it
            if action == "cancel":
                if inv["status"] not in ("SENT", "UNPAID", "SCHEDULED"):
                    raise unprocessable("CANNOT_CANCEL_INVOICE", "Invoice in %s state can't be cancelled." % inv["status"])
                inv["status"] = "CANCELLED"
                FAKE.cancelled.append({"id": inv["id"], "note": body.get("note")})
                return 204, None
            if action == "remind":
                if inv["status"] not in ("SENT", "UNPAID", "PARTIALLY_PAID"):
                    raise unprocessable("CANNOT_REMIND", "Invoice in %s state can't be reminded." % inv["status"])
                FAKE.reminders.append({"id": inv["id"], "subject": body.get("subject"), "note": body.get("note")})
                return 204, None
            if action == "payments" and method == "POST":
                if inv["status"] not in ("SENT", "UNPAID", "PARTIALLY_PAID"):
                    raise unprocessable("INVALID_INVOICE_STATUS", "Can't record a payment on a %s invoice." % inv["status"])
                cents = _cents(body["amount"]["value"])
                if cents <= 0 or cents > _cents(inv["due_amount"]["value"]):
                    raise unprocessable("INVALID_PAYMENT_AMOUNT", "Payment can't exceed the amount due.")
                return 200, {"payment_id": FAKE.add_payment(inv, cents, body.get("method", "OTHER"), body.get("note"))}
        if path == "/v2/invoicing/search-invoices":
            found = list(FAKE.invoices.values())
            if body.get("status"):
                found = [i for i in found if i["status"] in body["status"]]
            return 200, {"items": found, "total_items": len(found), "total_pages": 1}

        # ---- Orders ----
        if parts[:3] == ["v2", "checkout", "orders"]:
            if len(parts) == 3 and method == "POST":
                if body.get("intent") != "CAPTURE" or not body.get("purchase_units"):
                    raise ApiError(400, "INVALID_REQUEST", "intent and purchase_units are required.")
                oid = _id("", 17)
                order = {"id": oid, "intent": "CAPTURE", "status": "CREATED",
                         "purchase_units": json.loads(json.dumps(body["purchase_units"])),
                         "create_time": _now(),
                         "links": [{"rel": "approve", "method": "GET",
                                    "href": "%s/checkoutnow?token=%s" % (self.base(), oid)}]}
                FAKE.orders[oid] = order
                return 201, order if prefer else {"id": oid, "status": "CREATED", "links": order["links"]}
            order = FAKE.orders.get(parts[3])
            if not order:
                raise ApiError(404, "RESOURCE_NOT_FOUND", "The specified resource does not exist.", "INVALID_RESOURCE_ID")
            if len(parts) == 4 and method == "GET":
                return 200, order
            if parts[4] == "capture":
                if order["status"] == "COMPLETED":
                    raise unprocessable("ORDER_ALREADY_CAPTURED", "Order already captured.")
                if order["status"] != "APPROVED":
                    raise unprocessable("ORDER_NOT_APPROVED", "Payer has not yet approved the Order for payment.")
                unit = order["purchase_units"][0]
                cap = {"id": _id("", 17), "status": "COMPLETED", "amount": unit["amount"],
                       "invoice_id": unit.get("invoice_id"), "custom_id": unit.get("custom_id"),
                       "final_capture": True, "create_time": _now()}
                unit["payments"] = {"captures": [cap]}
                order["status"] = "COMPLETED"
                order["payer"] = {"email_address": "client@personal.example", "payer_id": _id("", 13)}
                FAKE.emit("PAYMENT.CAPTURE.COMPLETED", cap)
                return 201, order

        # ---- Payouts ----
        if path == "/v1/payments/payouts" and method == "POST":
            header = body.get("sender_batch_header", {})
            # Real PayPal also rejects a reused sender_batch_id for 30 days.
            if any(b["batch_header"]["sender_batch_header"]["sender_batch_id"] == header.get("sender_batch_id")
                   for b in FAKE.batches.values()):
                raise ApiError(400, "USER_BUSINESS_ERROR", "Batch with given sender_batch_id already exists",
                               "DUPLICATE_REQUEST_ID")
            bid = _id("", 13)
            batch = {"batch_header": {"payout_batch_id": bid, "batch_status": "PROCESSING",
                                      "sender_batch_header": header, "time_created": _now()}, "items": []}
            for item in body.get("items", []):
                iid = _id("", 13)
                # PayPal processes payouts after replying: items start PENDING and settle later
                # (here, the next time anyone looks at them).
                row = {"payout_item_id": iid, "transaction_status": "PENDING", "payout_batch_id": bid,
                       "payout_item": json.loads(json.dumps(item)), "time_processed": _now(),
                       "_settles_to": "SUCCESS" if item["receiver"] in FAKE.known_receivers else "UNCLAIMED"}
                FAKE.items[iid] = row
                batch["items"].append(row)
            FAKE.batches[bid] = batch
            return 201, {"batch_header": dict(batch["batch_header"], batch_status="PENDING")}
        if parts[:3] == ["v1", "payments", "payouts"] and len(parts) == 4 and method == "GET":
            batch = FAKE.batches.get(parts[3])
            if not batch:
                raise ApiError(404, "RESOURCE_NOT_FOUND", "Batch not found.", "INVALID_RESOURCE_ID")
            for row in batch["items"]:
                FAKE.settle(row)
            if not FAKE.hold_payouts:
                batch["batch_header"]["batch_status"] = "SUCCESS"
            return 200, batch
        if parts[:3] == ["v1", "payments", "payouts-item"] and len(parts) == 4 and method == "GET":
            item = FAKE.items.get(parts[3])
            if not item:
                raise ApiError(404, "RESOURCE_NOT_FOUND", "Item not found.", "INVALID_RESOURCE_ID")
            FAKE.settle(item)
            return 200, item
        if parts[:3] == ["v1", "payments", "payouts-item"] and len(parts) == 5 and parts[4] == "cancel":
            item = FAKE.items.get(parts[3])
            if not item:
                raise ApiError(404, "RESOURCE_NOT_FOUND", "Item not found.", "INVALID_RESOURCE_ID")
            if FAKE.fail_cancel:
                raise ApiError(500, "INTERNAL_SERVICE_ERROR", "An internal service error has occurred")
            if item["transaction_status"] != "UNCLAIMED":
                raise ApiError(400, "ITEM_INCORRECT_STATUS", "Only items in Unclaimed status can be cancelled.")
            item["transaction_status"] = "RETURNED"
            FAKE.emit("PAYMENT.PAYOUTS-ITEM.RETURNED", item)
            return 200, item

        # ---- Webhooks ----
        if path == "/v1/notifications/verify-webhook-signature":
            return 200, {"verification_status": FAKE.verify(body)}

        raise ApiError(404, "NOT_FOUND", "No fake for %s %s" % (method, path))

    def hook(self, path, body):
        """What a payer does on PayPal's own pages."""
        parts = path.strip("/").split("/")
        if parts[1:2] == ["invoices"] and parts[3:] == ["pay"]:
            inv = self.invoice(parts[2])
            due = _cents(inv["due_amount"]["value"])
            cents = _cents(body["amount"]) if body.get("amount") else due
            pp = inv["configuration"].get("partial_payment", {})
            if inv["status"] not in ("SENT", "UNPAID", "PARTIALLY_PAID"):
                raise unprocessable("INVALID_INVOICE_STATUS", "This invoice can't be paid.")
            if cents < due:
                if not pp.get("allow_partial_payment"):
                    raise unprocessable("PARTIAL_PAYMENT_NOT_ALLOWED", "This invoice must be paid in full.")
                minimum = _cents(pp.get("minimum_amount_due", {}).get("value", 0))
                if cents < min(minimum, due):
                    raise unprocessable("BELOW_MINIMUM", "Payment is below the minimum amount due.")
            if cents > due:
                raise unprocessable("INVALID_PAYMENT_AMOUNT", "Payment can't exceed the amount due.")
            FAKE.add_payment(inv, cents, "PAYPAL", ptype="PAYPAL")
            FAKE.emit("INVOICING.INVOICE.PAID", {"invoice": inv})
            return inv
        if parts[1:2] == ["orders"] and parts[3:] == ["approve"]:
            order = FAKE.orders.get(parts[2])
            if not order or order["status"] != "CREATED":
                raise unprocessable("ORDER_CANNOT_BE_APPROVED", "Order can't be approved.")
            order["status"] = "APPROVED"
            return order
        if parts[1:] == ["webhook"]:
            FAKE.webhook_url = body.get("url")
            return {"webhook_url": FAKE.webhook_url, "webhook_id": FAKE.webhook_id}
        if parts[1:] == ["reset"]:
            FAKE.reset()
            return {}
        raise ApiError(404, "NOT_FOUND", "No fake hook " + path)

    def base(self):
        return "http://%s" % (self.headers.get("Host") or "localhost")


def serve(port=0):
    """Starts the fake on a background thread and returns (server, base_url)."""
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, "http://127.0.0.1:%d" % server.server_address[1]


def main():
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument("--port", type=int, default=8791)
    ap.add_argument("--webhook", help="URL to deliver webhooks to")
    args = ap.parse_args()
    FAKE.webhook_url = args.webhook
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print("Fake PayPal sandbox on http://localhost:%d (webhooks to %s, webhook id %s)"
          % (args.port, args.webhook or "nowhere", FAKE.webhook_id))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
