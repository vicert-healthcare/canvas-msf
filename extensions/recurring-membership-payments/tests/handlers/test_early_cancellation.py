"""The staff early cancellation, criteria AC30 to AC33 of the specification.

Every test names the criterion and the scenario it covers, the way every
other test in this project does, so one grep over tests/ produces the whole
mapping from the feature file to the code.
"""
from unittest.mock import patch

import pytest

from recurring_membership_payments.logic import membership_logic
from recurring_membership_payments.models.membership import MembershipStatus
from tests.handlers.test_members_api import _api
from tests.support import DummyRequest, make_membership, make_patient


@pytest.mark.django_db
def test_override_lets_staff_end_early():
    """Covers criterion: AC30
    Covers scenario: AC30, a reason lets staff end a membership before its commitment is met and is recorded on the row
    """
    patient = make_patient()
    membership = make_membership(patient=patient, status=MembershipStatus.ACTIVE)

    with patch.object(
        membership_logic, "cancel_recurring_payment", return_value=True
    ) as mock_cancel:
        api = _api("POST", "/members/cancel")
        api.request = DummyRequest(
            body={
                "patient_key": patient.id,
                "override_reason": "Member moved out of state, manager approved the waiver.",
            }
        )
        result = api.cancel()
        mock_cancel.assert_called_once()

    assert result[0].status_code == 200
    membership.refresh_from_db()
    assert membership.status == MembershipStatus.CANCELLING
    assert membership.cancelled_by == "staff"
    assert membership.cancel_override_reason == (
        "Member moved out of state, manager approved the waiver."
    )


@pytest.mark.django_db
def test_blank_reason_is_still_refused_and_truncation_holds():
    """Covers criterion: AC31
    Covers scenario: AC31, a reason is trimmed before it counts and truncated before it is stored
    """
    patient = make_patient()
    make_membership(patient=patient, status=MembershipStatus.ACTIVE)

    with patch.object(membership_logic, "cancel_recurring_payment") as mock_cancel:
        api = _api("POST", "/members/cancel")
        api.request = DummyRequest(body={"patient_key": patient.id, "override_reason": "   "})
        result = api.cancel()
        mock_cancel.assert_not_called()
    assert result[0].status_code == 403

    with patch.object(
        membership_logic, "cancel_recurring_payment", return_value=True
    ):
        api = _api("POST", "/members/cancel")
        api.request = DummyRequest(body={"patient_key": patient.id, "override_reason": "x" * 400})
        result = api.cancel()
    assert result[0].status_code == 200
    from recurring_membership_payments.models.membership import Membership

    row = Membership.objects.get(patient_key=patient.id)
    assert len(row.cancel_override_reason) == 200


@pytest.mark.django_db
def test_ended_membership_still_refuses_even_with_a_reason():
    """Covers criterion: AC32
    Covers scenario: AC32, a reason never skips the status check, only the commitment
    """
    patient = make_patient()
    make_membership(patient=patient, status=MembershipStatus.ENDED)

    with patch.object(membership_logic, "cancel_recurring_payment") as mock_cancel:
        api = _api("POST", "/members/cancel")
        api.request = DummyRequest(body={"patient_key": patient.id, "override_reason": "because"})
        result = api.cancel()
        mock_cancel.assert_not_called()
    assert result[0].status_code == 403


@pytest.mark.django_db
def test_chart_panel_shows_the_override_trail():
    """Covers criterion: AC33
    Covers scenario: AC33, the chart panel shows the override trail once a reason is recorded
    """
    patient = make_patient()
    make_membership(
        patient=patient,
        status=MembershipStatus.CANCELLING,
        cancelled_at=1757000000,
        cancelled_by="staff",
        cancel_override_reason="Member moved out of state.",
    )
    from recurring_membership_payments.handlers.chart_api import ChartAPI
    from tests.handlers.test_chart_api import _api as chart_api

    api = chart_api("GET", "/chart/")
    api.request = DummyRequest(query_params={"patient_id": patient.id})
    body = api.index()[0].content
    assert b"Ended early by staff. Reason, Member moved out of state." in body
