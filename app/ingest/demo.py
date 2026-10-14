"""Deterministic synthetic ledger (12 months, ~3k transactions) for the public demo and evals.

It plants known facts so evals have exact answers: a subscription price increase, a duplicate
charge, a forgotten subscription, a one-off large purchase, a travel-heavy month, and a raise.
Each generated row carries its true category in `truth` (used by the categorization eval only).
"""
from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass
from datetime import date, timedelta

START, END = date(2025, 10, 1), date(2026, 9, 30)


@dataclass
class DemoTxn:
    date: date
    amount: float
    raw_name: str
    truth: str
    account: str = "checking"

    @property
    def id(self) -> str:
        return "demo_" + hashlib.sha1(f"{self.date}|{self.amount}|{self.raw_name}|{self.account}".encode()).hexdigest()[:16]


# (descriptor templates, category, (min, max) amount, weekly frequency)
VARIABLE = [
    (["WHOLEFDS BRK 10244", "TRADER JOE S #558", "KEY FOOD 1123 BKLYN", "INSTACART*SUBSCRIPTION ORDER"], "Groceries", (28, 140), 1.6),
    (["TST* JOE'S PIZZA", "SQ *DOMINIQUE ANSEL", "UBER   *EATS PENDING", "DOORDASH*SWEETGREEN", "CHIPOTLE 2291", "SHAKE SHACK 1098"], "Dining", (11, 68), 2.4),
    (["SQ *BLUE BOTTLE COFFEE", "STARBUCKS STORE 09912", "DEVOCION BKLYN"], "Coffee", (4.5, 9.5), 3.0),
    (["MTA*NYCT PAYGO", "UBER   *TRIP", "LYFT   *RIDE THU 8PM", "CITIBIKE 2394"], "Transport", (2.9, 38), 2.2),
    (["AMZN Mktp US*2K4L19", "TARGET 00012345", "UNIQLO USA 0332", "CVS/PHARMACY #0911"], "Shopping", (9, 120), 0.9),
    (["AMC 2938 ONLINE", "STEAMGAMES.COM 4259522", "TICKETMASTER*EVENT"], "Entertainment", (12, 85), 0.35),
    (["SUPERCUTS #1902", "SEPHORA.COM"], "Personal Care", (18, 70), 0.15),
]

# (descriptor, category, amount, day of month, start, end)
MONTHLY = [
    ("BLDG MGMT ACH RENT", "Rent & Mortgage", 2350.00, 1, START, END),
    ("CON ED OF NY  WEB PAY", "Utilities", None, 14, START, END),  # varies seasonally
    ("VERIZON WIRELESS PAYMNT", "Utilities", 65.00, 19, START, END),
    ("SPOTIFY USA", "Subscriptions", 11.99, 3, START, END),
    ("APPLE.COM/BILL ICLOUD", "Subscriptions", 2.99, 9, START, END),
    ("EQUINOX 0042 MEMBERSHIP", "Health & Fitness", 185.00, 5, START, END),
    ("HEADSPACE*SUBSCRIPTION", "Subscriptions", 12.99, 22, START, END),  # the "forgotten" one
    ("TRANSFER TO SAVINGS", "Transfers", 500.00, 16, START, END),
]
NETFLIX_OLD, NETFLIX_NEW, NETFLIX_CHANGE = 15.49, 17.99, date(2026, 4, 1)
RAISE_DATE = date(2026, 3, 1)
PAY_OLD, PAY_NEW = 3125.00, 3437.50


def _days():
    d = START
    while d <= END:
        yield d
        d += timedelta(days=1)


def generate(seed: int = 7) -> list[DemoTxn]:
    rnd = random.Random(seed)
    out: list[DemoTxn] = []

    for d in _days():
        # biweekly paycheck on Fridays
        if d.weekday() == 4 and (d - date(2025, 10, 3)).days % 14 == 0:
            pay = PAY_NEW if d >= RAISE_DATE else PAY_OLD
            out.append(DemoTxn(d, -pay, "ACME CORP PAYROLL PPD", "Income"))
        for raw, cat, amt, dom, s, e in MONTHLY:
            if d.day == dom and s <= d <= e:
                if amt is None:  # Con Ed: higher in winter and summer
                    amt = round(70 + 55 * (d.month in (12, 1, 2, 7, 8)) + rnd.uniform(-8, 8), 2)
                out.append(DemoTxn(d, amt, raw, cat))
        if d.day == 12:
            out.append(DemoTxn(d, NETFLIX_NEW if d >= NETFLIX_CHANGE else NETFLIX_OLD, "NETFLIX.COM", "Subscriptions", "credit"))
        if d.day == 25:
            out.append(DemoTxn(d, 4.75, "MONTHLY SERVICE FEE", "Fees & Interest"))
        for names, cat, (lo, hi), per_week in VARIABLE:
            if rnd.random() < per_week / 7:
                out.append(DemoTxn(d, round(rnd.uniform(lo, hi), 2), rnd.choice(names), cat, "credit"))

    # planted one-offs
    out += [
        DemoTxn(date(2026, 2, 7), 1899.00, "APPLE STORE R102 5TH AVE", "Shopping", "credit"),           # big purchase
        DemoTxn(date(2026, 6, 10), 642.30, "DELTA AIR 0062345981234", "Travel", "credit"),               # travel month
        DemoTxn(date(2026, 6, 14), 1180.00, "AIRBNB * HMXK2Q9", "Travel", "credit"),
        DemoTxn(date(2026, 6, 15), 86.40, "LISBOA TAXI 22", "Transport", "credit"),
        DemoTxn(date(2026, 6, 16), 54.10, "PASTEIS DE BELEM", "Dining", "credit"),
        DemoTxn(date(2026, 8, 3), 89.99, "BESTBUY 00001234", "Shopping", "credit"),                    # duplicate charge
        DemoTxn(date(2026, 8, 3), 89.99, "BESTBUY 00001234 ", "Shopping", "credit"),
        DemoTxn(date(2025, 12, 20), 310.00, "VENMO PAYMENT 1029384", "Transfers"),
        DemoTxn(date(2026, 3, 8), 60.00, "ZELLE PAYMENT TO JORDAN LEE 8829", "Transfers"),          # names to redact
        DemoTxn(date(2026, 7, 2), 45.00, "VENMO *Priya Shah", "Transfers"),
        DemoTxn(date(2026, 5, 2), 39.00, "LATE FEE", "Fees & Interest", "credit"),
    ]
    out.sort(key=lambda t: (t.date, t.raw_name))
    return out


# Planted facts the evals can reference without re-deriving.
FACTS = {
    "netflix_old": NETFLIX_OLD, "netflix_new": NETFLIX_NEW, "netflix_change_month": "2026-04",
    "duplicate": ("BESTBUY", 89.99, "2026-08-03"), "forgotten_subscription": "Headspace",
    "big_purchase": ("Apple Store", 1899.00, "2026-02"), "travel_month": "2026-06",
    "raise_month": "2026-03", "pay_old": PAY_OLD, "pay_new": PAY_NEW,
}
