"""The owner bills someone directly from the New invoice form.

    python3 tests/test_new_invoice.py
"""
import os
import sys
import tempfile
import unittest

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

import server as s  # noqa: E402


def bot():
    return s.q1("SELECT body FROM chat WHERE sender='bot' ORDER BY id DESC")["body"]


def send(**fields):
    s.sandbox("invoice/new", dict({"contact_id": "2", "title": "Menu photography reshoot", "amount": "450",
                                   "due_days": "10", "sub_share": "0", "sub_id": ""}, **fields))
    return s.q1("SELECT * FROM invoices ORDER BY id DESC")


class NewInvoice(unittest.TestCase):
    def setUp(self):
        FAKE.reset(client_id="cid", client_secret="secret")
        s._paypal = None
        s.reset()

    def test_existing_client_gets_a_paypal_invoice_and_email_at_once(self):
        inv = send()
        self.assertEqual((inv["number"], inv["amount"], inv["title"]), ("INV-1003", 450, "Menu photography reshoot"))
        pp = FAKE.invoices[inv["paypal_id"]]
        self.assertEqual((pp["status"], pp["amount"]["value"]), ("SENT", "450.00"))
        self.assertEqual(pp["detail"]["payment_term"]["due_date"], "2026-11-03")
        email = s.q1("SELECT * FROM outbox ORDER BY id DESC")
        self.assertIn("https://living.example/i/%s" % inv["token"], email["body"])
        self.assertIn("**Sent:** INV-1003 $450 to Tunde Bello (Brightside Cafe)", bot())
        self.assertIsNone(s.q1("SELECT id FROM proposals WHERE status='pending'"))  # no approval needed

    def test_new_client_is_saved_and_reused(self):
        inv = send(contact_id="new", new_name="Zainab Musa", new_company="Musa Bakes", new_email="zainab@musabakes.example")
        c = s.q1("SELECT * FROM contacts WHERE id=?", (inv["contact_id"],))
        self.assertEqual((c["name"], c["company"]), ("Zainab Musa", "Musa Bakes"))
        again = send(contact_id="new", new_name="Zainab M.", new_email="ZAINAB@musabakes.example")
        self.assertEqual(again["contact_id"], inv["contact_id"])

    def test_bad_input_is_refused_before_anything_is_sent(self):
        for fields, msg in [({"amount": "0"}, "more than zero"), ({"title": " "}, "what the invoice is for"),
                            ({"due_days": "500"}, "between 1 and 120"),
                            ({"contact_id": "new", "new_name": "X", "new_email": "not-an-email"}, "valid email"),
                            ({"sub_share": "500"}, "less than the invoice"),
                            ({"sub_share": "50"}, "who gets the share")]:
            with self.assertRaises(ValueError) as e:
                send(**fields)
            self.assertIn(msg, str(e.exception))
        self.assertEqual(FAKE.invoices, {})

    def test_subcontractor_share_is_paid_out_when_the_invoice_is_paid(self):
        inv = send(sub_share="150", sub_id="new", new_sub_name="Kemi Ade", new_sub_email="kemi@photo.example")
        self.assertIn("Kemi Ade gets $150 once it's paid", bot())
        order = s.create_checkout(inv["token"], "balance")
        s.fake_approve(inv["token"], order["order_id"])
        s.capture_checkout(inv["token"], order["order_id"])
        self.assertIn("**Approve?** Pay Kemi Ade $150 for “Menu photography reshoot”", bot())
        s.owner_message("approve")
        self.assertEqual(s.q1("SELECT amount, status FROM payouts"), {"amount": 150, "status": "unclaimed"})

    def test_undo_cancels_the_paypal_invoice(self):
        inv = send()
        s.owner_message("undo")
        self.assertEqual(FAKE.invoices[inv["paypal_id"]]["status"], "CANCELLED")
        self.assertEqual(s.q1("SELECT status FROM invoices WHERE id=?", (inv["id"],))["status"], "void")
        self.assertIn("Please disregard invoice INV-1003", s.q1("SELECT subject FROM outbox ORDER BY id DESC")["subject"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
