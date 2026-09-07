"""Tests for MembersAPI, 02-spec/SPEC.md criteria 20 to 22 and 25."""

import json

import pytest
from canvas_sdk.effects.effect import EffectType
from canvas_sdk.handlers.simple_api import SessionCredentials
from canvas_sdk.handlers.simple_api.exceptions import InvalidCredentialsError

from apex_recurring_membership_payments.handlers import members_api
from apex_recurring_membership_payments.models.membership import Membership, MembershipStatus
from apex_recurring_membership_payments.models.membership_charge import ChargeOutcome

from tests.support import DummyEvent, DummyRequest, make_charge, make_membership, make_patient


def _api(method: str, path: str, secrets: dict | None = None) -> members_api.MembersAPI:
    return members_api.MembersAPI(
        event=DummyEvent(context={"method": method, "path": path}),
        secrets=secrets or {},
    )


def _seed_ac20_memberships() -> dict:
    """The three memberships AC20 names, reused by AC21 exactly as its Given says."""
    daniel = make_patient(first_name="Daniel", last_name="Reyes")
    active = make_membership(patient=daniel, status=MembershipStatus.ACTIVE)

    marcus = make_patient(first_name="Marcus", last_name="Hale")
    payment_failed = make_membership(patient=marcus, status=MembershipStatus.PAYMENT_FAILED)
    make_charge(payment_failed, outcome=ChargeOutcome.FAILED, status="INSTRUMENT_FAILURE")

    third_patient = make_patient(first_name="Third", last_name="Zorn")
    ended = make_membership(patient=third_patient, status=MembershipStatus.ENDED)

    return {"active": active, "payment_failed": payment_failed, "ended": ended}


@pytest.mark.django_db
def test_members_page_lists_every_member_with_status_charges_and_open_chart_button():
    """Covers criterion: AC20
    Covers scenario: AC20, the members page lists every member with status, charges and an Open chart button
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
    assert b"Open chart" in body
    assert b'id="cancel-btn"' not in body
    assert b"Cancel membership" not in body


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
def test_opening_a_chart_from_a_members_row_returns_the_redirect():
    """Covers criterion: AC22
    Covers scenario: AC22, opening a chart from a members row returns the redirect
    """
    patient = make_patient()
    make_membership(patient=patient, patient_key="K1")

    found_api = _api("POST", "/members/open-chart")
    found_api.request = DummyRequest(body={"patient_key": "K1"})
    found_result = found_api.open_chart()

    assert found_result[0].status_code == 200
    redirect = found_result[1]
    assert redirect.type == EffectType.REDIRECT
    payload = json.loads(redirect.payload)
    assert payload["data"]["url"] == "/patient/K1"
    assert payload["data"]["target"] == "same_tab"

    missing_api = _api("POST", "/members/open-chart")
    missing_api.request = DummyRequest(body={"patient_key": "K9"})
    missing_result = missing_api.open_chart()

    assert missing_result[0].status_code == 400
    assert len(missing_result) == 1


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
