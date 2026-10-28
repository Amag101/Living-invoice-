"""The owner changes the invoice agent's rules in plain English, and the agent follows them.

    python3 tests/test_rules.py
"""
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
os.environ["INVISIBLE_DB"] = os.path.join(tempfile.mkdtemp(), "test.db")
os.environ["INVISIBLE_LLM"] = "off"
for k in ("PAYPAL_CLIENT_ID", "PAYPAL_CLIENT_SECRET", "PAYPAL_API_BASE"):
    os.environ.pop(k, None)  # offline accounting: these tests are about the rules

import server as s  # noqa: E402


def bot():
    return s.q1("SELECT body FROM chat WHERE sender='bot' ORDER BY id DESC")["body"]


class Rules(unittest.TestCase):
    def setUp(self):
        s._chat_times.clear()
        s.reset()

    def change(self, text):
        s.owner_message(text)
        return bot()

    def test_parser_reads_common_phrasings(self):
        p = s.parse_mandate_rules
        self.assertEqual(p("Allow up to 4 payments"), {"max_installments": 4})
        self.assertEqual(p("Give 3% off for paying today"), {"early_pay_discount_pct": 3})
        self.assertEqual(p("Let them extend the due date by up to 3 weeks"), {"max_extension_days": 21})
        self.assertEqual(p("Payment plans for returning clients only"), {"installments_for": "repeat_clients"})
        self.assertEqual(p("First payment at least 50%"), {"min_first_payment_pct": 50})
        self.assertEqual(p("Never go below $1,000"), {"floor_amount": 1000})
        self.assertEqual(p("Decline any discount over 15%"), {"escalate_discount_up_to_pct": 15})

    def test_owner_sees_the_change_and_must_confirm(self):
        msg = self.change("Allow up to 4 payments")
        self.assertIn("~~Payment plans of up to 3 payments", msg)
        self.assertIn("**Payment plans of up to 4 payments", msg)
        self.assertEqual(s.mandate()["max_installments"], 3)  # nothing changes before yes
        msg = self.change("yes")
        self.assertIn("Saved.", msg)
        self.assertEqual(s.mandate()["max_installments"], 4)
        self.assertEqual(s.q1("SELECT event FROM audit ORDER BY id DESC")["event"], "rules_changed")

    def test_new_rules_change_what_the_agent_agrees_to(self):
        token = s.q1("SELECT token FROM invoices WHERE number='INV-1001'")["token"]
        s.client_message(token, "Can I pay in 4 payments?")
        self.assertEqual(s.q1("SELECT decision FROM offers ORDER BY id DESC")["decision"], "ESCALATE")
        s.owner_message("skip")
        msg = self.change("allow up to 4 payments with 25% up front")  # 4 even parts start at 25%
        self.assertIn("**Payment plans of up to 4 payments, at least 25% up front", msg)
        self.change("yes")
        s.client_message(token, "Can I pay in 4 payments?")
        self.assertEqual(s.q1("SELECT decision FROM offers ORDER BY id DESC")["decision"], "ALLOW")

    def test_values_are_kept_sane(self):
        self.change("Give 90% off for paying today")
        self.change("yes")
        m = s.mandate()
        self.assertEqual(m["early_pay_discount_pct"], 50)
        self.assertGreaterEqual(m["escalate_discount_up_to_pct"], 50)

    def test_showing_rules_and_unclear_messages(self):
        self.assertIn("Up to 5% off for paying the whole balance now", self.change("rules"))
        self.assertIn("couldn't find a change", self.change("what about discounts generally"))

    def test_rules_from_claude_are_cleaned_too(self):
        s.LLM.client, original = object(), s.LLM._json
        s.LLM._json = lambda system, user, schema: dict({k: None for k in schema["required"]},
                                                        max_installments=40, max_extension_days=10)
        try:
            self.change("let people pay in as many parts as they like, and give 10 extra days")
        finally:
            s.LLM.client, s.LLM._json = None, original
        self.change("yes")
        self.assertEqual((s.mandate()["max_installments"], s.mandate()["max_extension_days"]), (12, 10))

    def test_reminder_rules_still_go_to_reminders(self):
        msg = self.change("remind clients 48 hours before appointments by email")
        self.assertNotIn("invoice agent", msg)


if __name__ == "__main__":
    unittest.main(verbosity=2)
