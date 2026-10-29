"""The invoice agent earns wider rules: three approvals of the same kind of request in a row
lead to an offer to agree to those itself, and an undo narrows the rule back.

    python3 tests/test_growth.py
"""
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
os.environ["INVISIBLE_DB"] = os.path.join(tempfile.mkdtemp(), "test.db")
os.environ["INVISIBLE_LLM"] = "off"
for k in ("PAYPAL_CLIENT_ID", "PAYPAL_CLIENT_SECRET", "PAYPAL_API_BASE"):
    os.environ.pop(k, None)

import server as s  # noqa: E402


def bot():
    return s.q1("SELECT body FROM chat WHERE sender='bot' ORDER BY id DESC")["body"]


class Growth(unittest.TestCase):
    def setUp(self):
        s._chat_times.clear()
        s.reset()
        self.n = 0

    def invoice(self, amount=1000):
        self.n += 1
        s.sandbox("invoice/new", {"contact_id": str(1 + self.n % 3), "title": "Job %d" % self.n,
                                  "amount": str(amount), "due_days": "14"})
        return s.q1("SELECT token FROM invoices ORDER BY id DESC")["token"]

    def ask(self, text, token=None):
        token = token or self.invoice()
        s.client_message(token, text)
        return s.q1("SELECT * FROM offers ORDER BY id DESC")

    def ask_and_decide(self, text, answer="approve"):
        o = self.ask(text)
        self.assertEqual(o["decision"], "ESCALATE")
        s.owner_message(answer)
        return o

    def test_three_approvals_offer_a_wider_rule_at_the_lowest_approved_value(self):
        for pct in (10, 8, 12):
            self.ask_and_decide("Can I get %d%% off?" % pct)
        msg = bot()
        self.assertIn("You've approved the last 3 requests for **a discount without paying in full**", msg)
        self.assertIn("~~No discount unless they pay in full now~~ → **Up to 8% off with no condition**", msg)
        s.owner_message("yes")
        self.assertEqual(s.mandate()["max_discount_pct"], 8)
        self.assertEqual(self.ask("Can I get 8% off?")["decision"], "ALLOW")
        self.assertEqual(self.ask("Can I get 9% off?")["decision"], "ESCALATE")

    def test_a_decline_breaks_the_streak(self):
        self.ask_and_decide("Can I get 10% off?")
        self.ask_and_decide("Can I get 10% off?")
        self.ask_and_decide("Can I get 10% off?", "skip")
        self.ask_and_decide("Can I get 10% off?")
        self.assertNotIn("You've approved the last", bot())
        self.assertIsNone(s.q1("SELECT id FROM proposals WHERE kind='mandate'"))

    def test_saying_no_waits_for_three_more(self):
        for _ in range(3):
            self.ask_and_decide("Can I get 10% off?")
        s.owner_message("no")
        self.assertEqual(s.mandate()["max_discount_pct"], 0)
        for _ in range(2):
            self.ask_and_decide("Can I get 10% off?")
        self.assertEqual(s.q1("SELECT COUNT(*) n FROM proposals WHERE kind='mandate'")["n"], 1)
        self.ask_and_decide("Can I get 10% off?")
        self.assertEqual(s.q1("SELECT COUNT(*) n FROM proposals WHERE kind='mandate'")["n"], 2)

    def test_undo_under_a_grown_rule_narrows_it_back(self):
        for _ in range(3):
            self.ask_and_decide("Can I get 10% off?")
        s.owner_message("yes")
        o = self.ask("Can I get 10% off?")
        self.assertEqual((o["decision"], o["status"]), ("ALLOW", "applied"))
        s.owner_message("undo")
        msg = bot()
        self.assertIn("Because you undid that, I've narrowed the rule back", msg)
        self.assertIn("~~Up to 10% off with no condition~~ → **No discount unless they pay in full now**", msg)
        self.assertEqual(s.mandate()["max_discount_pct"], 0)
        self.assertEqual(self.ask("Can I get 10% off?")["decision"], "ESCALATE")

    def test_undo_inside_the_original_rules_changes_nothing(self):
        o = self.ask("Any discount if I pay today?")
        self.assertEqual(o["decision"], "ALLOW")
        before = s.mandate()
        s.owner_message("undo")
        self.assertEqual(s.mandate(), before)
        self.assertNotIn("narrowed", bot())
        self.assertNotIn("ask you first", bot())  # the negotiation automation isn't demoted either
        self.assertEqual(s.q1("SELECT trust FROM automations WHERE template='invoice_negotiation'")["trust"],
                         "auto_notice")

    def test_payment_plans_grow_too(self):
        for _ in range(3):
            self.ask_and_decide("Can I pay in 4 payments?")
        msg = bot()
        self.assertIn("**payment plans**", msg)
        self.assertIn("up to 4 payments, at least 25% up front", msg)
        s.owner_message("yes")
        self.assertEqual((s.mandate()["max_installments"], s.mandate()["min_first_payment_pct"]), (4, 25))
        self.assertEqual(self.ask("Can I pay in 4 payments?")["decision"], "ALLOW")

    def test_the_owners_counters_do_not_count_as_approvals(self):
        for _ in range(3):
            o = self.ask("Can I get 10% off?")
            p = s.q1("SELECT id FROM proposals WHERE status='pending'")["id"]
            s.owner_message("counter #%d 6%%" % p)
        self.assertIsNone(s.q1("SELECT id FROM proposals WHERE kind='mandate'"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
