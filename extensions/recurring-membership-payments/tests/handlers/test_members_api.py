"""Tests for MembersAPI, 02-spec/SPEC.md criteria 20, 21, 25, 27, 28 and 29.

Criterion 22 carries no test here on purpose. AC22 is withdrawn in the
specification, version 5. It tested POST /members/open-chart and the
RedirectEffect it returned, and the 2026-09-07 engineer direction on the
members page redesign replaces that button and that route with a plain
anchor opening the chart in a new tab, so the route and its redirect effect
are both deleted and the test that covered them goes with them. See AC27
below for what the rows route returns now.
"""

from unittest.mock import patch

import pytest
from canvas_sdk.handlers.simple_api import SessionCredentials
from canvas_sdk.handlers.simple_api.exceptions import InvalidCredentialsError

from apex_recurring_membership_payments.handlers import members_api
from apex_recurring_membership_payments.logic import membership_logic
from apex_recurring_membership_payments.models.membership import MembershipStatus
from apex_recurring_membership_payments.models.membership_charge import ChargeOutcome

from tests.support import (
    DummyEvent,
    DummyRequest,
    make_charge,
    make_membership,
    make_patient,
    make_successful_charges,
)


def _api(method: str, path: str, secrets: dict | None = None) -> members_api.MembersAPI:
    return members_api.MembersAPI(
        event=DummyEvent(context={"method": method, "path": path}),
        secrets=secrets or {},
    )


def _seed_ac20_memberships() -> dict:
    """The three memberships AC20 names, reused by AC21 exactly as its Given says."""
    daniel = make_patient(first_name="Daniel", last_name="Reyes")
    # The one seeded card, so the payment method cell AC20's own Then clause
    # names, the brand with the last four, has something to render.
    active = make_membership(
        patient=daniel,
        status=MembershipStatus.ACTIVE,
        card_brand="Visa",
        card_last_four="4242",
    )

    marcus = make_patient(first_name="Marcus", last_name="Hale")
    payment_failed = make_membership(patient=marcus, status=MembershipStatus.PAYMENT_FAILED)
    make_charge(payment_failed, outcome=ChargeOutcome.FAILED, status="INSTRUMENT_FAILURE")

    third_patient = make_patient(first_name="Third", last_name="Zorn")
    ended = make_membership(patient=third_patient, status=MembershipStatus.ENDED)

    return {"active": active, "payment_failed": payment_failed, "ended": ended}


@pytest.mark.django_db
def test_members_page_lists_every_member_with_the_redesigned_columns_and_row_controls():
    """Covers criterion: AC20
    Covers scenario: AC20, the members page lists every member with the redesigned columns and the row's three controls
    """
    _seed_ac20_memberships()
    api = _api("GET", "/members/")
    api.request = DummyRequest()

    result = api.get_members()

    body = result[0].content
    assert body.index(b"Hale") < body.index(b"Reyes") < body.index(b"Zorn")
    assert b"Active" in body
    assert b"Payment failed" in body
    assert b"Ended" in body
    assert body.count(b", Declined") == 1
    # The payment method cell, the brand in normal weight and the four
    # digits bold, the one fact in that cell a reader must not misread.
    assert b"Visa" in body
    assert b"ending in <b" in body
    assert b">4242</b>" in body
    # Every column collapses to its content except Payment method, the
    # sixth of seven, nominated through grow on the table itself to absorb
    # a wide surface's slack, and the table scrolls sideways rather than
    # the page when a surface cannot hold seven columns that no longer wrap.
    assert b'grow="6"' in body
    assert b"<canvas-scroll-area horizontal" in body
    assert body.count(b"Open in chart") == 3
    assert body.count(b'data-action="view-history"') == 3
    # Every row carries all three controls, including the ended membership
    # that can never be cancelled, because a row missing one pulls the whole
    # cluster right and the column stops scanning as a column. The control is
    # disabled rather than absent, and it says why.
    assert body.count(b'data-action="cancel-membership"') == 3
    assert body.count(b"disabled") == 3
    assert body.count(b"This membership has already ended.") == 1
    assert (
        body.count(b"Cancellation opens once the third charge has been taken, 0 of 3 so far.")
        == 2
    )


