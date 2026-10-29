"""PayPal connector tests against the fake PayPal sandbox (no network, no real account).

    python3 tests/test_paypal.py
"""
import json
import os
import sys
import tempfile
import unittest
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import fake_paypal  # noqa: E402
from fake_paypal import FAKE  # noqa: E402

_server, BASE = fake_paypal.serve()
os.environ.update({
    "INVISIBLE_DB": os.path.join(tempfile.mkdtemp(), "test.db"), "INVISIBLE_LLM": "off",
    "PAYPAL_API_BASE": BASE, "PAYPAL_CLIENT_ID": "cid", "PAYPAL_CLIENT_SECRET": "secret",
    "PAYPAL_WEBHOOK_ID": FAKE.webhook_id, "PUBLIC_URL": "https://living.example",
})

import paypal_api as paypal  # noqa: E402
import server as s  # noqa: E402
from paypal_api import PayPalError  # noqa: E402


def payer(path, body=None):
    """What a client does on PayPal's own pages (fake hooks)."""
    req = urllib.request.Request(BASE + path, data=json.dumps(body or {}).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            return 200, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def deliver(event_index=-1, tamper=None):
    hook = FAKE.webhooks[event_index]
    body = hook["body"]
    if tamper:
        body = body.replace(*tamper)
    return s.paypal_webhook(hook["headers"], body)


def bot():
    return s.q1("SELECT body FROM chat WHERE sender='bot' ORDER BY id DESC")["body"]


def invoice_brand_workshop():
    """Marks the 1,200 Brand workshop done, approves the invoice, returns (local row, PayPal invoice)."""
    s.sandbox("event/complete", {"id": 1})
    pid = s.q1("SELECT id FROM proposals WHERE status='pending'")["id"]
    s.owner_message("approve #%d" % pid)
    inv = s.q1("SELECT * FROM invoices WHERE event_id=1")
    return inv, FAKE.invoices[inv["paypal_id"]]


class PayPalClient(unittest.TestCase):
    def setUp(self):
        FAKE.reset(client_id="cid", client_secret="secret")
        self.client = paypal.Client("cid", "secret", BASE)

    def test_token_is_cached_and_refreshed_once_on_401(self):
        self.client.search_invoices({})
        self.client.search_invoices({})
        self.assertEqual(sum(1 for c in FAKE.calls if c[1] == "/v1/oauth2/token"), 1)
        FAKE.tokens.clear()  # PayPal revoked or expired the token early
        self.client.search_invoices({})
        self.assertEqual(sum(1 for c in FAKE.calls if c[1] == "/v1/oauth2/token"), 2)

    def test_wrong_credentials_raise(self):
        with self.assertRaises(PayPalError) as e:
            paypal.Client("cid", "wrong", BASE).search_invoices({})
        self.assertEqual(e.exception.status, 401)

    def test_request_id_makes_writes_idempotent(self):
        body = paypal.build_invoice("T-1", {"name": "Ada Obi", "email": "ada@x.test"},
                                    [{"name": "Work", "amount": 100}], "USD", "2026-11-30")
        a = self.client.create_invoice(body, request_id="same")
        b = self.client.create_invoice(body, request_id="same")
        self.assertEqual(a["id"], b["id"])
        self.assertEqual(len(FAKE.invoices), 1)
        with self.assertRaises(PayPalError) as e:  # a new request with a used number is refused
            self.client.create_invoice(body, request_id="other")
        self.assertEqual(e.exception.details[0]["issue"], "DUPLICATE_INVOICE_NUMBER")

    def test_errors_carry_paypal_name_and_issue(self):
        with self.assertRaises(PayPalError) as e:
            self.client.get_invoice("INV2-NOPE")
        self.assertEqual((e.exception.status, e.exception.name), (404, "RESOURCE_NOT_FOUND"))

    def test_order_must_be_approved_before_capture(self):
        order = self.client.create_order(42.5, "USD", "INV-1", "li:1", "Deposit", request_id="o1")
        with self.assertRaises(PayPalError) as e:
            self.client.capture_order(order["id"])
        self.assertEqual(e.exception.details[0]["issue"], "ORDER_NOT_APPROVED")
        payer("/__fake/orders/%s/approve" % order["id"])
        captured = self.client.capture_order(order["id"], request_id="c1")
        cap_id, amount, status = paypal.capture_of(captured)
        self.assertEqual((amount, status), (42.5, "COMPLETED"))
        self.assertEqual(paypal.capture_of(self.client.capture_order(order["id"], request_id="c1"))[0], cap_id)

    def test_unclaimed_payout_can_be_cancelled(self):
        out = self.client.create_payout("b-1", "sub@contractor.test", 300, "USD", "Your share", "Payment")
        batch = self.client.get_payout_batch(out["batch_header"]["payout_batch_id"])
        item = batch["items"][0]
        self.assertEqual(item["transaction_status"], "UNCLAIMED")
        self.assertEqual(self.client.cancel_payout_item(item["payout_item_id"])["transaction_status"], "RETURNED")
        FAKE.known_receivers.add("pro@contractor.test")
        out = self.client.create_payout("b-2", "pro@contractor.test", 50, "USD", "n", "s")
        item = self.client.get_payout_batch(out["batch_header"]["payout_batch_id"])["items"][0]
        with self.assertRaises(PayPalError):
            self.client.cancel_payout_item(item["payout_item_id"])

    def test_apply_terms_round_trip(self):
        base = paypal.build_invoice("T-2", {"name": "A", "email": "a@x.test"},
                                    [{"name": "W", "amount": 10}], "USD", "2026-11-30")
        t = paypal.apply_terms(base, discount_pct=5, min_due=3, due_date="2026-12-15")
        self.assertEqual(t["amount"]["breakdown"]["discount"]["invoice_discount"]["percent"], "5")
        self.assertEqual(t["configuration"]["partial_payment"]["minimum_amount_due"]["value"], "3.00")
        back = paypal.apply_terms(t, discount_pct=0, min_due=0)
        self.assertNotIn("amount", back)  # PayPal recalculates; stale totals sent back are refused
        self.assertFalse(back["configuration"]["partial_payment"]["allow_partial_payment"])
        self.assertEqual(base["configuration"]["partial_payment"]["allow_partial_payment"], False)


class PayPalInvoicing(unittest.TestCase):
    def setUp(self):
        FAKE.reset(client_id="cid", client_secret="secret")
        s._paypal = None
        s.reset()

    def test_finished_job_becomes_a_sent_paypal_invoice(self):
        inv, pp = invoice_brand_workshop()
        self.assertEqual(pp["status"], "SENT")
        self.assertEqual(pp["amount"]["value"], "1200.00")
        self.assertEqual(pp["primary_recipients"][0]["billing_info"]["email_address"], "ruth@acmedental.example")
        self.assertEqual(pp["detail"]["invoice_number"], "INV-1003-%s" % s.get_setting("paypal_suffix"))
        self.assertIn("https://living.example/i/%s" % inv["token"], pp["detail"]["note"])
        self.assertTrue(inv["payer_url"].endswith("#" + pp["id"]))
        email = s.q1("SELECT body FROM outbox ORDER BY id DESC")["body"]
        self.assertIn("https://living.example/i/%s" % inv["token"], email)

    def test_terms_round_trip_from_a_real_shaped_get(self):
        # Real PayPal returns calculated totals; sending them back after removing a discount
        # used to fail with calculation_error.
        inv, pp = invoice_brand_workshop()
        acc = s.connector("accounting")
        acc.execute("update_terms", {"invoice_id": inv["id"], "discount_pct": 5})
        self.assertEqual(FAKE.invoices[pp["id"]]["amount"]["value"], "1140.00")
        acc.execute("update_terms", {"invoice_id": inv["id"], "discount_pct": 0})
        self.assertEqual(FAKE.invoices[pp["id"]]["amount"]["value"], "1200.00")
        with self.assertRaises(PayPalError) as e:  # what the old code sent
            stale = dict(FAKE.invoices[pp["id"]], amount={"value": "1140.00", "currency_code": "USD"})
            s.paypal_client().update_invoice(stale)
        self.assertEqual(e.exception.details[0]["issue"], "calculation_error")

    def test_payer_link_comes_back_from_a_single_link_reply(self):
        inv, pp = invoice_brand_workshop()
        self.assertEqual(inv["payer_url"], pp["detail"]["metadata"]["recipient_view_url"])
        self.assertEqual(paypal.payer_link({"href": "https://x/p", "rel": "payer-view"}), "https://x/p")

    def test_undo_cancels_the_paypal_invoice(self):
        inv, pp = invoice_brand_workshop()
        s.owner_message("undo #%d" % s.q1("SELECT id FROM proposals WHERE status='executed'")["id"])
        self.assertEqual(pp["status"], "CANCELLED")
        self.assertEqual(s.q1("SELECT status FROM invoices WHERE id=?", (inv["id"],))["status"], "void")

    def test_new_terms_reach_paypal_and_their_inverse_restores_them(self):
        inv, pp = invoice_brand_workshop()
        acc = s.connector("accounting")
        params = {"invoice_id": inv["id"], "discount_pct": 5, "min_due": 400, "due_at": "2026-12-20T12:00"}
        result = acc.execute("update_terms", params)
        pp = FAKE.invoices[inv["paypal_id"]]
        self.assertEqual(pp["amount"]["value"], "1140.00")
        self.assertEqual(pp["configuration"]["partial_payment"]["minimum_amount_due"]["value"], "400.00")
        self.assertEqual(pp["detail"]["payment_term"]["due_date"], "2026-12-20")
        inverse = acc.inverse("update_terms", params, result)
        acc.execute(inverse["op"], inverse["params"])
        pp = FAKE.invoices[inv["paypal_id"]]
        self.assertEqual(pp["amount"]["value"], "1200.00")
        self.assertFalse(pp["configuration"]["partial_payment"]["allow_partial_payment"])
        local = s.q1("SELECT * FROM invoices WHERE id=?", (inv["id"],))
        self.assertEqual((local["discount_pct"], local["min_due"]), (0, 0))

    def test_partial_payments_on_paypal_arrive_by_verified_webhook(self):
        inv, pp = invoice_brand_workshop()
        code, err = payer("/__fake/invoices/%s/pay" % pp["id"], {"amount": 400})
        self.assertEqual(err["details"][0]["issue"], "PARTIAL_PAYMENT_NOT_ALLOWED")
        s.connector("accounting").execute("update_terms", {"invoice_id": inv["id"], "min_due": 400})
        self.assertEqual(payer("/__fake/invoices/%s/pay" % pp["id"], {"amount": 300})[0], 422)
        self.assertEqual(payer("/__fake/invoices/%s/pay" % pp["id"], {"amount": 400})[0], 200)
        self.assertEqual(deliver(), 200)
        self.assertEqual(deliver(), 200)  # PayPal redelivers; nothing is counted twice
        local = s.q1("SELECT * FROM invoices WHERE id=?", (inv["id"],))
        self.assertEqual((local["amount_paid"], local["status"]), (400, "sent"))
        self.assertIn("paid $400 towards INV-1003", bot())
        payer("/__fake/invoices/%s/pay" % pp["id"], {"amount": 800})
        self.assertEqual(deliver(), 200)
        local = s.q1("SELECT * FROM invoices WHERE id=?", (inv["id"],))
        self.assertEqual((local["amount_paid"], local["status"]), (1200, "paid"))
        self.assertTrue(s.q1("SELECT id FROM chat WHERE body LIKE '%is now settled%'"))
        self.assertIn("**Approve?** Pay Ada Nwosu $300", bot())  # the subcontractor's share comes next

    def test_forged_webhooks_are_ignored(self):
        inv, pp = invoice_brand_workshop()
        payer("/__fake/invoices/%s/pay" % pp["id"])
        self.assertEqual(deliver(tamper=('"event_version": "1.0"', '"event_version": "9.9"')), 401)
        self.assertEqual(s.q1("SELECT status FROM invoices WHERE id=?", (inv["id"],))["status"], "sent")
        self.assertEqual(s.q1("SELECT event FROM audit ORDER BY id DESC")["event"], "webhook_rejected")
        self.assertEqual(s.paypal_webhook({}, "not json"), 400)

    def test_recorded_payment_is_idempotent_and_not_recounted_by_polling(self):
        inv, pp = invoice_brand_workshop()
        acc = s.connector("accounting")
        params = {"invoice_id": inv["id"], "amount": 500, "method": "PAYPAL", "reference": "capture:ABC"}
        acc.execute("record_payment", params)
        acc.execute("record_payment", params)
        self.assertEqual(len(FAKE.invoices[pp["id"]]["payments"]["transactions"]), 1)
        s.poll_paypal()
        local = s.q1("SELECT * FROM invoices WHERE id=?", (inv["id"],))
        self.assertEqual(local["amount_paid"], 500)
        self.assertEqual(local["paypal_status"], "PARTIALLY_PAID")

    def test_owner_marking_paid_records_it_in_paypal(self):
        inv, pp = invoice_brand_workshop()
        s.sandbox("invoice/paid", {"id": inv["id"]})
        self.assertEqual(FAKE.invoices[pp["id"]]["status"], "MARKED_AS_PAID")
        self.assertEqual(FAKE.invoices[pp["id"]]["payments"]["transactions"][0]["method"], "BANK_TRANSFER")
        self.assertEqual(s.q1("SELECT status FROM invoices WHERE id=?", (inv["id"],))["status"], "paid")

    def test_invoices_from_before_paypal_stay_local(self):
        s.sandbox("invoice/paid", {"id": 2})  # INV-1001 was seeded without a PayPal id
        self.assertEqual(s.q1("SELECT status FROM invoices WHERE id=2")["status"], "paid")
        self.assertEqual(FAKE.invoices, {})

    def test_paypal_outage_fails_the_action_without_a_local_invoice(self):
        FAKE.fail_next = (400, {"name": "INVALID_REQUEST", "message": "bad"})
        s.sandbox("event/complete", {"id": 1})
        pid = s.q1("SELECT id FROM proposals WHERE status='pending'")["id"]
        s.owner_message("approve #%d" % pid)
        self.assertEqual(s.q1("SELECT status FROM proposals WHERE id=?", (pid,))["status"], "failed")
        self.assertIsNone(s.q1("SELECT id FROM invoices WHERE event_id=1"))
        self.assertIn("Nothing else was changed", bot())


if __name__ == "__main__":
    unittest.main(verbosity=2)
