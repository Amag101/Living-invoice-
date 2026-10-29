"""
Minimal PayPal REST client: OAuth (client credentials), Invoicing v2,
Orders v2, Payouts v1 and webhook signature verification.

Standard library only. The base URL can be pointed elsewhere through
PAYPAL_API_BASE, which is how tests (and the offline demo) run against a fake
PayPal server. Every write takes a request id, sent as PayPal-Request-Id, so a
retried call never creates a second invoice, order or payout.
"""
import base64
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

SANDBOX_BASE = "https://api-m.sandbox.paypal.com"
LIVE_BASE = "https://api-m.paypal.com"

# Headers PayPal signs a webhook with; all five go to verify-webhook-signature.
WEBHOOK_HEADERS = {
    "auth_algo": "PAYPAL-AUTH-ALGO",
    "cert_url": "PAYPAL-CERT-URL",
    "transmission_id": "PAYPAL-TRANSMISSION-ID",
    "transmission_sig": "PAYPAL-TRANSMISSION-SIG",
    "transmission_time": "PAYPAL-TRANSMISSION-TIME",
}


class PayPalError(Exception):
    def __init__(self, status, name, message, debug_id=None, details=None):
        super().__init__("PayPal API %s %s: %s" % (status, name, message))
        self.status, self.name, self.message = status, name, message
        self.debug_id, self.details = debug_id, details or []


def base_url():
    if os.environ.get("PAYPAL_API_BASE"):
        return os.environ["PAYPAL_API_BASE"].rstrip("/")
    return LIVE_BASE if os.environ.get("PAYPAL_ENV", "sandbox") == "live" else SANDBOX_BASE


def load_client():
    """App credentials from PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET, or None."""
    cid, secret = os.environ.get("PAYPAL_CLIENT_ID"), os.environ.get("PAYPAL_CLIENT_SECRET")
    if cid and secret:
        return {"client_id": cid, "client_secret": secret}
    return None


def money(value, currency="USD"):
    """PayPal amounts are strings with two decimals."""
    return {"currency_code": currency, "value": "%.2f" % round(float(value), 2)}


