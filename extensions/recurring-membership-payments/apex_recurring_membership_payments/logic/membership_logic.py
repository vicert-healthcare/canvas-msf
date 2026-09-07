"""The state transitions and the effect builders shared by every handler and the cron.

Splitting this module out of the handlers is what lets the join route, the
webhook route, the payment method route and the daily health check apply the
same failure and the same recovery exactly once each, and what lets a test
drive a transition with no request and no event, per the specification's own
note in the components table.

No function in this module ever creates a note, a billing line item, a claim
or a claim payment. Step 36 of the specification is the refusal this module
exists to keep, the charge history lives in MembershipCharge alone, and a
failed or a recovered charge is told to the front desk and to the member
through a task, a comment, a banner and a message, never through the
Canvas ledger. Reaching for one of those four here, however small, is the one
thing this file was written to avoid.
"""

from datetime import datetime, timedelta, timezone
from dateutil.relativedelta import relativedelta
from uuid import uuid4

from canvas_sdk.effects.banner_alert.add_banner_alert import AddBannerAlert
from canvas_sdk.effects.banner_alert.remove_banner_alert import RemoveBannerAlert
from canvas_sdk.effects.note.message import Message
from canvas_sdk.effects.task.task import AddTask, AddTaskComment, TaskStatus, UpdateTask
from canvas_sdk.v1.data.staff import Staff

from apex_recurring_membership_payments.models.membership_charge import (
    ChargeOutcome,
    MembershipCharge,
)

MEMBER_BANNER_KEY = "membership-member"
PAYMENT_FAILED_BANNER_KEY = "membership-payment-failed"

# The number of successful charges a membership has to have taken before
# either the patient or staff may cancel it, step 11 and step 12. The
# practice hears this as a ninety day minimum because three monthly charges
# buy exactly ninety days, but the count is what the code and the server
# enforce, never an elapsed day.
CHARGES_BEFORE_CANCEL = 3

_INTERVAL_DAYS = {
    "WEEKLY": 7,
    "BI_WEEKLY": 14,
}
_INTERVAL_MONTHS = {
    "MONTHLY": 1,
    "QUARTERLY": 3,
    "BI_ANNUAL": 6,
    "ANNUAL": 12,
}


def format_amount(cents: int) -> str:
    """Format an integer cent amount as a dollar string, for a task comment or a banner."""
    return f"${cents / 100:,.2f}"


def format_enrolled_date(enrolled_at: int) -> str:
    """Format the enrolled_at epoch seconds as the month and year a member banner names."""
    return datetime.fromtimestamp(enrolled_at, tz=timezone.utc).strftime("%B %Y")


def _advance_date(date_string: str, interval: str, periods: int) -> str:
    """Advance an ISO date string forward by a number of periods of the given interval.

    This is the one value the specification names as computed rather than
    read, the projected date shown before a membership has taken its third
    charge, step 11.
    """
    date = datetime.strptime(date_string, "%Y-%m-%d").date()
    if periods <= 0:
        return date.isoformat()
    if interval in _INTERVAL_DAYS:
        return (date + timedelta(days=_INTERVAL_DAYS[interval] * periods)).isoformat()
    months = _INTERVAL_MONTHS.get(interval, 1) * periods
    return (date + relativedelta(months=months)).isoformat()


def successful_charge_count(membership) -> int:
    """Count the successful MembershipCharge rows belonging to a membership's current subscription."""
    return MembershipCharge.objects.filter(
        recurring_id=membership.recurring_id,
        outcome=ChargeOutcome.SUCCESS,
    ).count()


def can_cancel(membership) -> bool:
    """Whether the membership has taken enough successful charges to be cancelled.

    Step 11 and step 12 read the same count, so the button the patient or
    staff sees and the check the server enforces never disagree.
    """
    return successful_charge_count(membership) >= CHARGES_BEFORE_CANCEL


def cancellation_opens_on(membership) -> str:
    """The date cancellation opens, the third charge's own date once it has landed, otherwise projected.

    Before the third charge, the date is projected forward from
    next_payment_date by the number of charges still owed, using the
    subscription's own payment_interval, per step 11.
    """
    charges = list(
        MembershipCharge.objects.filter(
            recurring_id=membership.recurring_id,
            outcome=ChargeOutcome.SUCCESS,
        ).order_by("received_at")
    )
    if len(charges) >= CHARGES_BEFORE_CANCEL:
        return charges[CHARGES_BEFORE_CANCEL - 1].transaction_date
    charges_owed = CHARGES_BEFORE_CANCEL - len(charges)
    return _advance_date(membership.next_payment_date, membership.payment_interval, charges_owed)


def member_banner_effect(*, patient_key: str, enrolled_at: int) -> AddBannerAlert:
    """The member banner, shown on join and put back on recovery, step 8 and step 27."""
    return AddBannerAlert(
        patient_id=patient_key,
        key=MEMBER_BANNER_KEY,
        narrative=f"Practice member since {format_enrolled_date(enrolled_at)}",
        placement=[AddBannerAlert.Placement.CHART],
        intent=AddBannerAlert.Intent.INFO,
    )