@pytest.mark.django_db
@pytest.mark.parametrize(
    "query,expected_name,expect_no_match",
    [
        ({"q": "hale"}, b"Hale", False),
        ({"status": "ACTIVE"}, b"Reyes", False),
        ({"status": "DECLINED"}, b"Hale", False),
        ({"q": "zzz"}, None, True),
    ],
)
def test_members_page_searches_by_name_and_filters_by_status_and_declined_charge(
    query, expected_name, expect_no_match
):
    """Covers criterion: AC21
    Covers scenario: AC21, the members page searches by name and filters by status and by declined charge
    """
    _seed_ac20_memberships()
    api = _api("GET", "/members/")
    api.request = DummyRequest(query_params=query)

    result = api.get_members()

    body = result[0].content
    if expect_no_match:
        assert b"No members match" in body
    else:
        assert expected_name in body


@pytest.mark.django_db
def test_members_rows_route_returns_the_fragment_alone():
    """Covers criterion: AC27
    Covers scenario: AC27, the members rows route returns the fragment alone and requires a staff session
    """
    _seed_ac20_memberships()
    api = _api("GET", "/members/rows")
    api.request = DummyRequest(query_params={"q": "hale"})

    result = api.get_rows()

    body = result[0].content
    assert result[0].status_code == 200
    assert b"Hale" in body
    assert b"<!DOCTYPE" not in body
    assert b"canvas-plugin-ui.css" not in body
    assert b'id="search-input"' not in body

    with pytest.raises(InvalidCredentialsError):
        credentials = SessionCredentials(DummyRequest(headers={}))
        api.authenticate(credentials)


@pytest.mark.django_db
@pytest.mark.parametrize("query", [{"q": "hale"}, {"status": "ACTIVE"}, {"page": "1"}])
def test_members_rows_route_reads_the_same_query_parameters_the_full_page_does(query):
    """Covers criterion: AC27
    Covers scenario: AC27, the members rows route returns the fragment alone and requires a staff session

    The rows route and the full page route both build their context through
    the same _rows_context function, so a query that filters the page down
    to one member filters the fragment down to the same member. This is
    what proves the two routes cannot read one query string two ways.
    """
    _seed_ac20_memberships()

    rows_api = _api("GET", "/members/rows")
    rows_api.request = DummyRequest(query_params=query)
    rows_body = rows_api.get_rows()[0].content

    page_api = _api("GET", "/members/")
    page_api.request = DummyRequest(query_params=query)
    page_body = page_api.get_members()[0].content

    for name in (b"Hale", b"Reyes", b"Zorn"):
        assert (name in rows_body) == (name in page_body)


@pytest.mark.django_db
def test_members_history_route_serves_the_shared_charge_history_fragment():
    """Covers criterion: AC28
    Covers scenario: AC28, the members history route serves the shared charge history fragment keyed on patient_key
    """
    patient = make_patient()
    membership = make_membership(patient=patient, patient_key="K1")
    make_charge(membership, transaction_id="T1")
    make_charge(membership, transaction_id="T2")

    api = _api("GET", "/members/history/K1")
    api.request = DummyRequest(path_params={"patient_key": "K1"})
    result = api.get_history()

    body = result[0].content
    assert result[0].status_code == 200
    assert b"<!DOCTYPE" not in body
    assert b'id="charge-history-body"' in body
    assert b"Paid" in body

    missing_api = _api("GET", "/members/history/K9")
    missing_api.request = DummyRequest(path_params={"patient_key": "K9"})
    missing_result = missing_api.get_history()

    assert missing_result[0].status_code == 404


