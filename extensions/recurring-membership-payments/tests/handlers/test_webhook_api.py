"""Tests for WebhookAPI, 02-spec/SPEC.md criteria 8 to 12.

WebhookAPI carries no session mixin, the path secret is the only
authentication a delivery carries, section 2, so every test here drives
authenticate() directly for criterion 10 and calls receive_delivery()
directly, with self.request standing in for the parsed body, for every
other criterion.
"""

import json

import pytest

from recurring_membership_payments.handlers import webhook_api
from recurring_membership_payments.models.membership import Membership, MembershipStatus
from recurring_membership_payments.models.membership_charge import MembershipCharge
from canvas_sdk.effects.effect import EffectType

from tests.support import DummyEvent, DummyRequest, make_membership, make_patient, make_staff


def _api(secret_param: str, body: dict, secrets: dict | None = None) -> webhook_api.WebhookAPI:
    api = webhook_api.WebhookAPI(
        event=DummyEvent(context={"method": "POST", "path": f"/webhook/{secret_param}"}),
        secrets=secrets or {"PAYTHEORY_WEBHOOK_SECRET": "s3cret"},
    )
    api.request = DummyRequest(path_params={"secret": secret_param}, body=body)
    return api


@pytest.mark.django_db
def test_failed_delivery_raises_task_with_comment_banner_and_message():
    """Covers criterion: AC8
    Covers scenario: AC8, a failed charge delivery raises the task with its comment, the banner and the message
    """
    patient = make_patient()
    staff = make_staff()
    membership = make_membership(
        patient=patient, status=MembershipStatus.ACTIVE, recurring_id="R1"
    )
    api = _api(
        "s3cret",
        {
            "event": "PAYMENT",
            "payload": {
                "recurring": {"recurring_id": "R1"},
                "status": "FAILED",
                "transaction_id": "T1",
                "gross_amount": 11900,
                "transaction_date": "2026-09-05",
                "failure_reasons": ["insufficient funds"],
            },
        },
        secrets={"PAYTHEORY_WEBHOOK_SECRET": "s3cret", "FAILURE_TASK_ASSIGNEE_ID": staff.id},
    )

    result = api.receive_delivery()

    charge = MembershipCharge.objects.get(transaction_id="T1")
    assert charge.outcome == "FAILED"

    membership.refresh_from_db()
    assert membership.status == MembershipStatus.PAYMENT_FAILED
    assert membership.failure_task_id

    assert result[0].status_code == 200
    effects = result[1:]
    types = [effect.type for effect in effects]

    task_effect = next(effect for effect in effects if effect.type == EffectType.CREATE_TASK)
    task_payload = json.loads(task_effect.payload)
    assert task_payload["data"]["id"] == membership.failure_task_id
    assert task_payload["data"]["assignee"]["id"] == staff.id
    assert task_payload["data"]["patient"]["id"] == patient.id
    assert task_payload["data"]["due"]
    assert task_payload["data"]["labels"] == ["Membership"]

    comment_effect = next(
        effect for effect in effects if effect.type == EffectType.CREATE_TASK_COMMENT
    )
    comment_payload = json.loads(comment_effect.payload)
    assert comment_payload["data"]["task"]["id"] == membership.failure_task_id
    assert "119.00" in comment_payload["data"]["body"]
    assert "2026-09-05" in comment_payload["data"]["body"]

    banner_payloads = [
        json.loads(effect.payload) for effect in effects if effect.type == EffectType.ADD_BANNER_ALERT
    ]
    failed_banners = [payload for payload in banner_payloads if payload["key"] == "membership-payment-failed"]
    assert len(failed_banners) == 1
    assert failed_banners[0]["data"]["intent"] == "warning"

    assert EffectType.CREATE_AND_SEND_MESSAGE in types
    message_effect = next(effect for effect in effects if effect.type == EffectType.CREATE_AND_SEND_MESSAGE)
    message_payload = json.loads(message_effect.payload)
    assert message_payload["data"]["recipient_id"] == patient.id


