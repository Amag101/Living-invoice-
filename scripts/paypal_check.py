#!/usr/bin/env python3
"""
Checks every PayPal call Living Invoice relies on against the real PayPal sandbox,
using the credentials in .env, and prints PASS / FAIL for each. Creates a few
sandbox invoices, an order and a payout; cancels what it can afterwards.
Sandbox only: it refuses to run with PAYPAL_ENV=live.

    python3 scripts/paypal_check.py
"""
import os
import secrets
import time
import sys
from datetime import date, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def load_env(path):
    if not os.path.exists(path):
        return
    for line in open(path):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_env(os.path.join(ROOT, ".env"))
os.environ.pop("PAYPAL_API_BASE", None)  # always the real sandbox here
if os.environ.get("PAYPAL_ENV", "sandbox") != "sandbox":
    sys.exit("This check only runs against the sandbox. Set PAYPAL_ENV=sandbox.")

import paypal_api as paypal  # noqa: E402
from paypal_api import PayPalError  # noqa: E402

RESULTS = []


def check(name, fn):
    try:
        note = fn()
        RESULTS.append((True, name, note or ""))
        print("PASS  %s%s" % (name, ("  (" + note + ")") if note else ""))
        return True
    except PayPalError as e:
        if e.status >= 500:  # PayPal's own fault, not a problem with the request
            RESULTS.append((None, name, "PayPal error %s %s" % (e.status, e.name)))
            print("PAYPAL ERROR  %s  -> %s %s (a fault on PayPal's side; our request was valid)" % (name, e.status, e.name))
            return False
        return failed(name, e)
    except (AssertionError, KeyError) as e:
        return failed(name, e)


def failed(name, e):
    detail = e.message if isinstance(e, PayPalError) else str(e)
    if isinstance(e, PayPalError):
        issues = ", ".join(d.get("issue", "") for d in e.details if d.get("issue"))
        detail = "%s %s: %s%s" % (e.status, e.name, detail, (" [" + issues + "]") if issues else "")
    RESULTS.append((False, name, detail))
    print("FAIL  %s  -> %s" % (name, detail))
    return False


