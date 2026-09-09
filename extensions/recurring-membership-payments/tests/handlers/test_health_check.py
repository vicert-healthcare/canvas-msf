"""Tests for DailyHealthCheck, 02-spec/SPEC.md criterion 15.

execute() is called directly on a DailyHealthCheck built with a secrets dict
and no event at all, since CronTask.execute reads nothing off self.event,
only steps 30 to 34 through the Pay Theory client functions this module
imported by name, every one of them patched here rather than called for
real.
"""

import json
from unittest.mock import patch

import pytest

from recurring_membership_payments.handlers import health_check
from recurring_membership_payments.models.membership import MembershipStatus
from recurring_membership_payments.models.membership_charge import (
    ChargeOutcome,
    ChargeSource,
    MembershipCharge,
)
from canvas_sdk.effects.effect import EffectType

from tests.support import iso_date_days_from_now, make_membership, make_patient, make_staff


@pytest.mark.django_db
def test_daily_health_check_reactivates_webhook_reconciles_failures_and_ends_cancellations():
    """Covers criterion: AC15
    Covers scenario: AC15, the daily health check reactivates the webhook, reconciles failures and ends cancelled memberships
    """
    staff = make_staff()
    active_patient = make_patient()
    active_membership = make_membership(
        patient=active_patient,
        status=MembershipStatus.ACTIVE,
        recurring_id="R1",
    )
    cancelling_patient = make_patient()
    cancelling_membership = make_membership(
        patient=cancelling_patient,
        status=MembershipStatus.CANCELLING,
        recurring_id="R2",
        ends_at=iso_date_days_from_now(-1),
    )

    task = health_check.DailyHealthCheck(
        event=None,
        secrets={
            "CANVAS_PUBLIC_URL": "https://example.canvasmedical.com",
            "PAYTHEORY_WEBHOOK_SECRET": "s3cret",
            "FAILURE_TASK_ASSIGNEE_ID": staff.id,
        },
    )

    with (
        patch.object(health_check, "webhooks", return_value=[{"is_active": False}]) as mock_webhooks,
        patch.object(health_check, "create_webhook") as mock_create_webhook,
        patch.object(health_check, "update_webhook", return_value=True) as mock_update_webhook,
        patch.object(
            health_check,
            "recurring_payments",
            return_value={
                "items": [
                    {
                        "recurring_id": "R1",
                        "status": "INSTRUMENT_FAILURE",
                        "amount_per_payment": 11900,
                        "prev_payment_date": "2026-09-01",
                    }
                ],
                "total_row_count": 1,
            },
        ) as mock_recurring_payments,
    ):
        effects = task.execute()

    mock_webhooks.assert_called_once()
    mock_create_webhook.assert_not_called()
    mock_update_webhook.assert_called_once()
    _, update_kwargs = mock_update_webhook.call_args
    assert update_kwargs["is_active"] is True

    mock_recurring_payments.assert_called_once()

    charge = MembershipCharge.objects.get(recurring_id="R1", source=ChargeSource.HEALTH_CHECK)
    assert charge.outcome == ChargeOutcome.FAILED

    active_membership.refresh_from_db()
    assert active_membership.status == MembershipStatus.PAYMENT_FAILED

    cancelling_membership.refresh_from_db()
    assert cancelling_membership.status == MembershipStatus.ENDED

    types = [effect.type for effect in effects]
    assert EffectType.CREATE_TASK in types
    assert EffectType.CREATE_TASK_COMMENT in types

    remove_banner_payloads = [
        json.loads(effect.payload) for effect in effects if effect.type == EffectType.REMOVE_BANNER_ALERT
    ]
    assert any(
        payload["patient"] == cancelling_patient.id and payload["key"] == "membership-member"
        for payload in remove_banner_payloads
    )