class Client:
    """One app's credentials and its cached access token. Thread safe."""

    def __init__(self, client_id, client_secret, base=None, timeout=20):
        self.client_id, self.client_secret = client_id, client_secret
        self.base = (base or base_url()).rstrip("/")
        self.timeout = timeout
        self._token, self._expires = None, 0
        self._lock = threading.Lock()

    # ---- transport -------------------------------------------------------

    def _send(self, method, url, headers, data):
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        for attempt in (1, 2):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    raw = resp.read()
                    return json.loads(raw) if raw else {}
            except urllib.error.HTTPError as e:
                raw = e.read()
                try:
                    body = json.loads(raw)
                except ValueError:
                    body = {}
                # Writes carry PayPal-Request-Id, so retrying a 5xx is safe.
                if attempt == 1 and (e.code == 429 or e.code >= 500):
                    time.sleep(1)
                    continue
                name = body.get("name") or body.get("error") or str(e.code)
                message = body.get("message") or body.get("error_description") or e.reason
                raise PayPalError(e.code, name, message, body.get("debug_id"), body.get("details"))
            except urllib.error.URLError as e:
                if attempt == 1:
                    time.sleep(1)
                    continue
                raise PayPalError(0, "NETWORK", str(e.reason))

    def token(self, force=False):
        with self._lock:
            if force or not self._token or time.time() > self._expires - 60:
                basic = base64.b64encode(("%s:%s" % (self.client_id, self.client_secret)).encode()).decode()
                tok = self._send("POST", self.base + "/v1/oauth2/token", {
                    "Authorization": "Basic " + basic, "Accept": "application/json",
                    "Content-Type": "application/x-www-form-urlencoded",
                }, b"grant_type=client_credentials")
                self._token = tok["access_token"]
                self._expires = time.time() + int(tok.get("expires_in", 3600))
            return self._token

    def call(self, method, path, body=None, request_id=None, params=None, representation=False):
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = json.dumps(body).encode() if body is not None else None
        for attempt in (1, 2):
            headers = {"Authorization": "Bearer " + self.token(force=attempt == 2),
                       "Accept": "application/json", "Content-Type": "application/json"}
            if request_id:
                headers["PayPal-Request-Id"] = request_id
            if representation:
                headers["Prefer"] = "return=representation"
            try:
                return self._send(method, url, headers, data)
            except PayPalError as e:
                if e.status == 401 and attempt == 1:  # token expired or revoked early
                    continue
                raise

    # ---- Invoicing v2 ----------------------------------------------------

    def create_invoice(self, invoice, request_id=None):
        """Creates a draft invoice and returns the full invoice."""
        return self.call("POST", "/v2/invoicing/invoices", invoice, request_id, representation=True)

    def send_invoice(self, invoice_id, request_id=None, notify=True):
        """Sends a draft. Returns links, including the payer's view of the invoice."""
        return self.call("POST", "/v2/invoicing/invoices/%s/send" % invoice_id,
                         {"send_to_invoicer": False, "send_to_recipient": notify}, request_id)

    def get_invoice(self, invoice_id):
        return self.call("GET", "/v2/invoicing/invoices/%s" % invoice_id)

    def update_invoice(self, invoice, request_id=None, notify=False):
        """Full update (PUT). `invoice` must be the whole invoice including its id."""
        return self.call("PUT", "/v2/invoicing/invoices/%s" % invoice["id"], invoice, request_id,
                         params={"send_to_recipient": str(notify).lower(), "send_to_invoicer": "false"},
                         representation=True)

    def cancel_invoice(self, invoice_id, subject, note, request_id=None, notify=True):
        return self.call("POST", "/v2/invoicing/invoices/%s/cancel" % invoice_id,
                         {"subject": subject, "note": note,
                          "send_to_invoicer": False, "send_to_recipient": notify}, request_id)

    def remind(self, invoice_id, subject, note, request_id=None):
        return self.call("POST", "/v2/invoicing/invoices/%s/remind" % invoice_id,
                         {"subject": subject, "note": note,
                          "send_to_invoicer": False, "send_to_recipient": True}, request_id)

    def record_payment(self, invoice_id, amount, currency, payment_date, method="PAYPAL",
                       note=None, request_id=None):
        body = {"method": method, "payment_date": payment_date, "amount": money(amount, currency)}
        if note:
            body["note"] = note
        return self.call("POST", "/v2/invoicing/invoices/%s/payments" % invoice_id, body, request_id)

    def search_invoices(self, criteria, page_size=50):
        return self.call("POST", "/v2/invoicing/search-invoices", criteria,
                         params={"page": 1, "page_size": page_size, "total_required": "true"})

    # ---- Orders v2 -------------------------------------------------------

    def create_order(self, amount, currency, invoice_id, custom_id, description, request_id=None):
        unit = {"reference_id": custom_id, "custom_id": custom_id, "description": description[:127],
                "amount": money(amount, currency)}
        if invoice_id:  # PayPal rejects a second capture with the same invoice_id
            unit["invoice_id"] = invoice_id
        return self.call("POST", "/v2/checkout/orders", {"intent": "CAPTURE", "purchase_units": [unit]},
                         request_id, representation=True)

    def get_order(self, order_id):
        return self.call("GET", "/v2/checkout/orders/%s" % order_id)

    def capture_order(self, order_id, request_id=None):
        return self.call("POST", "/v2/checkout/orders/%s/capture" % order_id, {}, request_id,
                         representation=True)

    # ---- Payouts v1 ------------------------------------------------------

    def create_payout(self, batch_id, receiver_email, amount, currency, note, subject, request_id=None):
        return self.call("POST", "/v1/payments/payouts", {
            "sender_batch_header": {"sender_batch_id": batch_id, "email_subject": subject,
                                    "email_message": note},
            "items": [{"recipient_type": "EMAIL", "receiver": receiver_email,
                       "amount": {"value": "%.2f" % round(float(amount), 2), "currency": currency},
                       "note": note, "sender_item_id": batch_id + "-1"}],
        }, request_id or batch_id)

    def get_payout_batch(self, batch_id):
        return self.call("GET", "/v1/payments/payouts/%s" % batch_id)

    def get_payout_item(self, item_id):
        return self.call("GET", "/v1/payments/payouts-item/%s" % item_id)

    def cancel_payout_item(self, item_id):
        """Only works while the item is UNCLAIMED; the money returns to the sender."""
        return self.call("POST", "/v1/payments/payouts-item/%s/cancel" % item_id, {})

    # ---- Webhooks --------------------------------------------------------

    def verify_webhook(self, headers, event, webhook_id):
        """Asks PayPal whether a webhook delivery is genuine. `event` is the parsed body."""
        body = {k: headers.get(h) or headers.get(h.lower()) for k, h in WEBHOOK_HEADERS.items()}
        if not all(body.values()) or not webhook_id:
            return False
        body.update(webhook_id=webhook_id, webhook_event=event)
        out = self.call("POST", "/v1/notifications/verify-webhook-signature", body)
        return out.get("verification_status") == "SUCCESS"