def main():
    creds = paypal.load_client()
    if not creds:
        sys.exit("No PAYPAL_CLIENT_ID / PAYPAL_CLIENT_SECRET found in .env.")
    client = paypal.Client(creds["client_id"], creds["client_secret"], paypal.SANDBOX_BASE)
    buyer = os.environ.get("PAYPAL_TEST_BUYER_EMAIL") or "client.%s@example.com" % secrets.token_hex(3)
    tag = secrets.token_hex(3).upper()
    due = (date.today() + timedelta(days=14)).isoformat()
    state = {}

    def body(n):
        return paypal.build_invoice("LI-CHECK-%s-%d" % (tag, n), {"name": "Check Client", "email": buyer},
                                    [{"name": "Living Invoice check", "amount": 1200}], "USD", due,
                                    note="Created by Living Invoice's sandbox check.", invoicer_name="Kora Studio")

    print("PayPal sandbox check (%s)\n" % paypal.SANDBOX_BASE)
    if not check("1. Sign in with your app's credentials", lambda: bool(client.token()) and None):
        print("\nStopping: the Client ID or Secret in .env isn't accepted. Copy them again from the sandbox app.")
        return summary()

    def create():
        inv = client.create_invoice(body(1), request_id="li-check-%s-1" % tag)
        state["a"] = inv
        assert inv.get("id") and inv.get("status") == "DRAFT", "unexpected reply: %s" % inv.get("status")
        return inv["id"]
    if not check("2. Create a draft invoice", create):
        return summary()

    def send():
        out = client.send_invoice(state["a"]["id"], request_id="li-check-%s-1s" % tag)
        state["payer"] = paypal.payer_link(out)
        assert client.get_invoice(state["a"]["id"])["status"] in ("SENT", "UNPAID"), "not sent"
        return "payer link %s" % ("returned" if state["payer"] else "missing")
    check("3. Send it", send)

    def update_sent():
        current = client.get_invoice(state["a"]["id"])
        later = (date.today() + timedelta(days=30)).isoformat()
        updated = client.update_invoice(paypal.apply_terms(current, discount_pct=5, min_due=400, due_date=later),
                                        request_id="li-check-%s-1u" % tag)
        fresh = client.get_invoice(state["a"]["id"])
        got = (fresh["amount"]["value"], fresh["configuration"]["partial_payment"].get("allow_partial_payment"),
               fresh["detail"]["payment_term"].get("due_date"))
        assert got == ("1140.00", True, later), "PayPal kept %s" % (got,)
        assert updated is not None
        return "5% off -> 1140.00, part payments from 400, due date moved"
    check("4. Change the terms of a SENT invoice (discount, plan minimum, due date)", update_sent)

    def undo_terms():
        current = client.get_invoice(state["a"]["id"])
        client.update_invoice(paypal.apply_terms(current, discount_pct=0, min_due=0, due_date=due),
                              request_id="li-check-%s-1r" % tag)
        fresh = client.get_invoice(state["a"]["id"])
        assert fresh["amount"]["value"] == "1200.00", "total is %s" % fresh["amount"]["value"]
        return "back to 1200.00"
    check("5. Undo those terms", undo_terms)

    def remind():
        client.remind(state["a"]["id"], "Reminder", "Friendly reminder from the sandbox check.",
                      request_id="li-check-%s-1m" % tag)
    check("6. Send a payment reminder", remind)

    def record_partial():
        out = client.record_payment(state["a"]["id"], 400, "USD", date.today().isoformat(), method="PAYPAL",
                                    note="Check: part payment", request_id="li-check-%s-1p" % tag)
        state["payment_id"] = out.get("payment_id")
        fresh = client.get_invoice(state["a"]["id"])
        return "status %s, due %s, payment id %s" % (fresh["status"], fresh.get("due_amount", {}).get("value"),
                                                     "returned" if state["payment_id"] else "missing")
    check("7. Record a part payment made with PayPal", record_partial)

    def record_rest():
        client.record_payment(state["a"]["id"], 800, "USD", date.today().isoformat(), method="PAYPAL",
                              note="Check: the rest", request_id="li-check-%s-1q" % tag)
        return "status %s" % client.get_invoice(state["a"]["id"])["status"]
    check("8. Record the rest (invoice paid in full)", record_rest)

    def cancel():
        inv = client.create_invoice(body(2), request_id="li-check-%s-2" % tag)
        client.send_invoice(inv["id"], request_id="li-check-%s-2s" % tag)
        client.cancel_invoice(inv["id"], "Cancelled", "Sandbox check.", request_id="li-check-%s-2c" % tag)
        assert client.get_invoice(inv["id"])["status"] == "CANCELLED"
    check("9. Cancel a sent invoice (what undo does)", cancel)

    def order():
        o = client.create_order(400, "USD", None, "li:check", "Living Invoice check", request_id="li-check-%s-o" % tag)
        assert o.get("status") == "CREATED", o.get("status")
        return "order %s created; a buyer approves it in PayPal's popup" % o["id"]
    check("10. Create a checkout order (Orders v2)", order)

    def payout():
        out = client.create_payout("li-check-%s" % tag, "subcontractor.%s@example.com" % tag.lower(), 25, "USD",
                                   "Sandbox check", "Living Invoice check")
        bid = out["batch_header"]["payout_batch_id"]
        items = client.get_payout_batch(bid).get("items", [])
        state["item"] = items[0] if items else {}
        return "batch %s, item status %s" % (out["batch_header"].get("batch_status"),
                                             state["item"].get("transaction_status", "not ready yet"))
    has_payouts = check("11. Send a payout (Payouts v1)", payout)

    def cancel_payout():
        item = state.get("item") or {}
        assert item.get("payout_item_id"), "no payout item id yet (PayPal still processing the batch)"
        status, batch = item.get("transaction_status"), "PENDING"
        for _ in range(30):  # PayPal finishes the item, then the batch, some seconds after accepting it
            batch = client.get_payout_batch(item["payout_batch_id"])["batch_header"]["batch_status"]
            status = client.get_payout_item(item["payout_item_id"]).get("transaction_status")
            if status != "PENDING" and batch not in ("PENDING", "PROCESSING"):
                break
            time.sleep(3)
        assert status == "UNCLAIMED", "payout is %s (batch %s), so it can't be cancelled" % (status, batch)
        out = client.cancel_payout_item(item["payout_item_id"])
        return "was UNCLAIMED, now %s" % out.get("transaction_status")
    if has_payouts:
        check("12. Cancel an unclaimed payout (what undo does)", cancel_payout)

    wid = os.environ.get("PAYPAL_WEBHOOK_ID")
    print("\nSkipped: webhook signatures (%s). They need the app hosted at a public HTTPS address first."
          % ("PAYPAL_WEBHOOK_ID is set" if wid else "no PAYPAL_WEBHOOK_ID yet"))
    return summary()


def summary():
    ok = sum(1 for r in RESULTS if r[0])
    paypal_side = sum(1 for r in RESULTS if r[0] is None)
    print("\n%d of %d checks passed%s." % (ok, len(RESULTS), (", %d hit a PayPal-side error" % paypal_side)
                                          if paypal_side else ""))
    for passed, name, detail in RESULTS:
        if passed is False:
            print("  - %s: %s" % (name, detail))


if __name__ == "__main__":
    main()
