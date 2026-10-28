"""
The negotiation policy: what the invoice's agent may agree to on its own.

The model proposes an offer, this module decides, and only then does anything
touch PayPal. It is plain, deterministic Python on purpose: nothing a client
types into the chat can change these rules, however it is phrased.

    check(offer, invoice, mandate, history) -> Verdict

Verdicts:
    ALLOW     within the owner's mandate; the agent can agree straight away
    ESCALATE  plausible but outside the mandate; the owner decides on their phone
    DENY      never acceptable; the agent declines politely, the owner isn't bothered

Amounts are handled in cents so a plan always adds up to the exact balance.
"""
from dataclasses import dataclass, field
from datetime import date, timedelta

DEFAULT_MANDATE = {
    # Discounts
    "early_pay_discount_pct": 5,     # allowed if the whole balance is paid now
    "max_discount_pct": 0,           # allowed with no condition attached
    "escalate_discount_up_to_pct": 25,  # above this the agent declines outright
    "floor_amount": None,            # never agree to a total below this
    # Payment plans
    "max_installments": 3,
    "min_first_payment_pct": 30,
    "max_installment_gap_days": 31,
    "installments_for": "anyone",    # or "repeat_clients"
    # Due dates
    "max_extension_days": 14,
    "escalate_extension_up_to_days": 60,
}

ALLOW, ESCALATE, DENY = "ALLOW", "ESCALATE", "DENY"
HARD_MAX_INSTALLMENTS = 12
OPEN_STATUSES = ("sent",)


@dataclass
class Verdict:
    decision: str
    reasons: list = field(default_factory=list)
    terms: dict = field(default_factory=dict)  # what changes on the invoice if this goes ahead

    @property
    def allowed(self):
        return self.decision == ALLOW

    def as_dict(self):
        return {"decision": self.decision, "reasons": self.reasons, "terms": self.terms}


def cents(x):
    return int(round(float(x) * 100))


def mandate_for(business_default, client_override=None):
    m = dict(DEFAULT_MANDATE)
    m.update(business_default or {})
    m.update(client_override or {})
    return m


def worst(*decisions):
    order = {ALLOW: 0, ESCALATE: 1, DENY: 2}
    return max(decisions, key=order.get)


def check(offer, invoice, mandate, history=None):
    """`offer` is {"kind": ..., ...} from the agent. `invoice` has amount,
    discount_pct, amount_paid, status and due_at. `history` has paid_invoices
    (how many this client has paid before) and today (a date)."""
    history = history or {}
    m = mandate_for(mandate)
    kind = offer.get("kind")
    if invoice.get("status") not in OPEN_STATUSES:
        return Verdict(DENY, ["the invoice is %s" % invoice.get("status")])
    checker = {"discount": _discount, "payment_plan": _plan, "extension": _extension}.get(kind)
    if not checker:
        return Verdict(DENY, ["the agent can't offer “%s”" % kind])
    try:
        return checker(offer, invoice, m, history)
    except (KeyError, TypeError, ValueError) as e:
        return Verdict(DENY, ["the offer was malformed (%s)" % e])


# ---- discounts -----------------------------------------------------------

def _discount(offer, inv, m, history):
    pct = float(offer["pct"])
    condition = offer.get("condition", "none")
    if not 0 < pct < 100:
        return Verdict(DENY, ["%g%% isn't a valid discount" % round(pct, 1)])
    if condition not in ("none", "pay_in_full_now"):
        return Verdict(DENY, ["unknown condition “%s”" % condition])
    already = float(inv.get("discount_pct") or 0)
    new_total = cents(inv["amount"]) * (100 - pct) / 100.0
    terms = {"discount_pct": pct, "new_total": round(new_total / 100, 2),
             "requires_full_payment_now": condition == "pay_in_full_now"}
    if pct > m["escalate_discount_up_to_pct"]:
        return Verdict(DENY, ["%g%% is far beyond anything the owner allows" % round(pct, 1)])
    if pct <= already:
        return Verdict(DENY, ["a %g%% discount is already applied" % already])
    if inv.get("amount_paid"):
        if new_total < cents(inv["amount_paid"]):
            return Verdict(DENY, ["the client has already paid more than that total"])
        return Verdict(ESCALATE, ["part of the invoice is already paid, so a discount needs the owner"], terms)

    reasons, decision = [], ALLOW
    limit = m["early_pay_discount_pct"] if condition == "pay_in_full_now" else 0
    limit = max(limit, m["max_discount_pct"])
    if pct > limit:
        decision = ESCALATE
        if condition == "pay_in_full_now" and limit:
            reasons.append("%g%% is above the %g%% early-payment discount" % (round(pct, 1), limit))
        elif limit:
            reasons.append("%g%% is above the %g%% the agent may give" % (round(pct, 1), limit))
        else:
            reasons.append("the agent may only discount for paying in full now")

    if m.get("floor_amount") is not None and new_total < cents(m["floor_amount"]):
        decision = worst(decision, ESCALATE)
        reasons.append("it would take the total below the owner's floor")
    return Verdict(decision, reasons or ["within the owner's discount rules"], terms)