@pytest.mark.django_db
def test_members_history_route_returns_charges_for_an_ended_membership_with_no_recurring_id():
    """Covers criterion: AC28
    Covers scenario: AC28, the members history route serves the shared charge history fragment keyed on patient_key

    A membership whose status is ENDED carries an empty recurring_id,
    since the daily health check clears it once a membership ends. Reading
    this route on patient_key rather than on recurring_id is what keeps a
    former member's history reachable, and keying on recurring_id instead
    would silently return an empty history for exactly this membership,
    which is the defect the shared patient_key read was introduced to fix.
    """
    patient = make_patient()
    membership = make_membership(
        patient=patient, patient_key="K2", status=MembershipStatus.ENDED, recurring_id=""
    )
    make_charge(membership, transaction_id="T1")

    api = _api("GET", "/members/history/K2")
    api.request = DummyRequest(path_params={"patient_key": "K2"})
    result = api.get_history()

    body = result[0].content
    assert result[0].status_code == 200
    assert b"Paid" in body


@pytest.mark.django_db
def test_cancelling_from_a_members_row_requires_a_patient_key():
    """Covers criterion: AC29
    Covers scenario: AC29, cancelling from a members row shares the three successful charge rule and effects with the chart
    """
    api = _api("POST", "/members/cancel")
    api.request = DummyRequest(body={})

    result = api.cancel()

    assert result[0].status_code == 400


@pytest.mark.django_db
def test_cancelling_from_a_members_row_refuses_a_membership_that_is_not_eligible():
    """Covers criterion: AC29
    Covers scenario: AC29, cancelling from a members row shares the three successful charge rule and effects with the chart
    """
    patient = make_patient()
    make_membership(patient=patient, patient_key="K1", status=MembershipStatus.ENDED)

    with patch.object(membership_logic, "cancel_recurring_payment") as mock_cancel:
        api = _api("POST", "/members/cancel")
        api.request = DummyRequest(body={"patient_key": "K1"})
        result = api.cancel()
        mock_cancel.assert_not_called()

    assert result[0].status_code == 403


@pytest.mark.django_db
@pytest.mark.parametrize(
    "successes,expected_status,ends_up_cancelling",
    [
        (3, 200, True),
        (2, 403, False),
    ],
)
def test_cancelling_from_a_members_row_shares_the_three_successful_charge_rule_and_effects_with_the_chart(
    successes, expected_status, ends_up_cancelling
):
    """Covers criterion: AC29
    Covers scenario: AC29, cancelling from a members row shares the three successful charge rule and effects with the chart
    """
    patient = make_patient()
    membership = make_membership(patient=patient, patient_key="K1", status=MembershipStatus.ACTIVE)
    make_successful_charges(membership, successes)

    with patch.object(
        membership_logic, "cancel_recurring_payment", return_value=True
    ) as mock_cancel:
        api = _api("POST", "/members/cancel")
        api.request = DummyRequest(body={"patient_key": "K1"})
        result = api.cancel()

    membership.refresh_from_db()
    assert result[0].status_code == expected_status
    if ends_up_cancelling:
        assert membership.status == MembershipStatus.CANCELLING
        assert membership.cancelled_by == "staff"
        mock_cancel.assert_called_once()
    else:
        assert membership.status == MembershipStatus.ACTIVE
        mock_cancel.assert_not_called()


@pytest.mark.django_db
@pytest.mark.parametrize(
    "headers",
    [
        {"canvas-logged-in-user-type": "Patient", "canvas-logged-in-user-id": "p1"},
        {},
    ],
)
def test_members_page_refuses_anything_but_a_staff_session(headers):
    """Covers criterion: AC25
    Covers scenario: AC25, the members page refuses anything but a staff session
    """
    api = _api("GET", "/members/")
    request = DummyRequest(headers=headers)

    with pytest.raises(InvalidCredentialsError):
        credentials = SessionCredentials(request)
        api.authenticate(credentials)
