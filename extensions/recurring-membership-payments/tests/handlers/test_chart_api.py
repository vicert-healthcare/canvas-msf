"""Tests for ChartAPI, 02-spec/SPEC.md criteria 17, 23 and 24.

Criterion 13 and its scenario carry no test here on purpose. SPEC.md
contradicts itself there, behaviour steps 11, 12 and 18 each gate the
Cancel button on three MembershipCharge rows whose outcome is SUCCESS,
while criterion 13 gives a membership two such rows and expects the button
enabled, which that rule can never allow. 04-delivery/WORK-LEDGER.md marks
both rows unplaceable and exhausted and routes the contradiction back to
canvas-plugin-technical-spec rather than to a test that would have to pick
a side.
"""

from unittest.mock import patch

import pytest

from apex_recurring_membership_payments.handlers import chart_api
from apex_recurring_membership_payments.models.membership import MembershipStatus

from tests.support import DummyEvent, DummyRequest, days_ago, make_membership, make_patient, make_successful_charges


def _api(method: str, path: str, secrets: dict | None = None) -> chart_api.ChartAPI:
    return chart_api.ChartAPI(
        event=DummyEvent(context={"method": method, "path": path}),
        secrets=secrets or {},
    )


@pytest.mark.django_db
def test_non_member_panel_offers_no_action():
    """Covers criterion: AC17
    Covers scenario: AC17, the chart panel for a patient who is not a member offers no action
    """
    patient = make_patient()
    api = _api("GET", "/chart/")
    api.request = DummyRequest(query_params={"patient_id": patient.id})

    result = api.index()

    body = result[0].content
    assert b"Not a member" in body
    assert b"Membership page in the patient portal" in body
    assert b'id="cancel-btn"' not in body
    assert b"Enrol" not in body
    assert b"Retry" not in body


@pytest.mark.django_db
def test_staff_cannot_cancel_inside_lock_in_either():
    """Covers criterion: AC23
    Covers scenario: AC23, staff cannot cancel inside the 90 day lock in either
    """
    patient = make_patient()
    membership = make_membership(
        patient=patient, status=MembershipStatus.ACTIVE, enrolled_at=days_ago(45)
    )

    index_api = _api("GET", "/chart/")
    index_api.request = DummyRequest(query_params={"patient_id": patient.id})
    index_result = index_api.index()
    body = index_result[0].content
    assert b'id="cancel-btn"' in body
    assert b"disabled" in body
    assert b"Cancellation opens" in body

    with patch.object(chart_api, "cancel_recurring_payment") as mock_cancel:
        cancel_api = _api("POST", "/chart/cancel")
        cancel_api.request = DummyRequest(body={"patient_id": patient.id})
        cancel_result = cancel_api.cancel()
        mock_cancel.assert_not_called()

    assert cancel_result[0].status_code == 403
    membership.refresh_from_db()
    assert membership.status == MembershipStatus.ACTIVE
    assert membership.cancelled_at == 0


@pytest.mark.django_db
def test_staff_cancelling_after_lock_in_records_who_cancelled():
    """Covers criterion: AC24
    Covers scenario: AC24, staff cancelling after the lock in records who cancelled and shows it in the history
    """
    patient = make_patient()
    membership = make_membership(
        patient=patient,
        status=MembershipStatus.ACTIVE,
        enrolled_at=days_ago(100),
        next_payment_date="2026-12-09",
    )
    make_successful_charges(membership, 3)

    with patch.object(chart_api, "cancel_recurring_payment", return_value=True):
        cancel_api = _api("POST", "/chart/cancel")
        cancel_api.request = DummyRequest(body={"patient_id": patient.id})
        cancel_result = cancel_api.cancel()

    membership.refresh_from_db()
    assert membership.status == MembershipStatus.CANCELLING
    assert membership.ends_at == "2026-12-09"
    assert membership.cancelled_by == "staff"
    assert cancel_result[0].status_code == 200

    index_api = _api("GET", "/chart/")
    index_api.request = DummyRequest(query_params={"patient_id": patient.id})
    index_result = index_api.index()
    body = index_result[0].content
    assert b"Staff cancelled, ends 9 Dec 2026" in body
