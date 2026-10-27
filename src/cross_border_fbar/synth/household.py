"""The fictional household behind the demo and the accuracy test.

Joy Seulgi Jung (정슬기) is a fictional Korean-American US citizen working in Seoul.
Every bank, person and account number here is invented. Each account carries at
least one case the tool must handle:

- kr-checking: a jeonse deposit passes through for two days in March (a peak no
  month-end balance shows).
- kr-parents: her parents' account, where she only has signature authority; the
  August statement is withheld and August holds the true peak.
- de-joint: joint with her husband; the bank merges mid-year (same IBAN, new name
  and layout); an inheritance passes through in November. The first bank's
  layout prints no running balance.
- th-savings: left over from a job in Bangkok; Buddhist-era dates; a same-day pair
  posted debit-first, so no printed balance shows the possible intraday peak.
"""

from __future__ import annotations

import random
from datetime import date, timedelta

from .model import AccountSpec, Bank, BankSpan, Entry

JOY_PASSPORT = "JUNG SEULGI JOY"
JOY_HANGUL = "정슬기"
FATHER_HANGUL = "정민호"
HUSBAND = "Jonas Albrecht"


def _weekday_on_or_before(d: date) -> date:
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def _day(year: int, month: int, day: int) -> date:
    return date(year, month, day)