# ---- payment plans -------------------------------------------------------

def plan_schedule(balance_cents, n, first_cents, start, gap_days):
    """n dated installments adding up to exactly balance_cents; the first is due on `start`."""
    rest = balance_cents - first_cents
    each, extra = divmod(rest, n - 1) if n > 1 else (0, 0)
    amounts = [first_cents] + [each + (1 if i < extra else 0) for i in range(n - 1)]
    return [{"due": (start + timedelta(days=gap_days * i)).isoformat(), "amount": a / 100.0}
            for i, a in enumerate(amounts)]


def _plan(offer, inv, m, history):
    n = int(offer["installments"])
    gap = int(offer.get("gap_days") or 30)
    total = cents(inv["amount"]) * (100 - float(inv.get("discount_pct") or 0)) / 100.0
    balance = int(round(total)) - cents(inv.get("amount_paid") or 0)
    if balance <= 0:
        return Verdict(DENY, ["nothing is left to pay"])
    if not 2 <= n <= HARD_MAX_INSTALLMENTS:
        return Verdict(DENY, ["a plan needs between 2 and %d payments" % HARD_MAX_INSTALLMENTS])
    if not 1 <= gap <= 92:
        return Verdict(DENY, ["payments must be between 1 and 92 days apart"])
    first = cents(offer["first_amount"]) if offer.get("first_amount") is not None else -(-balance // n)
    if not 0 < first < balance:
        return Verdict(DENY, ["the first payment must be more than zero and less than the balance"])

    reasons, decision = [], ALLOW
    if n > m["max_installments"]:
        decision = ESCALATE
        reasons.append("%d payments is more than the %d the agent may agree" % (n, m["max_installments"]))
    if first * 100 < balance * m["min_first_payment_pct"]:
        decision = ESCALATE
        reasons.append("the first payment is under %g%% of the balance" % m["min_first_payment_pct"])
    if gap > m["max_installment_gap_days"]:
        decision = ESCALATE
        reasons.append("payments %d days apart is longer than %d" % (gap, m["max_installment_gap_days"]))
    if m["installments_for"] == "repeat_clients" and not history.get("paid_invoices"):
        decision = ESCALATE
        reasons.append("plans are only automatic for repeat clients, and this is a new one")
    today = history.get("today") or date.today()
    schedule = plan_schedule(balance, n, first, today, gap)
    terms = {"installments": n, "gap_days": gap, "schedule": schedule,
             "min_due": schedule[0]["amount"], "due_at": schedule[-1]["due"]}
    return Verdict(decision, reasons or ["within the owner's payment plan rules"], terms)


# ---- due date extensions -------------------------------------------------

def _extension(offer, inv, m, history):
    days = int(offer["days"])
    if days <= 0:
        return Verdict(DENY, ["an extension has to be at least one day"])
    if days > m["escalate_extension_up_to_days"]:
        return Verdict(DENY, ["%d days is far beyond anything the owner allows" % days])
    due = date.fromisoformat(str(inv["due_at"])[:10])
    terms = {"due_at": (due + timedelta(days=days)).isoformat(), "days": days}
    if days > m["max_extension_days"]:
        return Verdict(ESCALATE, ["%d days is longer than the %d-day extension the agent may give"
                                  % (days, m["max_extension_days"])], terms)
    return Verdict(ALLOW, ["within the owner's extension rules"], terms)
