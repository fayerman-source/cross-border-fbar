"""Stated intake facts shared by reconciliation and the preparer worksheet.

Known fields are strictly typed. Additional metadata is preserved, but never used in calculations.
Missing facts remain unknown; none are inferred from statements or a demo persona.
"""

from dataclasses import dataclass, field

from ..extract.parse import account_key

FILING_STATUSES = {"single", "mfj", "mfs", "hoh", "qss"}
OWNERSHIPS = {"sole", "joint", "signature_authority", "unknown"}


def _mapping(value, label):
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def _boolean(value, label):
    if value is not None and type(value) is not bool:
        raise ValueError(f"{label} must be true, false or null")
    return value


def _text(value, label, *, optional=False):
    if optional and value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{label} must be text")
    return value


@dataclass(frozen=True)
class Intake:
    ownership: str
    financial_interest: bool | None = None
    account_number: str = ""
    joint_with_spouse: bool | None = None
    metadata: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, value):
        value = _mapping(value, "account intake")
        ownership = value.get("ownership", "unknown")
        if not isinstance(ownership, str) or ownership not in OWNERSHIPS:
            raise ValueError("unrecognized ownership")
        known = {"ownership", "financial_interest", "account_number", "joint_with_spouse"}
        return cls(
            ownership,
            _boolean(value.get("financial_interest"), "financial_interest"),
            _text(value.get("account_number", ""), "account_number"),
            _boolean(value.get("joint_with_spouse"), "joint_with_spouse"),
            {k: v for k, v in value.items() if k not in known},
        )


@dataclass
class Household:
    filing_status: str | None = None
    lives_abroad: bool | None = None
    spouse_specified_individual: bool | None = None
    metadata: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, value):
        value = _mapping(value, "household intake")
        status = value.get("filing_status")
        if status is not None and (not isinstance(status, str) or status not in FILING_STATUSES):
            raise ValueError("unrecognized filing_status")
        known = {"filing_status", "lives_abroad", "spouse_specified_individual"}
        return cls(
            status,
            _boolean(value.get("lives_abroad"), "lives_abroad"),
            _boolean(value.get("spouse_specified_individual"), "spouse_specified_individual"),
            {k: v for k, v in value.items() if k not in known},
        )


@dataclass
class IntakeDocument:
    client: str | None
    source: str | None
    household: Household
    accounts: dict[str, Intake]
    metadata: dict

    @classmethod
    def from_dict(cls, value):
        value = _mapping(value, "intake")
        rows = value.get("accounts")
        if not isinstance(rows, list):
            raise ValueError("intake accounts must be a list")
        accounts = {}
        for row in rows:
            record = Intake.from_dict(row)
            key = account_key(record.account_number)
            if not key or key in accounts:
                raise ValueError("intake account numbers must be nonempty and unique")
            accounts[key] = record
        return cls(
            _text(value.get("client"), "client", optional=True),
            _text(value.get("source"), "source", optional=True),
            Household.from_dict(value.get("household", {})),
            accounts,
            {k: v for k, v in value.items() if k not in {"client", "source", "household", "accounts"}},
        )
