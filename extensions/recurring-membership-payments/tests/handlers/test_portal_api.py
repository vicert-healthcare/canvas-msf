"""Tests for PortalAPI, 02-spec/SPEC.md criteria 1 to 7, 18 and 26.

Every route is called directly on a PortalAPI instance built with support.DummyEvent
and support.DummyRequest, never through the full SimpleAPI dispatch, since the
authentication mixin itself carries no criterion of its own here, patient_key
always arrives through the canvas-logged-in-user-id header the way
PatientSessionAuthMixin already guarantees, and every route trusts that header
alone. Pay Theory itself is always a test double, patched at the point
portal_api imported the function, never the real GraphQL client.
"""

import json
from http import HTTPStatus
from unittest.mock import patch

import pytest

from apex_recurring_membership_payments.handlers import portal_api
from apex_recurring_membership_payments.logic.paytheory import PayTheoryError
from apex_recurring_membership_payments.models.membership import Membership, MembershipStatus
from apex_recurring_membership_payments.models.membership_charge import MembershipCharge
from canvas_sdk.effects.effect import EffectType

from tests.support import (
    DummyEvent,
    DummyRequest,
    days_ago,
    make_charge,
    make_membership,
    make_patient,
    make_successful_charges,
)


def _api(method: str, path: str, secrets: dict | None = None) -> portal_api.PortalAPI:
    return portal_api.PortalAPI(
        event=DummyEvent(context={"method": method, "path": path}),
        secrets=secrets or {},
    )


@pytest.mark.django_db
def test_portal_page_mounts_hosted_fields_under_its_own_csp():
    """Covers criterion: AC1
    Covers scenario: AC1, the portal page mounts Pay Theory's hosted fields under its own content security policy and carries no payment input of its own
    """
    patient = make_patient()
    api = _api(
        "GET",
        "/portal/",
        secrets={
            "PAYTHEORY_SDK_URL": "https://sdk.example.test/pt.js",
            "PAYTHEORY_PUBLIC_KEY": "pk_test_1",
            "PAYTHEORY_BROWSER_ORIGINS": "https://sdk.example.test https://fields.example.test",
            "MEMBERSHIP_PRICE_CENTS": "11900",
        },
    )
    api.request = DummyRequest(headers={"canvas-logged-in-user-id": patient.id})

    result = api.index()

    response = result[0]
    assert response.status_code == HTTPStatus.OK
    policy = response.headers["Content-Security-Policy"]
    for section_line in policy.split(";"):
        if section_line.strip().startswith(("script-src", "frame-src", "connect-src")):
            assert "https://sdk.example.test" in section_line
            assert "https://fields.example.test" in section_line

    body = response.content
    assert b'src="https://sdk.example.test/pt.js"' in body
    for element_id in (
        "pay-theory-credit-card-number",
        "pay-theory-credit-card-exp",
        "pay-theory-credit-card-cvv",
    ):
        assert element_id.encode() in body
    assert b"payTheoryFields" in body
    assert b'apiKey: "pk_test_1"' in body
    assert b"tokenizePaymentMethod" in body
    assert b"119.00" in body
    assert b"consent-checkbox" in body
    assert b"join-btn" in body
    assert b'type="text"' not in body
    assert b'type="number"' not in body
    assert b'type="tel"' not in body


@pytest.mark.django_db
def test_returning_patients_payor_is_handed_back_to_the_tokenize_call():
    """Covers criterion: AC2
    Covers scenario: AC2, a returning patient's payor is handed back to the tokenize call
    """
    patient = make_patient()
    make_membership(patient=patient, payor_id="P1")
    api = _api("GET", "/portal/")
    api.request = DummyRequest(headers={"canvas-logged-in-user-id": patient.id})

    result = api.index()

    body = result[0].content
    assert b'var payorId = "P1";' in body


