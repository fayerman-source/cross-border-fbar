"""Turn an account spec into a full-year ledger and monthly statements, deterministically."""

from __future__ import annotations

import random

from .model import AccountSpec, Statement, Txn


def build_ledger(spec: AccountSpec, year: int, seed: int) -> list[Txn]:
    """Every transaction of the year in posting order, with running balances.

    Within a day, entries keep their generation order, so a spec controls posting
    order (e.g. a same-day debit posted before its matching credit). A routine
    debit that would overdraw the account is dropped; a scripted event that
    would overdraw it is a bug in the spec.
    """
    rng = random.Random(f"{seed}:{spec.account_id}")
    balance = spec.opening_balance
    txns: list[Txn] = []
    for month in range(1, 13):
        entries = spec.monthly(rng, year, month) + [e for e in spec.events if e.day.month == month]
        entries.sort(key=lambda e: e.day)  # stable
        n = 0
        for e in entries:
            if balance + e.amount < 0:
                if e.tag == "routine":
                    continue
                raise ValueError(f"{spec.account_id}: scripted {e.tag} on {e.day} overdraws the account")
            balance += e.amount
            n += 1
            txns.append(
                Txn(
                    f"{spec.account_id}-{year}{month:02d}-{n:03d}",
                    e.day,
                    e.description,
                    e.amount,
                    balance,
                    e.tag,
                )
            )
    return txns


def monthly_statements(spec: AccountSpec, year: int, ledger: list[Txn]) -> list[Statement]:
    """One statement per month, including months that will later be withheld as 'missing'."""
    statements = []
    balance = spec.opening_balance
    for month in range(1, 13):
        txns = tuple(t for t in ledger if t.day.month == month)
        closing = txns[-1].balance_after if txns else balance
        statements.append(
            Statement(spec.account_id, year, month, spec.bank_for(month), balance, closing, txns)
        )
        balance = closing
    return statements