@pytest.mark.django_db
def test_successful_charge_after_failure_recovers_membership_and_closes_task():
    """Covers criterion: AC9
    Covers scenario: AC9, a successful charge after a failure recovers the membership and closes the stored task
    """
    patient = make_patient()
    membership = make_membership(
        patient=patient,
        status=MembershipStatus.PAYMENT_FAILED,
        recurring_id="R1",
        failure_task_id="TK1",
    )
    api = _api(
        "s3cret",
        {
            "event": "PAYMENT",
            "payload": {
                "recurring": {"recurring_id": "R1", "next_payment_date": "2026-11-09"},
                "status": "SUCCESS",
                "transaction_id": "T2",
            },
        },
    )

    result = api.receive_delivery()

    charge = MembershipCharge.objects.get(transaction_id="T2")
    assert charge.outcome == "SUCCESS"

    membership.refresh_from_db()
    assert membership.status == MembershipStatus.ACTIVE
    assert membership.next_payment_date == "2026-11-09"
    assert membership.failure_task_id == ""

    effects = result[1:]
    remove_banner_payloads = [
        json.loads(effect.payload) for effect in effects if effect.type == EffectType.REMOVE_BANNER_ALERT
    ]
    assert any(payload["key"] == "membership-payment-failed" for payload in remove_banner_payloads)

    update_task_effect = next(effect for effect in effects if effect.type == EffectType.UPDATE_TASK)
    update_payload = json.loads(update_task_effect.payload)
    assert update_payload["data"]["id"] == "TK1"
    assert update_payload["data"]["status"] == "COMPLETED"


@pytest.mark.django_db
def test_delivery_with_wrong_secret_is_refused():
    """Covers criterion: AC10
    Covers scenario: AC10, a delivery with the wrong secret is refused
    """
    api = webhook_api.WebhookAPI(
        event=DummyEvent(context={"method": "POST", "path": "/webhook/wrong"}),
        secrets={"PAYTHEORY_WEBHOOK_SECRET": "s3cret"},
    )
    api.request = DummyRequest(path_params={"secret": "wrong"})

    assert api.authenticate(credentials=None) is False
    assert MembershipCharge.objects.count() == 0


@pytest.mark.django_db
@pytest.mark.parametrize(
    "payload",
    [
        {"status": "SUCCESS", "transaction_id": "T-no-recurring"},
        {
            "recurring": {"recurring_id": "R-unknown"},
            "status": "SUCCESS",
            "transaction_id": "T-unknown",
        },
    ],
)
def test_delivery_naming_no_known_membership_is_ignored(payload):
    """Covers criterion: AC11
    Covers scenario: AC11, a delivery that names no known membership is ignored
    """
    make_membership(status=MembershipStatus.ACTIVE, recurring_id="R1")
    api = _api("s3cret", {"event": "PAYMENT", "payload": payload})

    result = api.receive_delivery()

    assert result[0].status_code == 200
    assert MembershipCharge.objects.count() == 0
    assert len(result) == 1


@pytest.mark.django_db
def test_redelivered_transaction_is_written_once():
    """Covers criterion: AC12
    Covers scenario: AC12, a redelivered transaction is written once
    """
    membership = make_membership(status=MembershipStatus.ACTIVE, recurring_id="R1")
    MembershipCharge.objects.create(
        patient_key=membership.patient_key,
        recurring_id="R1",
        transaction_id="T1",
        status="SUCCESS",
        outcome="SUCCESS",
        source="webhook",
        received_at=0,
    )
    api = _api(
        "s3cret",
        {
            "event": "PAYMENT",
            "payload": {
                "recurring": {"recurring_id": "R1"},
                "status": "SUCCESS",
                "transaction_id": "T1",
            },
        },
    )

    result = api.receive_delivery()

    assert result[0].status_code == 200
    assert MembershipCharge.objects.filter(transaction_id="T1").count() == 1
    assert len(result) == 1
