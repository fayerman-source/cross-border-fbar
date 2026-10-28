"""A compiled transaction list: the kind of export a client or family member sends instead of statements.

Modeled on the layout of a real compiled Korean list (layout only; nothing copied):
several accounts on one page, one amount column with a separate in/out column,
ISO dates, a channel column, counterparty and remarks columns, and no balances
at all. It spans more than the tax year, and one account appears only as a
partially masked number.

What a correct reading must conclude:
- kr-mmda: transactions only, no balance anywhere, so the FBAR maximum cannot
  be determined from this document. The right output is a review flag asking
  for statements with balances, never an estimate.
- the masked rows: an account that can't be identified from the document, so
  it is not counted and is flagged for review.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import date, timedelta

from .household import FATHER_HANGUL, JOY_HANGUL
from .model import Txn

MMDA_NUMBER = "110-731-552904"
MASKED_CHECKING = "110-48*-******"
PERIOD = (date(2024, 9, 1), date(2026, 1, 31))
MMDA_OPENING = 85_000_000  # balance on 2024-09-01; printed nowhere


@dataclass(frozen=True)
class CompiledRow:
    row_id: str
    day: date
    account_label: str  # 계좌 column: "MMDA" or "주거래"
    account_printed: str
    account_id: str  # which household account the row belongs to (answer key only)
    channel: str  # 종류
    direction: str  # 구분: 입금 | 출금
    amount: int  # won, positive
    counterparty: str
    remark: str
    balance_after: int | None  # true balance (answer key only); never printed

    @property
    def signed(self) -> int:
        return self.amount if self.direction == "입금" else -self.amount


def _mmda_entries(rng: random.Random) -> list[tuple[date, str, str, int, str, str]]:
    """(day, channel, direction, amount, counterparty, remark) for the MMDA account, oldest first."""
    out = []
    d = PERIOD[0]
    while d <= PERIOD[1]:
        for _ in range(rng.randrange(1, 4)):
            day = d + timedelta(days=rng.randrange(1, 27))
            kind = rng.random()
            if kind < 0.45:
                out.append((day, "현금지급", "출금", rng.randrange(2_000, 9_000) * 1_000, "", ""))
            elif kind < 0.8:
                out.append(
                    (
                        day,
                        "대체지급",
                        "출금",
                        rng.randrange(3_000, 7_000) * 1_000 + rng.randrange(0, 99) * 10,
                        "",
                        "",
                    )
                )
            else:
                out.append(
                    (
                        day,
                        "인터넷",
                        "입금",
                        rng.randrange(5_000, 12_000) * 1_000,
                        JOY_HANGUL,
                        "본인 계좌 간 이체",
                    )
                )
        d = date(d.year + d.month // 12, d.month % 12 + 1, 1)
    # A time deposit matures into the account and most of it moves on within weeks (the 2025 peak).
    out += [
        (date(2025, 6, 9), "대체입금", "입금", 150_000_000, "정기예금 만기", "본인 계좌 간 이체"),
        (date(2025, 6, 23), "텔레뱅킹", "출금", 120_000_000, FATHER_HANGUL, "제3자 송금 - 상대방 확인 필요"),
    ]
    return sorted(out, key=lambda e: e[0])


def compiled_rows(seed: int, checking: list[Txn]) -> list[CompiledRow]:
    rng = random.Random(f"{seed}:compiled")
    rows, balance = [], MMDA_OPENING
    for i, (day, channel, direction, amount, cp, remark) in enumerate(_mmda_entries(rng), start=1):
        signed = amount if direction == "입금" else -amount
        if balance + signed < 0:
            continue
        balance += signed
        rows.append(
            CompiledRow(
                f"compiled-mmda-{i:03d}",
                day,
                "MMDA",
                MMDA_NUMBER,
                "kr-mmda",
                channel,
                direction,
                amount,
                cp,
                remark,
                balance,
            )
        )
    # A handful of checking-account rows appear with a masked number; the reader can't tie them to an account.
    salary = [t for t in checking if t.description.startswith("급여")][::3]
    for j, t in enumerate(salary, start=1):
        rows.append(
            CompiledRow(
                f"compiled-masked-{j:03d}",
                t.day,
                "주거래",
                MASKED_CHECKING,
                "masked-checking",
                "이체",
                "입금",
                t.amount,
                "(주)새온랩스",
                "급여",
                None,
            )
        )
    return sorted(rows, key=lambda r: (r.day, r.account_label))


def mmda_true_peak(rows: list[CompiledRow], year: int) -> int:
    """The real 2025 maximum, knowable only from the hidden ledger; reported as unknowable."""
    balance, peak = MMDA_OPENING, None
    for r in (r for r in rows if r.account_id == "kr-mmda"):
        if r.day.year == year and peak is None:
            peak = balance  # the balance carried into the year
        balance = r.balance_after
        if r.day.year == year:
            peak = max(peak, balance)
    return peak
