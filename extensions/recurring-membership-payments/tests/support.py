"""Shared test doubles and factories for recurring_membership_payments.

Not collected as a test module itself, tests/tests.py in the pytest.ini
python_files list, and this file is named neither test_*.py nor *_tests.py
nor tests.py, so pytest leaves it alone and every handler test imports from
it instead of redeclaring the same double.

DummyEvent and DummyRequest stand in for the real Event and Request the SDK
builds from a live gRPC call. A handler under SimpleAPIBase only ever reads
self.event.context for routing in __init__ and self.request for everything
else, and request is an ordinary attribute a test can overwrite after
construction, since only the SDK's own Request is a cached_property, so
these two lightweight doubles are the whole of what a route needs to run.
"""

from datetime import date, timedelta
from typing import Any

from canvas_sdk.test_utils.factories import PatientFactory, StaffFactory

from recurring_membership_payments.models.membership import Membership, MembershipStatus
from recurring_membership_payments.models.membership_charge import (
    ChargeOutcome,
    ChargeSource,
    MembershipCharge,
)


class DummyEvent:
    """Stands in for canvas_sdk.events.Event, carrying only the context dict a handler reads."""

    def __init__(self, context: dict[str, Any]) -> None:
        self.context = context


class DummyRequest:
    """Stands in for canvas_sdk.handlers.simple_api.api.Request.

    Every handler in this plugin reads at most headers, query_params,
    path_params and json() off its own request, so this carries exactly
    those four and nothing the real Request class does for a body that
    is not JSON.
    """

    def __init__(
        self,
        headers: dict[str, str] | None = None,
        query_params: dict[str, str] | None = None,
        path_params: dict[str, str] | None = None,
        body: Any = None,
    ) -> None:
        self.headers = headers or {}
        self.query_params = query_params or {}
        self.path_params = path_params or {}
        self._body = body if body is not None else {}

    def json(self) -> Any:
        """Return the body a test constructed this request with."""
        return self._body


def make_patient(**overrides: Any):
    """A real Patient row, through the SDK's own factory, patient_key and patient_dbid both real."""
    return PatientFactory.create(**overrides)


def make_staff(**overrides: Any):
    """A real Staff row, through the SDK's own factory, so a Message or an AddTask assignee validates."""
    return StaffFactory.create(**overrides)


def days_ago(count: int) -> int:
    """A Unix epoch second timestamp, UTC, count days before now, for enrolled_at and cancelled_at."""
    from datetime import datetime, timezone

    return int((datetime.now(timezone.utc) - timedelta(days=count)).timestamp())


def iso_date_days_from_now(count: int) -> str:
    """An ISO date string count days from today, for next_payment_date and ends_at."""
    return (date.today() + timedelta(days=count)).isoformat()


def make_membership(patient=None, **overrides: Any) -> Membership:
    """A Membership row with every required field defaulted, one keyword overriding any of them.

    patient defaults to a freshly made one when the caller has no patient of
    its own to reuse, the ordinary case for a scenario that cares about one
    membership in isolation. next_payment_date defaults to a real future
    date rather than an empty string, since a membership only ever exists
    once a join has copied one back from the provider, and the projected
    cancellation date membership_logic computes for a membership short of
    its third charge parses that field as a date every time it renders.
    """
    if patient is None:
        patient = make_patient()
    fields: dict[str, Any] = {
        "patient_id": patient.dbid,
        "patient_key": patient.id,
        "recurring_id": "",
        "payor_id": "",
        "payment_method_id": "",
        "card_brand": "",
        "card_last_four": "",
        "status": MembershipStatus.ACTIVE,
        "amount_cents": 11900,
        "payment_interval": "MONTHLY",
        "enrolled_at": days_ago(0),
        "next_payment_date": iso_date_days_from_now(30),
        "ends_at": "",
        "cancelled_at": 0,
        "cancelled_by": "",
        "consent_at": days_ago(0),
        "failure_task_id": "",
        "updated_at": days_ago(0),
    }
    fields.update(overrides)
    return Membership.objects.create(**fields)


def make_charge(membership: Membership, **overrides: Any) -> MembershipCharge:
    """A MembershipCharge row belonging to membership, successful by default."""
    fields: dict[str, Any] = {
        "patient_key": membership.patient_key,
        "recurring_id": membership.recurring_id,
        "transaction_id": "",
        "status": "SUCCESS",
        "outcome": ChargeOutcome.SUCCESS,
        "amount_cents": membership.amount_cents,
        "failure_reasons": "",
        "transaction_date": date.today().isoformat(),
        "source": ChargeSource.WEBHOOK,
        "received_at": days_ago(0),
    }
    fields.update(overrides)
    return MembershipCharge.objects.create(**fields)


def make_successful_charges(membership: Membership, count: int) -> list[MembershipCharge]:
    """count successful MembershipCharge rows for membership, oldest first, spaced a day apart.

    Used wherever a criterion names an elapsed day count alongside a
    membership that must also carry the three successful charges the
    version 4 Cancel rule reads, so the count is satisfied and the rule
    is satisfied at the same time, never one without the other.
    """
    charges = []
    for index in range(count):
        charges.append(
            make_charge(
                membership,
                transaction_id=f"T-{membership.patient_key}-{index}",
                transaction_date=(date.today() - timedelta(days=(count - index) * 30)).isoformat(),
                received_at=days_ago((count - index) * 30),
            )
        )
    return charges