# ---- Builders --------------------------------------------------------------

def build_invoice(number, recipient, items, currency, due_date, note=None, discount_pct=None,
                  min_due=None, invoicer_name=None):
    """A draft invoice body. `recipient` has name and email; items have name and amount."""
    given, _, surname = (recipient.get("name") or "").partition(" ")
    detail = {"invoice_number": number, "currency_code": currency,
              "payment_term": {"due_date": due_date}}
    if note:
        detail["note"] = note[:4000]
    invoice = {
        "detail": detail,
        "primary_recipients": [{"billing_info": {
            "name": {"given_name": given or recipient["email"], "surname": surname},
            "email_address": recipient["email"]}}],
        "items": [{"name": it["name"][:200], "quantity": "1", "unit_of_measure": "AMOUNT",
                   "unit_amount": money(it["amount"], currency)} for it in items],
        "configuration": {"allow_tip": False, "partial_payment": {"allow_partial_payment": False}},
    }
    if invoicer_name:
        invoice["invoicer"] = {"business_name": invoicer_name}
    return apply_terms(invoice, discount_pct=discount_pct, min_due=min_due)


def apply_terms(invoice, discount_pct=None, min_due=None, due_date=None):
    """Returns a copy of `invoice` with negotiated terms. None leaves a term as it is;
    a discount of 0 or a min_due of 0 removes it."""
    inv = json.loads(json.dumps(invoice))
    currency = inv["detail"]["currency_code"]
    # A GET returns PayPal's own calculated totals. Sent back after the terms change they no
    # longer add up (PayPal answers calculation_error), so only the discount percent goes back
    # and PayPal recalculates. Read-only parts of a GET are dropped for the same reason.
    for key in ("due_amount", "payments", "refunds", "links", "status"):
        inv.pop(key, None)
    if discount_pct is None:
        discount_pct = current_discount(invoice)
    inv.pop("amount", None)
    if discount_pct:
        inv["amount"] = {"breakdown": {"discount": {"invoice_discount": {"percent": "%g" % discount_pct}}}}
    if min_due is not None:
        if min_due:
            inv["configuration"]["partial_payment"] = {"allow_partial_payment": True,
                                                       "minimum_amount_due": money(min_due, currency)}
        else:
            inv["configuration"]["partial_payment"] = {"allow_partial_payment": False}
    if due_date:
        inv["detail"]["payment_term"] = {"due_date": due_date}
    return inv


def current_discount(invoice):
    pct = (invoice.get("amount", {}).get("breakdown", {}).get("discount", {})
           .get("invoice_discount", {}).get("percent"))
    return float(pct) if pct else 0


def payer_link(send_response, invoice=None):
    """Sending replies with the payer's link on its own ({"href", "rel": "payer-view"}) or in a list."""
    links = send_response.get("links") or [send_response]
    for link in links:
        if link.get("rel") in ("payer-view", "payer_view") and link.get("href"):
            return link["href"]
    return ((invoice or {}).get("detail", {}).get("metadata", {}).get("recipient_view_url"))


def capture_of(order):
    """The first capture in a captured order: (capture id, amount, status)."""
    for unit in order.get("purchase_units", []):
        for cap in unit.get("payments", {}).get("captures", []):
            return cap["id"], float(cap["amount"]["value"]), cap["status"]
    return None, 0.0, None
