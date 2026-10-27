"""Data types for the synthetic household. Money is integer minor units (won, cents, satang)."""

from __future__ import annotations

import random
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date

# Digits after the decimal point. KRW has no minor unit.
EXPONENT = {"KRW": 0, "EUR": 2, "THB": 2}


@dataclass(frozen=True)
class Entry:
    """A transaction before balances exist. Positive = credit."""

    day: date
    description: str
    amount: int
    tag: str = "routine"  # routine | spike_in | spike_out | sameday_in | sameday_out | interest


@dataclass(frozen=True)
class Txn:
    txn_id: str
    day: date
    description: str
    amount: int
    balance_after: int
    tag: str


@dataclass(frozen=True)
class Bank:
    key: str
    name: str  # as printed on the statement
    name_en: str
    address: str
    layout: str  # renderer layout key
    bic: str | None = None


@dataclass(frozen=True)
class BankSpan:
    bank: Bank
    first_month: int
    last_month: int


MonthlyFn = Callable[[random.Random, int, int], list[Entry]]


@dataclass(frozen=True)
class AccountSpec:
    account_id: str
    currency: str
    account_number: str
    product: str  # as printed
    holder_printed: str  # account holder line as printed
    ownership: str  # sole | joint | signature_authority
    financial_interest: bool
    owners: tuple[str, ...]
    opening_balance: int
    banks: tuple[BankSpan, ...]
    monthly: MonthlyFn
    events: tuple[Entry, ...] = ()
    missing_months: tuple[int, ...] = ()

    def bank_for(self, month: int) -> Bank:
        for span in self.banks:
            if span.first_month <= month <= span.last_month:
                return span.bank
        raise ValueError(f"{self.account_id}: no bank for month {month}")


@dataclass(frozen=True)
class Statement:
    account_id: str
    year: int
    month: int
    bank: Bank
    opening_balance: int
    closing_balance: int
    txns: tuple[Txn, ...]

    @property
    def statement_id(self) -> str:
        return f"{self.account_id}_{self.year}-{self.month:02d}"
