"""Single source of truth for reconciliation flags and both review audiences."""

from dataclasses import dataclass
from types import MappingProxyType


@dataclass(frozen=True)
class FlagDefinition:
    severity: str
    condition: str
    cpa_action: str
    title: str
    client_phrase: str
    preparer_only: bool = False


CATALOG = MappingProxyType(
    {
        "balance_readings_conflict": FlagDefinition(
            "review",
            "Readable opening or closing balances for the same account, boundary and date disagree.",
            "Compare every source and resolve the opening or closing balance for that date.",
            "Opening or closing balance readings disagree",
            "readings of the same opening or closing balance that disagree",
        ),
        "institution_names_differ": FlagDefinition(
            "review",
            "Statements for one account print different institution names.",
            "Confirm whether the bank changed or one bank is printed in different ways.",
            "Statements print different bank names",
            "the bank names printed differently on statements",
        ),
        "duplicate_upload": FlagDefinition(
            "info",
            "Two uploads have the same currency, period, balances and transactions.",
            "Review the retained original if needed.",
            "Same statement uploaded twice",
            "duplicate copies of statements",
        ),
        "missing_statement": FlagDefinition(
            "review",
            "No statement period covers a whole calendar month.",
            "Request the missing statements or record why none exist.",
            "Months no statement covers",
            "months of the year that no statement covers",
        ),
        "coverage_gap": FlagDefinition(
            "review",
            "Some days outside whole missing months have no statement coverage.",
            "Request documents for the uncovered dates or record why none exist.",
            "Days in the year no statement covers",
            "days of the year that no statement covers",
        ),
        "arithmetic_mismatch": FlagDefinition(
            "review",
            "Printed balances differ from the balances computed from transaction lines.",
            "Compare the amounts with the source lines and resolve the discrepancy.",
            "Printed balance does not match the transactions",
            "statement balances that did not match the transactions as read",
        ),
        "field_unreadable": FlagDefinition(
            "review",
            "A key statement field could not be read or parsed.",
            "Check the source or request a clearer copy.",
            "A key field could not be read",
            "parts of a statement that could not be read, such as a date, currency or balance",
        ),
        "out_of_year_statement": FlagDefinition(
            "info",
            "A statement period is entirely outside the tax year.",
            "Review the ignored statement dates if needed.",
            "Statement outside the tax year, excluded",
            "statements outside the year under review",
        ),
        "continuity_break": FlagDefinition(
            "review",
            "Adjacent statement closing and opening balances differ.",
            "Check both source balances and the intervening transactions.",
            "Closing balance differs from the next opening balance",
            "closing balances that did not match the next statement's opening balance",
        ),
        "no_balance_anchor": FlagDefinition(
            "review",
            "Transactions are present but no in-year balance can be established.",
            "Request statements showing balances for this account.",
            "Maximum unavailable from transactions without a balance",
            "statements that list transactions but show no balance",
        ),
        "no_usable_balance": FlagDefinition(
            "review",
            "An identified account has neither an in-year balance nor transaction lines.",
            "Request statements with readable balances for the year.",
            "No usable balance for the tax year",
            "accounts with no readable balance for the year",
        ),
        "max_not_at_month_end": FlagDefinition(
            "info",
            "The largest observed balance comes from a transaction line.",
            "Review the source of the maximum if needed.",
            "Highest balance falls within a statement period",
            "highest balances shown within statement periods",
        ),
        "max_from_computed_balance": FlagDefinition(
            "info",
            "The maximum is computed from transactions rather than printed.",
            "Review the balance anchor and transaction lines if needed.",
            "Highest balance computed from transactions",
            "highest balances computed from transactions",
        ),
        "possible_intraday_peak": FlagDefinition(
            "review",
            "Deposits posting first could increase the maximum by the configured threshold.",
            "Check the posting order of same-day deposits and withdrawals.",
            "Possible same-day peak above the observed maximum",
            "the order of same-day deposits and withdrawals, which can change the highest balance",
        ),
        "currency_conflict": FlagDefinition(
            "review",
            "Statements for one account print more than one currency.",
            "Select the account currency and record a source note before calculating balances.",
            "Statements show different currencies",
            "statements showing different currencies for this account",
        ),
        "currency_unknown": FlagDefinition(
            "review",
            "No Treasury rate is available for the account currency.",
            "Record a verifiable alternative rate, its source and date, "
            "or leave the dollar amount undetermined.",
            "No Treasury exchange rate available",
            "accounts with no Treasury exchange rate, whose dollar amounts "
            "depend on a separately recorded rate",
        ),
        "account_number_masked": FlagDefinition(
            "review",
            "A partly masked account number prevents identification.",
            "Match the rows to a known account or request the full account number.",
            "Account number partly masked, excluded from totals",
            "rows with partly masked account numbers left out of the totals",
        ),
        "account_unidentified": FlagDefinition(
            "review",
            "The account number cannot be read.",
            "Identify the account from the source or request a clearer copy.",
            "Account number unreadable, excluded from totals",
            "rows with unreadable account numbers left out of the totals",
        ),
        "signature_authority_only": FlagDefinition(
            "review",
            "Intake states signature authority and explicitly no financial interest.",
            "Confirm your client can sign on the account without financial interest.",
            "Signature authority with no financial interest",
            "your authority to sign on an account owned by someone else",
        ),
        "financial_interest_unknown": FlagDefinition(
            "review",
            "Signature authority is stated but financial interest is unknown.",
            "Confirm and record whether your client has financial interest.",
            "Financial interest not recorded",
            "whether you have a financial interest in accounts you can sign on",
        ),
        "ownership_interest_conflict": FlagDefinition(
            "review",
            "Intake or correction states sole or joint ownership and no financial interest.",
            "Review the stated classification and correct the relevant fact with its source.",
            "Ownership and financial interest need clarification",
            "",
            preparer_only=True,
        ),
        "authority_interest_review": FlagDefinition(
            "review",
            "Signature authority and financial interest are both stated.",
            "Review the stated classification and its source; both facts can coexist.",
            "Signature authority and financial interest",
            "",
            preparer_only=True,
        ),
        "ownership_unknown": FlagDefinition(
            "review",
            "No account ownership classification is recorded.",
            "Confirm and record who owns the account.",
            "Ownership not recorded",
            "who owns the account, which has not been recorded",
        ),
        "no_statements": FlagDefinition(
            "review",
            "An intake account has no statement among the documents read.",
            "Request the account statements or record why none exist.",
            "Listed at intake but no statement was read",
            "accounts with no statement among the documents read",
        ),
        "page_not_read": FlagDefinition(
            "review",
            "A page could not be extracted.",
            "Re-read the page or request a clearer copy, as its contents are excluded.",
            "Page could not be read, excluded from totals",
            "pages that could not be read and were left out of the totals",
        ),
    }
)