def remove_member_banner_effect(patient_key: str) -> RemoveBannerAlert:
    """Remove the member banner, on a failure and when a membership ends, step 26 and step 34."""
    return RemoveBannerAlert(patient_id=patient_key, key=MEMBER_BANNER_KEY)


def payment_failed_banner_effect(*, patient_key: str, transaction_date: str) -> AddBannerAlert:
    """The declined charge banner, raised in place of the member banner, step 26."""
    return AddBannerAlert(
        patient_id=patient_key,
        key=PAYMENT_FAILED_BANNER_KEY,
        narrative=f"Membership charge declined on {transaction_date}, see Membership panel",
        placement=[AddBannerAlert.Placement.CHART],
        intent=AddBannerAlert.Intent.WARNING,
    )


def remove_payment_failed_banner_effect(patient_key: str) -> RemoveBannerAlert:
    """Remove the declined charge banner, on recovery, step 27 and step 41."""
    return RemoveBannerAlert(patient_id=patient_key, key=PAYMENT_FAILED_BANNER_KEY)


def _failure_task(
    *,
    patient_key: str,
    first_name: str,
    last_name: str,
    assignee_id: str | None,
    team_id: str | None,
) -> tuple[AddTask, str]:
    """Mint the failure task and its id, the id the caller stores as failure_task_id, step 26."""
    task_id = str(uuid4())
    due = datetime.now(timezone.utc) + timedelta(days=2)
    task = AddTask(
        id=task_id,
        title=f"Contact {first_name} {last_name} about a declined membership charge",
        patient_id=patient_key,
        assignee_id=assignee_id or None,
        team_id=team_id or None,
        due=due,
        labels=["Membership"],
        status=TaskStatus.OPEN,
    )
    return task, task_id


def _failure_task_comment(
    *,
    task_id: str,
    amount_cents: int,
    transaction_date: str,
    failure_reasons: str,
) -> AddTaskComment:
    """The comment paired with the failure task in the same effect list, step 26."""
    body = (
        f"The membership charge of {format_amount(amount_cents)} on {transaction_date} was declined, "
        f"{failure_reasons}. Contact the member. They can enter new payment details on the Membership "
        "page in the portal, which collects the missed month and continues the membership. Pay Theory "
        "will not retry on its own."
    )
    return AddTaskComment(task_id=task_id, body=body)


def _failure_message(*, patient_key: str, assignee_id: str | None) -> Message | None:
    """The portal message to the member, sent only when assignee_id names an existing Staff, step 26."""
    if not assignee_id or not Staff.objects.filter(id=assignee_id).exists():
        return None
    return Message(
        content=(
            "Your membership payment did not go through. Open Membership in the portal to enter new "
            "payment details, or contact the practice."
        ),
        sender_id=assignee_id,
        recipient_id=patient_key,
    ).create_and_send()


def failure_effects(
    *,
    patient_key: str,
    first_name: str,
    last_name: str,
    assignee_id: str | None,
    team_id: str | None,
    amount_cents: int,
    transaction_date: str,
    failure_reasons: str,
) -> tuple[list, str]:
    """Build the failure effects of step 26, used by the join route, the webhook route and the health check.

    Returns the effect list and the minted task id. The caller stores that id
    on the Membership row as failure_task_id, so a later recovery can close
    the same task by id rather than by title.
    """
    task, task_id = _failure_task(
        patient_key=patient_key,
        first_name=first_name,
        last_name=last_name,
        assignee_id=assignee_id,
        team_id=team_id,
    )
    comment = _failure_task_comment(
        task_id=task_id,
        amount_cents=amount_cents,
        transaction_date=transaction_date,
        failure_reasons=failure_reasons,
    )
    effects: list = [
        task,
        comment,
        remove_member_banner_effect(patient_key),
        payment_failed_banner_effect(patient_key=patient_key, transaction_date=transaction_date),
    ]
    message = _failure_message(patient_key=patient_key, assignee_id=assignee_id)
    if message is not None:
        effects.append(message)
    return effects, task_id


def recovery_effects(
    *,
    patient_key: str,
    enrolled_at: int,
    failure_task_id: str | None,
) -> list:
    """Build the recovery effects of step 27, used by the webhook route, the payment method route and the health check.

    Putting the member banner back is the mirror of the removal step 26
    performs, so the chart never carries both banners at once. The caller
    clears failure_task_id on the Membership row after this returns.
    """
    effects: list = [
        remove_payment_failed_banner_effect(patient_key),
        member_banner_effect(patient_key=patient_key, enrolled_at=enrolled_at),
    ]
    if failure_task_id:
        effects.append(UpdateTask(id=failure_task_id, status=TaskStatus.COMPLETED))
    return effects
