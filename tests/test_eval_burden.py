"""Count reviewer work per flag occurrence, including unattributed and misread-induced work."""

from types import SimpleNamespace

from cross_border_fbar.eval.reconcile_eval import _review_burden
from cross_border_fbar.reconcile.accounts import Flag
from cross_border_fbar.reconcile.observe import Source


def account(key, *flags):
    return SimpleNamespace(account_key=key, flags=list(flags))


def test_burden_counts_occurrences_once_and_includes_unknown_accounts():
    truth = [
        {
            "account_number": "123",
            "expected_flags": [
                {"code": "missing_statement", "severity": "review", "statement_id": "bank_2025-08"},
                {"code": "max_not_at_month_end", "severity": "info"},
            ],
        }
    ]
    items = [
        account(
            "123",
            Flag("missing_statement", "expected", month="2025-08"),
            Flag("missing_statement", "duplicate", month="2025-08"),
            Flag("institution_names_differ", "misread", [Source("p1", "institution", None)]),
            Flag("institution_names_differ", "another misread", [Source("p2", "institution", None)]),
            Flag("max_not_at_month_end", "info"),
        ),
        account("unread:p3", Flag("page_not_read", "unread page")),
    ]
    burden = _review_burden(
        items, truth, {("s1", "institution"), ("s2", "institution")}, {"p1": "s1", "p2": "s2"}
    )
    assert burden == {
        "review_items_total": 5,
        "expected_review_items_matched": 1,
        "additional_review_items": 4,
        "additional_items_from_printed_name_choice": 0,
        "additional_items_linked_to_header_misreads": 2,
        "additional_items_other": 2,
    }


def test_a_missing_expected_review_is_not_counted_as_actual_work():
    truth = [
        {
            "account_number": "123",
            "expected_flags": [{"code": "signature_authority_only", "severity": "review"}],
        }
    ]
    assert _review_burden([], truth, set(), {})["review_items_total"] == 0


def test_unreadable_header_item_links_to_its_scored_misread():
    results = [
        account("123", Flag("field_unreadable", "currency unreadable", [Source("page", "currency", None)]))
    ]
    summary = _review_burden(results, [], {("statement", "currency")}, {"page": "statement"})
    assert summary["additional_review_items"] == 1
    assert summary["additional_items_linked_to_header_misreads"] == 1
    assert summary["additional_items_other"] == 0


def test_duplicate_expected_items_match_individually():
    expected = {"code": "signature_authority_only", "severity": "review"}
    truth = [{"account_number": "123", "expected_flags": [expected, expected]}]
    result = account(
        "123", Flag("signature_authority_only", "first"), Flag("signature_authority_only", "second")
    )
    burden = _review_burden([result], truth, set(), {})
    assert burden["expected_review_items_matched"] == 2
    assert burden["additional_review_items"] == 0


def test_a_choice_between_printed_names_has_its_own_count_and_stays_in_the_workload():
    chosen = Flag("institution_names_differ", "two printed names", [Source("p1", "institution", None)])
    result = account("123", chosen, Flag("page_not_read", "unread page"))
    burden = _review_burden([result], [], {("s1", "institution")}, {"p1": "s1"}, [chosen])
    assert burden == {
        "review_items_total": 2,
        "expected_review_items_matched": 0,
        "additional_review_items": 2,
        "additional_items_from_printed_name_choice": 1,
        "additional_items_linked_to_header_misreads": 0,
        "additional_items_other": 1,
    }
