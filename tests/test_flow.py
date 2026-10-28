"""End-to-end checks of the engine, run against a throwaway database.

    python3 tests/test_flow.py
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

import server as s  # noqa: E402


def last_bot():
    return s.q1("SELECT * FROM chat WHERE sender='bot' ORDER BY id DESC")["body"]


def pending():
    return s.q("SELECT * FROM proposals WHERE status='pending' ORDER BY id")


def say(text):
    s.owner_message(text)
    return last_bot()


class Flow(unittest.TestCase):
    def setUp(self):
        s.reset()

    def test_invoice_on_completion_asks_then_creates_and_emails(self):
        s.sandbox("event/complete", {"id": 1})
        p = pending()
        self.assertEqual(len(p), 1)
        self.assertIn("Invoice Ruth Okafor", p[0]["summary"])
        say("yes")
        inv = s.q1("SELECT * FROM invoices WHERE event_id=1")
        self.assertEqual(inv["number"], "INV-1003")
        self.assertEqual(inv["status"], "sent")
        mail = s.q1("SELECT * FROM outbox ORDER BY id DESC")
        self.assertIn("INV-1003", mail["subject"])
        self.assertIn("$1,200", mail["body"])

    def test_undo_voids_invoice_and_sends_correction(self):
        s.sandbox("event/complete", {"id": 1})
        say("yes")
        reply = say("undo")
        self.assertIn("Undone", reply)
        self.assertEqual(s.q1("SELECT status FROM invoices WHERE event_id=1")["status"], "void")
        self.assertIn("disregard", s.q1("SELECT * FROM outbox ORDER BY id DESC")["subject"].lower())
        # An undone job can be invoiced again only by a new completion; nothing pending now.
        self.assertEqual(pending(), [])

    def test_amount_above_ceiling_always_asks(self):
        s.set_trust("invoice_on_completion", "auto_notice")
        s.sandbox("event/complete", {"id": 3})
        p = pending()
        self.assertEqual(len(p), 1)
        self.assertIn("above your $5,000 limit", json.loads(p[0]["reasons"])[0])

    def test_auto_notice_acts_and_reports(self):
        s.set_trust("invoice_on_completion", "auto_notice")
        s.sandbox("event/complete", {"id": 1})
        self.assertEqual(pending(), [])
        self.assertIn("**Done:**", last_bot())
        self.assertIsNotNone(s.q1("SELECT * FROM invoices WHERE event_id=1"))

    def test_clock_advance_triggers_overdue_and_first_contact_rule(self):
        s.sandbox("clock/advance", {"hours": 24})
        summaries = [p["summary"] for p in pending()]
        self.assertTrue(any("INV-1001" in x and "friendly" in x for x in summaries), summaries)
        self.assertTrue(any("INV-1002" in x and "firm" in x for x in summaries), summaries)
        # Appointment reminders are automatic, but Nnamdi has never been contacted -> asks.
        nnamdi = [p for p in pending() if "Nnamdi" in p["summary"]]
        self.assertEqual(len(nnamdi), 1)
        self.assertIn("first message", json.loads(nnamdi[0]["reasons"])[0])

    def test_paying_withdraws_pending_reminder(self):
        s.sandbox("clock/advance", {"hours": 24})
        rem = [p for p in pending() if "INV-1001" in p["summary"]][0]
        s.sandbox("invoice/paid", {"id": 2})
        self.assertEqual(s.q1("SELECT status FROM proposals WHERE id=?", (rem["id"],))["status"], "cancelled")
        self.assertIn("withdrawn", last_bot())

    def test_three_approvals_promote_to_automatic(self):
        # Three completions in a row, approved each time.
        for eid in (1, 4):
            s.sandbox("event/complete", {"id": eid})
            say("yes")
        s.sandbox("event/create", {"contact_id": 1, "title": "Follow-up", "start": "2026-10-24T12:00", "amount": 300})
        new_id = s.q1("SELECT id FROM cal_events ORDER BY id DESC")["id"]
        s.sandbox("event/complete", {"id": new_id})
        reply = say("yes")
        self.assertIn("automatically", reply)
        self.assertEqual(s.q1("SELECT trust FROM automations WHERE template='invoice_on_completion'")["trust"],
                         "auto_notice")

    def test_skip_does_nothing(self):
        s.sandbox("event/complete", {"id": 1})
        self.assertIn("Skipped", say("no"))
        self.assertIsNone(s.q1("SELECT * FROM invoices WHERE event_id=1"))

    def test_monday_summary(self):
        s.sandbox("clock/advance", {"hours": 48})  # Sat 09:00 -> Mon 09:00
        bodies = [m["body"] for m in s.q("SELECT body FROM chat WHERE sender='bot'")]
        self.assertTrue(any(b.startswith("**Your week at Kora Studio**") for b in bodies))

    def test_natural_language_setup(self):
        self.assertIn("48 hours", say("remind clients 48 hours before appointments by email"))
        say("yes")
        auto = s.q1("SELECT * FROM automations WHERE template='appointment_reminders'")
        self.assertEqual(json.loads(auto["params"]), {"hours_before": 48, "channel": "email"})

    def test_overdue_stages_from_text(self):
        say("chase overdue invoices at 5, 15 and 30 days on whatsapp")
        say("yes")
        auto = s.q1("SELECT * FROM automations WHERE template='overdue_reminders'")
        self.assertEqual(json.loads(auto["params"]), {"stages": [5, 15, 30], "channel": "whatsapp"})

    def test_trust_commands_and_pause(self):
        self.assertIn("asks you first", say("ask first for appointment reminders"))
        self.assertIn("Paused", say("pause summary"))
        self.assertIn("Resumed", say("resume summary"))
        self.assertIn("Do you mean", say("ask first for reminders"))

    def test_undo_of_automatic_action_demotes_trust(self):
        s.sandbox("clock/advance", {"hours": 24})
        say("approve #%d" % [p for p in pending() if "Nnamdi" in p["summary"]][0]["id"])
        # Next appointment reminder (Lagos Print, Mon 13:00) is automatic: contacted before.
        s.sandbox("clock/advance", {"hours": 6})
        auto_done = s.q1("SELECT * FROM proposals WHERE summary LIKE '%Rebrand%' AND status='executed'")
        self.assertIsNotNone(auto_done)
        reply = say("undo #%d" % auto_done["id"])
        self.assertIn("ask you first", reply)
        self.assertIn("Couldn't reverse", reply)

    def test_audit_log_is_append_only_record(self):
        s.sandbox("event/complete", {"id": 1})
        say("yes")
        events = [r["event"] for r in s.q("SELECT event FROM audit ORDER BY id")]
        self.assertEqual(events[-3:], ["proposed", "approved", "executed"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