def _pick_days(rng: random.Random, year: int, month: int, k: int) -> list[int]:
    last = (date(year + month // 12, month % 12 + 1, 1) - timedelta(days=1)).day
    return sorted(rng.sample(range(1, last + 1), k))


def _iban_de(blz: str, account: str) -> str:
    # ISO 13616 mod-97 check digits; D=13, E=14.
    check = 98 - int(blz + account + "131400") % 97
    raw = f"DE{check:02d}{blz}{account}"
    return " ".join(raw[i : i + 4] for i in range(0, len(raw), 4))


# --- banks (all fictional) -------------------------------------------------

SAEBYEOL = Bank("saebyeol", "새별은행", "Saebyeol Bank", "서울특별시 중구 새별로 21", "kr")
DAON = Bank("daon", "다온은행", "Daon Bank", "대전광역시 서구 다온대로 88", "kr")
RHEINAUEN = Bank(
    "rheinauen",
    "Rheinauen Privatbank",
    "Rheinauen Privatbank",
    "Uferstraße 4, 55116 Mainz",
    "de_plain",
    bic="RHAUDE5MXXX",
)
BERGWALD = Bank(
    "bergwald",
    "Bergwald Bank AG",
    "Bergwald Bank AG",
    "Waldring 17, 60311 Frankfurt am Main",
    "de_saldo",
    bic="BGWADEFFXXX",
)
SIAM_LOTUS = Bank("siamlotus", "ธนาคารสยามโลตัส", "Siam Lotus Bank", "99 ถนนสาทรใต้ กรุงเทพมหานคร 10120", "th")


# --- monthly patterns ------------------------------------------------------


def _kr_checking(rng: random.Random, year: int, month: int) -> list[Entry]:
    e = [
        Entry(_day(year, month, 10), f"{FATHER_HANGUL} 생활비", -500_000),
        Entry(_day(year, month, 14), "새별카드 결제대금", -rng.randrange(1_600, 3_400) * 1_000),
        Entry(_day(year, month, 20), "아파트 관리비", -rng.randrange(230, 340) * 1_000),
        Entry(_day(year, month, 22), "통신요금 자동이체", -rng.randrange(620, 780) * 100),
        Entry(_weekday_on_or_before(_day(year, month, 25)), "급여 (주)새온랩스", 6_840_000),
    ]
    merchants = ["체크카드 GS25", "체크카드 교보문고", "체크카드 올리브영", "카카오페이 송금", "ATM 출금"]
    for d in _pick_days(rng, year, month, rng.randrange(3, 8)):
        e.append(Entry(_day(year, month, d), rng.choice(merchants), -rng.randrange(80, 2_500) * 100))
    if month % 3 == 0:
        e.append(Entry(_day(year, month, 21), "예금이자", rng.randrange(900, 1_600)))
    return e


def _kr_parents(rng: random.Random, year: int, month: int) -> list[Entry]:
    e = [
        Entry(_day(year, month, 10), JOY_HANGUL, 500_000),
        Entry(_day(year, month, 15), "다온카드 결제대금", -rng.randrange(600, 1_400) * 1_000),
        Entry(_day(year, month, 20), "관리비", -rng.randrange(180, 260) * 1_000),
        Entry(_weekday_on_or_before(_day(year, month, 25)), "국민연금", 1_386_000),
    ]
    if rng.random() < 0.4:
        e.append(Entry(_day(year, month, rng.randrange(2, 28)), "병원비", -rng.randrange(30, 400) * 1_000))
    return e


def _de_joint(rng: random.Random, year: int, month: int) -> list[Entry]:
    e = [
        Entry(_day(year, month, 1), "Hausgeld WEG Lindenstr. 12", -31_000),
        Entry(_day(year, month, 3), "Mieteingang Wohnung Leipzig", 95_000),
        Entry(_day(year, month, 5), "Haftpflichtversicherung", -4_590),
    ]
    if month in (1, 4, 7, 10):
        e.append(Entry(_day(year, month, 15), "Rundfunkbeitrag", -5_508))
    if month in (2, 5, 8, 11):
        e.append(Entry(_day(year, month, 15), "Grundsteuer Stadt Leipzig", -21_460))
    for d in _pick_days(rng, year, month, rng.randrange(0, 3)):
        e.append(
            Entry(
                _day(year, month, d),
                "Kartenzahlung " + rng.choice(["REWE", "DB Fernverkehr", "Thalia"]),
                -rng.randrange(1_500, 12_000),
            )
        )
    return e


def _th_savings(rng: random.Random, year: int, month: int) -> list[Entry]:
    e = []
    if month in (4, 10):
        e.append(Entry(_day(year, month, rng.randrange(5, 25)), "ถอนเงิน ATM", -500_000))
    if month == 1:
        e.append(Entry(_day(year, month, 8), "ค่าธรรมเนียมบัตรเดบิต", -20_000))
    if month in (6, 12):
        e.append(Entry(_day(year, month, 20), "ดอกเบี้ยเงินฝาก", rng.randrange(40_000, 55_000), "interest"))
    return e


def household(year: int) -> list[AccountSpec]:
    return [
        AccountSpec(
            "kr-checking",
            "KRW",
            "110-482-913746",
            "입출금자유예금",
            JOY_PASSPORT,
            "sole",
            True,
            (JOY_PASSPORT,),
            31_250_000,
            (BankSpan(SAEBYEOL, 1, 12),),
            _kr_checking,
            events=(
                Entry(_day(year, 3, 10), "보증금반환 이○○", 250_000_000, "spike_in"),
                Entry(_day(year, 3, 12), "전세보증금 박○○", -250_000_000, "spike_out"),
            ),
        ),
        AccountSpec(
            "kr-parents",
            "KRW",
            "302-0571-2288-41",
            "보통예금",
            FATHER_HANGUL,
            "signature_authority",
            False,
            (FATHER_HANGUL,),
            43_800_000,
            (BankSpan(DAON, 1, 12),),
            _kr_parents,
            events=(
                Entry(_day(year, 8, 7), "토지보상금", 180_000_000, "spike_in"),
                Entry(_day(year, 8, 21), f"타행이체 {FATHER_HANGUL}", -180_000_000, "spike_out"),
            ),
            missing_months=(8,),
        ),
        AccountSpec(
            "de-joint",
            "EUR",
            _iban_de("99012345", "0481527360"),
            "Gemeinschaftskonto (Girokonto)",
            f"{JOY_PASSPORT} und {HUSBAND}",
            "joint",
            True,
            (JOY_PASSPORT, HUSBAND),
            1_840_000,
            (BankSpan(RHEINAUEN, 1, 6), BankSpan(BERGWALD, 7, 12)),
            _de_joint,
            events=(
                Entry(
                    _day(year, 11, 6), "Gutschrift Nachlass Albrecht, Notariat Dresden", 8_500_000, "spike_in"
                ),
                Entry(_day(year, 11, 10), "Wertpapierkauf Depot 7741", -8_500_000, "spike_out"),
            ),
        ),
        AccountSpec(
            "th-savings",
            "THB",
            "714-2-38065-9",
            "ออมทรัพย์ / SAVINGS",
            f"MS. {JOY_PASSPORT}",
            "sole",
            True,
            (JOY_PASSPORT,),
            41_235_025,
            (BankSpan(SIAM_LOTUS, 1, 12),),
            _th_savings,
            events=(
                # Posted debit first: no printed balance ever shows the possible intraday peak.
                Entry(_day(year, 5, 16), "โอนเงินไปต่างประเทศ", -30_000_000, "sameday_out"),
                Entry(_day(year, 5, 16), "รับโอนเงิน ค่าขายรถยนต์", 30_000_000, "sameday_in"),
            ),
        ),
    ]