@pytest.mark.django_db
def test_join_creates_subscription_with_no_first_payment_date_and_records_membership():
    """Covers criterion: AC3
    Covers scenario: AC3, joining with a token creates the subscription with no first payment date and records the membership
    """
    patient = make_patient()
    api = _api(
        "POST",
        "/portal/join",
        secrets={
            "MEMBERSHIP_PRICE_CENTS": "12900",
            "MEMBERSHIP_INTERVAL": "MONTHLY",
            "PAYTHEORY_MERCHANT_ID": "merch-1",
        },
    )
    api.request = DummyRequest(
        headers={"canvas-logged-in-user-id": patient.id},
        body={
            "consent": True,
            "payment_method_id": "PM1",
            "payor_id": "P1",
            "last_four": "4242",
            "brand": "VISA",
        },
    )

    with patch.object(
        portal_api,
        "create_recurring_payment",
        return_value={
            "recurring_id": "R1",
            "status": "SUCCESS",
            "next_payment_date": "2026-10-04",
        },
    ) as mock_create:
        result = api.join()

    mock_create.assert_called_once()
    _, kwargs = mock_create.call_args
    assert kwargs["amount"] == 12900
    assert kwargs["merchant_uid"] == "merch-1"
    assert kwargs["payment_interval"] == "MONTHLY"
    assert kwargs["payment_method_id"] == "PM1"
    assert kwargs["payor_id"] == "P1"
    assert kwargs["recurring_name"] == "Apex membership"
    assert "first_payment_date" not in kwargs

    membership = Membership.objects.get(patient_key=patient.id)
    assert membership.status == MembershipStatus.ACTIVE
    assert membership.recurring_id == "R1"
    assert membership.payor_id == "P1"
    assert membership.payment_method_id == "PM1"
    assert membership.card_last_four == "4242"
    assert membership.amount_cents == 12900
    assert membership.next_payment_date == "2026-10-04"
    assert MembershipCharge.objects.filter(patient_key=patient.id).count() == 0

    assert result[0].status_code == HTTPStatus.OK
    effects = result[1:]
    assert len(effects) == 1
    payload = json.loads(effects[0].payload)
    assert effects[0].type == EffectType.ADD_BANNER_ALERT
    assert payload["key"] == "membership-member"
    assert payload["data"]["intent"] == "info"


@pytest.mark.django_db
def test_join_without_consent_or_token_is_refused_and_calls_nothing():
    """Covers criterion: AC4
    Covers scenario: AC4, joining without consent or without a token is refused and calls nothing
    """
    with patch.object(portal_api, "create_recurring_payment") as mock_create:
        for body in (
            {"consent": False, "payment_method_id": "PM1"},
            {"consent": True, "payment_method_id": ""},
        ):
            patient = make_patient()
            api = _api("POST", "/portal/join")
            api.request = DummyRequest(
                headers={"canvas-logged-in-user-id": patient.id}, body=body
            )

            result = api.join()

            assert result[0].status_code == HTTPStatus.BAD_REQUEST
            assert not Membership.objects.filter(patient_key=patient.id).exists()

        mock_create.assert_not_called()


@pytest.mark.django_db
def test_provider_failure_on_join_leaves_nothing_behind():
    """Covers criterion: AC5
    Covers scenario: AC5, a provider failure on join leaves nothing behind because nothing was charged
    """
    patient = make_patient()
    api = _api("POST", "/portal/join")
    api.request = DummyRequest(
        headers={"canvas-logged-in-user-id": patient.id},
        body={"consent": True, "payment_method_id": "PM1"},
    )

    with patch.object(portal_api, "create_recurring_payment", side_effect=PayTheoryError("down")):
        result = api.join()

    assert result[0].status_code == HTTPStatus.BAD_GATEWAY
    assert not Membership.objects.filter(patient_key=patient.id).exists()
    assert len(result) == 1


@pytest.mark.django_db
def test_cancelling_inside_lock_in_is_refused_and_button_disabled():
    """Covers criterion: AC6
    Covers scenario: AC6, cancelling inside the 90 day lock in is refused and the button is disabled
    """
    patient = make_patient()
    membership = make_membership(
        patient=patient,
        status=MembershipStatus.ACTIVE,
        enrolled_at=days_ago(30),
    )

    with patch.object(portal_api, "cancel_recurring_payment") as mock_cancel:
        cancel_api = _api("POST", "/portal/cancel")
        cancel_api.request = DummyRequest(headers={"canvas-logged-in-user-id": patient.id})
        cancel_result = cancel_api.cancel()
        mock_cancel.assert_not_called()

    assert cancel_result[0].status_code == HTTPStatus.FORBIDDEN
    membership.refresh_from_db()
    assert membership.status == MembershipStatus.ACTIVE
    assert membership.cancelled_at == 0

    index_api = _api("GET", "/portal/")
    index_api.request = DummyRequest(headers={"canvas-logged-in-user-id": patient.id})
    index_result = index_api.index()
    body = index_result[0].content
    assert b'id="cancel-btn"' in body
    assert b"disabled" in body
    assert b"Cancellation opens" in body


