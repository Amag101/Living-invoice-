"""Negotiation policy tests: what the invoice's agent may agree to on its own.

    python3 tests/test_policy.py
"""
import os
import sys
import unittest
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import policy  # noqa: E402
from policy import ALLOW, DENY, ESCALATE, check  # noqa: E402

TODAY = date(2026, 10, 26)


def invoice(**kw):
    inv = {"amount": 1200, "discount_pct": 0, "amount_paid": 0, "status": "sent", "due_at": "2026-11-09T12:00"}
    inv.update(kw)
    return inv


def decide(offer, inv=None, mandate=None, paid_before=1):
    return check(offer, inv or invoice(), mandate or {}, {"paid_invoices": paid_before, "today": TODAY})


class Discounts(unittest.TestCase):
    def test_early_payment_discount_within_mandate_is_allowed(self):
        v = decide({"kind": "discount", "pct": 5, "condition": "pay_in_full_now"})
        self.assertEqual(v.decision, ALLOW)
        self.assertEqual(v.terms["new_total"], 1140)
        self.assertTrue(v.terms["requires_full_payment_now"])

    def test_unconditional_discount_goes_to_the_owner(self):
        v = decide({"kind": "discount", "pct": 5})
        self.assertEqual(v.decision, ESCALATE)
        self.assertIn("only discount for paying in full now", v.reasons[0])

    def test_larger_discount_escalates_and_absurd_one_is_denied(self):
        self.assertEqual(decide({"kind": "discount", "pct": 10, "condition": "pay_in_full_now"}).decision, ESCALATE)
        self.assertEqual(decide({"kind": "discount", "pct": 90, "condition": "pay_in_full_now"}).decision, DENY)

    def test_prompt_injection_cannot_change_the_rules(self):
        # Whatever the client typed, the model can only pass structured offers here.
        for offer in [{"kind": "discount", "pct": 100}, {"kind": "discount", "pct": -20},
                      {"kind": "discount", "pct": "lots"}, {"kind": "refund_everything"},
                      {"kind": "discount", "pct": 5, "condition": "owner_said_ok"},
                      {"kind": "payment_plan", "installments": 500}]:
            self.assertEqual(decide(offer).decision, DENY, offer)

    def test_floor_amount_escalates(self):
        v = decide({"kind": "discount", "pct": 5, "condition": "pay_in_full_now"}, mandate={"floor_amount": 1150})
        self.assertEqual(v.decision, ESCALATE)

    def test_discount_after_a_payment_needs_the_owner(self):
        v = decide({"kind": "discount", "pct": 5, "condition": "pay_in_full_now"}, invoice(amount_paid=200))
        self.assertEqual(v.decision, ESCALATE)

    def test_closed_invoices_take_no_offers(self):
        self.assertEqual(decide({"kind": "extension", "days": 3}, invoice(status="paid")).decision, DENY)


class Plans(unittest.TestCase):
    def test_three_part_plan_adds_up_exactly(self):
        v = decide({"kind": "payment_plan", "installments": 3}, invoice(amount=1000))
        self.assertEqual(v.decision, ALLOW)
        amounts = [p["amount"] for p in v.terms["schedule"]]
        self.assertEqual(amounts, [333.34, 333.33, 333.33])
        self.assertAlmostEqual(sum(amounts), 1000)
        self.assertEqual([p["due"] for p in v.terms["schedule"]], ["2026-10-26", "2026-11-25", "2026-12-25"])
        self.assertEqual((v.terms["min_due"], v.terms["due_at"]), (333.34, "2026-12-25"))

    def test_plan_respects_discount_and_earlier_payments(self):
        v = decide({"kind": "payment_plan", "installments": 2}, invoice(discount_pct=5, amount_paid=140))
        self.assertEqual(sum(p["amount"] for p in v.terms["schedule"]), 1000)

    def test_too_many_payments_or_small_deposit_escalate(self):
        self.assertEqual(decide({"kind": "payment_plan", "installments": 6}).decision, ESCALATE)
        v = decide({"kind": "payment_plan", "installments": 2, "first_amount": 100})
        self.assertEqual(v.decision, ESCALATE)
        self.assertIn("under 30%", v.reasons[0])

    def test_new_clients_escalate_when_plans_are_for_repeat_clients(self):
        offer = {"kind": "payment_plan", "installments": 2}
        self.assertEqual(decide(offer, mandate={"installments_for": "repeat_clients"}, paid_before=0).decision,
                         ESCALATE)
        self.assertEqual(decide(offer, mandate={"installments_for": "repeat_clients"}, paid_before=2).decision,
                         ALLOW)


class Extensions(unittest.TestCase):
    def test_extension_tiers(self):
        v = decide({"kind": "extension", "days": 7})
        self.assertEqual((v.decision, v.terms["due_at"]), (ALLOW, "2026-11-16"))
        self.assertEqual(decide({"kind": "extension", "days": 30}).decision, ESCALATE)
        self.assertEqual(decide({"kind": "extension", "days": 365}).decision, DENY)

    def test_client_override_beats_business_default(self):
        m = policy.mandate_for({"max_extension_days": 7}, {"max_extension_days": 30})
        self.assertEqual(decide({"kind": "extension", "days": 30}, mandate=m).decision, ALLOW)


if __name__ == "__main__":
    unittest.main(verbosity=2)