@pytest.mark.django_db
def test_cancelling_after_lock_in_stops_subscription_with_no_chart_banner():
    """Covers criterion: AC7
    Covers scenario: AC7, cancelling after the lock in stops the subscription and puts no banner on the chart
    """
    patient = make_patient()
    membership = make_membership(
        patient=patient,
        status=MembershipStatus.ACTIVE,
        enrolled_at=days_ago(91),
        next_payment_date="2026-10-09",
    )
    make_successful_charges(membership, 3)

    with patch.object(portal_api, "cancel_recurring_payment", return_value=True):
        api = _api("POST", "/portal/cancel")
        api.request = DummyRequest(headers={"canvas-logged-in-user-id": patient.id})
        result = api.cancel()

    membership.refresh_from_db()
    assert membership.status == MembershipStatus.CANCELLING
    assert membership.ends_at == "2026-10-09"
    assert membership.cancelled_by == "patient"
    assert result[0].status_code == HTTPStatus.OK
    assert len(result) == 1


@pytest.mark.django_db
def test_new_payment_details_collect_missed_month_and_recover_membership():
    """Covers criterion: AC18
    Covers scenario: AC18, a member enters new payment details and the update collects the missed month and recovers the membership
    """
    patient = make_patient()
    membership = make_membership(
        patient=patient,
        status=MembershipStatus.PAYMENT_FAILED,
        recurring_id="R1",
        payor_id="P1",
        payment_method_id="PM1",
        failure_task_id="TK1",
    )
    make_charge(
        membership,
        recurring_id="R1",
        outcome="FAILED",
        status="INSTRUMENT_FAILURE",
        transaction_id="T-declined",
    )

    index_api = _api("GET", "/portal/")
    index_api.request = DummyRequest(headers={"canvas-logged-in-user-id": patient.id})
    index_result = index_api.index()
    body = index_result[0].content
    for element_id in (
        "pay-theory-credit-card-number",
        "pay-theory-credit-card-exp",
        "pay-theory-credit-card-cvv",
    ):
        assert element_id.encode() in body
    assert b'var payorId = "P1";' in body
    assert b"save-pay-btn" in body
    assert b"Save and pay" in body

    with patch.object(
        portal_api,
        "update_recurring_payment",
        return_value={"recurring_id": "R1", "status": "SUCCESS", "next_payment_date": "2026-11-09"},
    ) as mock_update:
        payment_api = _api("POST", "/portal/payment-method")
        payment_api.request = DummyRequest(
            headers={"canvas-logged-in-user-id": patient.id},
            body={"payment_method_id": "PM2", "last_four": "1881", "brand": "VISA"},
        )
        result = payment_api.payment_method()

    mock_update.assert_called_once()
    _, kwargs = mock_update.call_args
    assert kwargs["recurring_id"] == "R1"
    assert kwargs["payment_method_id"] == "PM2"
    assert kwargs["pay_all_missed_payments"] is True

    membership.refresh_from_db()
    assert membership.status == MembershipStatus.ACTIVE
    assert membership.payment_method_id == "PM2"
    assert membership.card_last_four == "1881"

    effects = result[1:]
    remove_banner_payloads = [
        json.loads(effect.payload)
        for effect in effects
        if effect.type == EffectType.REMOVE_BANNER_ALERT
    ]
    assert any(payload["key"] == "membership-payment-failed" for payload in remove_banner_payloads)
    update_task_payloads = [
        json.loads(effect.payload) for effect in effects if effect.type == EffectType.UPDATE_TASK
    ]
    assert len(update_task_payloads) == 1
    assert update_task_payloads[0]["data"]["id"] == "TK1"


@pytest.mark.django_db
def test_declined_first_charge_records_payment_failed_and_raises_failure_effects():
    """Covers criterion: AC26
    Covers scenario: AC26, a declined first charge on join records the membership as payment failed and raises the failure effects
    """
    patient = make_patient()
    api = _api("POST", "/portal/join")
    api.request = DummyRequest(
        headers={"canvas-logged-in-user-id": patient.id},
        body={"consent": True, "payment_method_id": "PM1"},
    )

    with patch.object(
        portal_api,
        "create_recurring_payment",
        return_value={"recurring_id": "R1", "status": "INSTRUMENT_FAILURE"},
    ):
        result = api.join()

    membership = Membership.objects.get(patient_key=patient.id)
    assert membership.status == MembershipStatus.PAYMENT_FAILED
    assert membership.recurring_id == "R1"

    effects = result[1:]
    types = [effect.type for effect in effects]
    assert EffectType.ADD_BANNER_ALERT in types
    banner_payloads = [
        json.loads(effect.payload) for effect in effects if effect.type == EffectType.ADD_BANNER_ALERT
    ]
    assert any(payload["key"] == "membership-member" for payload in banner_payloads)
    assert any(payload["key"] == "membership-payment-failed" for payload in banner_payloads)
    assert EffectType.CREATE_TASK in types
    assert EffectType.CREATE_TASK_COMMENT in types
