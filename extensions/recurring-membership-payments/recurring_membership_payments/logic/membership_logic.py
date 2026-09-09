"""The state transitions, the effect builders and the charge history shared by every handler and the cron.

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

This module now also carries one network call. cancel_membership reaches Pay
Theory to cancel a subscription, the single exception to every other
function here computing a pure transition or an effect with no request and
no event of its own.
"""

from datetime import datetime, timedelta, timezone
from dateutil.relativedelta import relativedelta
from uuid import uuid4

from django.db.models import Count

from canvas_sdk.effects.banner_alert.add_banner_alert import AddBannerAlert
from canvas_sdk.effects.banner_alert.remove_banner_alert import RemoveBannerAlert
from canvas_sdk.effects.note.message import Message
from canvas_sdk.effects.task.task import AddTask, AddTaskComment, TaskStatus, UpdateTask
from canvas_sdk.v1.data.staff import Staff

from recurring_membership_payments.logic.paytheory import (
    PayTheoryError,
    cancel_recurring_payment,
)
from recurring_membership_payments.models.membership import Membership, MembershipStatus
from recurring_membership_payments.models.membership_charge import (
    ChargeOutcome,
    MembershipCharge,
)

MEMBER_BANNER_KEY = "membership-member"
PAYMENT_FAILED_BANNER_KEY = "membership-payment-failed"

# The number of successful charges a membership has to have taken before
# either the patient or staff may cancel it, step 11 and step 12. The first
# practice hears this as a ninety day minimum because three monthly charges
# buy exactly ninety days, but the count is what the code and the server
# enforce, never an elapsed day.
#
# It is a plugin variable rather than a constant, because the commitment is
# the one rule of this plugin a practice sets for itself and the next one
# will not want three. The default below is what an instance that declares
# nothing runs on, and it is deliberately the original three rather than
# zero, since a missing value should keep the commitment rather than
# silently retire it.
DEFAULT_COMMITMENT_CHARGES = 3
COMMITMENT_CHARGES_SECRET = "COMMITMENT_CHARGES"

_ORDINAL_WORDS = {
    1: "first",
    2: "second",
    3: "third",
    4: "fourth",
    5: "fifth",
    6: "sixth",
    7: "seventh",
    8: "eighth",
    9: "ninth",
    10: "tenth",
    11: "eleventh",
    12: "twelfth",
}


def commitment_charges(secrets: dict) -> int:
    """The number of successful charges a membership commits to, read off the instance.

    Every caller that decides whether a cancel is allowed resolves this once
    at its own boundary and passes the number down, so nothing below holds
    an opinion about where the number came from. A value that is absent,
    unparseable or below one falls back to DEFAULT_COMMITMENT_CHARGES,
    because a zero here would retire the commitment for the whole practice
    and a typo should not be able to do that quietly.
    """
    raw = (secrets.get(COMMITMENT_CHARGES_SECRET) or "").strip()
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_COMMITMENT_CHARGES
    return value if value >= 1 else DEFAULT_COMMITMENT_CHARGES


def ordinal(count: int) -> str:
    """The word form of a small ordinal, so the refusal sentence reads the way it always did.

    Written out to twelve because that is as far as a monthly commitment
    plausibly runs and the words read better than the digits. Above that it
    falls back to the digit form with the right suffix, including the teens,
    which take th whatever their last digit is.
    """
    if count in _ORDINAL_WORDS:
        return _ORDINAL_WORDS[count]
    suffix = "th"
    if count % 100 not in (11, 12, 13):
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(count % 10, "th")
    return f"{count}{suffix}"

# The two statuses a membership has to be in before either a patient or
# staff may cancel it, read by every surface that decides whether a cancel
# control shows at all, the chart panel, the portal page and the members
# page row, and by cancel_membership itself before it reaches the provider.
CANCEL_ELIGIBLE_STATUSES = (MembershipStatus.ACTIVE, MembershipStatus.PAYMENT_FAILED)

# The longest cancellation override reason a staff member can type. Matches
# the max_length of Membership.cancel_override_reason and the maxlength both
# staff dialogs put on their reason field, so the character counter the
# component renders is the same limit the column holds and nothing a staff
# member can type is ever cut. A reason arriving longer than this can only
# come from a caller that is not one of those two dialogs, and it is
# truncated here rather than refused, because losing the tail of an
# explanation is a smaller harm than refusing a cancellation the staff
# member has already decided on.
CANCEL_REASON_MAX_LENGTH = 200

# The label a chart or a members page reader sees for who cancelled a
# membership, patient facing text never shows cancelled_by at all so this
# stays a staff facing mapping alone.
CANCELLED_BY_LABELS = {
    "patient": "The member",
    "staff": "Staff",
}

# The three ways cancel_membership can refuse, in the order the checks run.
CANCEL_NOT_ELIGIBLE = "not_eligible"
CANCEL_TOO_EARLY = "too_early"
CANCEL_PROVIDER_UNREACHABLE = "provider_unreachable"

# The sentence each refusal shows, verbatim against what the chart and the
# portal routes already said before cancel_membership existed, so a caller
# reads CANCEL_MESSAGES[reason] rather than restating any of these itself.
CANCEL_MESSAGES = {
    CANCEL_NOT_ELIGIBLE: "This membership cannot be cancelled.",
    CANCEL_TOO_EARLY: "Cancellation opens once the {ordinal} charge has been taken.",
    CANCEL_PROVIDER_UNREACHABLE: "The payment provider could not be reached. Nothing was changed.",
}


def cancel_message(reason: str, charges_before_cancel: int) -> str:
    """The sentence a refused cancel shows, with the commitment count worded into the early one.

    Every route reads this rather than indexing CANCEL_MESSAGES itself, so
    the one message that has to name a number gets it from the same value
    the check used, and the other two pass through unchanged because they
    carry no placeholder.
    """
    return CANCEL_MESSAGES[reason].format(ordinal=ordinal(charges_before_cancel))

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

_ISO_DATE_PATTERNS = ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S%z")


def format_amount(cents: int) -> str:
    """Format an integer cent amount as a dollar string, for a task comment or a banner."""
    return f"${cents / 100:,.2f}"


def format_charge_amount(cents: int) -> str:
    """Format an integer cent amount as a plain number string, for example 119.00, for a charge row.

    A charge row's own markup carries the currency sign as a fixed character
    beside the figure, so this leaves it off, unlike format_amount above
    which is written straight into a sentence and needs one.
    """
    return f"{cents / 100:,.2f}"


def format_iso_date(value: str) -> str:
    """Format an ISO date or timestamp string in the design system form, for example Mar 24, 2026.

    DESIGN.md states that form and forbids a numeric month, so this is the
    one date formatter every table and every panel reads on now, in place of
    the day first form some of these handlers used to render on their own.
    Blank when there is no value, and the raw value unchanged when none of
    the three patterns above parse it, since Pay Theory's own delivered date
    shapes are Documented rather than Read, and a value that surprises us is
    more useful shown as delivered than dropped. Built through the parsed
    day, month and year fields rather than through a platform specific
    strftime directive, since a leading zero suppressor for the day is not
    available the same way on every platform.
    """
    if not value:
        return ""
    for pattern in _ISO_DATE_PATTERNS:
        try:
            parsed = datetime.strptime(value, pattern)
        except ValueError:
            continue
        return f"{parsed.strftime('%b')} {parsed.day}, {parsed.year}"
    return value


def format_epoch(value: int) -> str:
    """Format epoch seconds in the design system form, for example Mar 24, 2026, blank when there is none."""
    if not value:
        return ""
    parsed = datetime.fromtimestamp(value, tz=timezone.utc)
    return f"{parsed.strftime('%b')} {parsed.day}, {parsed.year}"


def format_enrolled_date(enrolled_at: int) -> str:
    """Format the enrolled_at epoch seconds as the month and year a member banner names."""
    return datetime.fromtimestamp(enrolled_at, tz=timezone.utc).strftime("%B %Y")


def _advance_date(date_string: str, interval: str, periods: int) -> str:
    """Advance an ISO date string forward by a number of periods of the given interval.

    This is the one value the specification names as computed rather than
    read, the projected date shown before a membership has met its
    commitment, step 11.
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


def success_count_by_recurring_id(recurring_ids: list[str]) -> dict[str, int]:
    """Count successful MembershipCharge rows per recurring id, in one grouped query.

    Built for the members page, which has to decide whether Cancel is
    enabled on every row of a page at once rather than running can_cancel's
    own query once per row. Safe to key on recurring_id here, unlike
    charge_rows below, because Cancel only ever shows on ACTIVE and
    PAYMENT_FAILED, CANCEL_ELIGIBLE_STATUSES, and a membership in either of
    those two statuses always still carries a recurring_id, it only goes
    empty once a membership ends, step 34.
    """
    if not recurring_ids:
        return {}
    counts: dict[str, int] = {}
    grouped = (
        MembershipCharge.objects.filter(
            recurring_id__in=recurring_ids,
            outcome=ChargeOutcome.SUCCESS,
        )
        .values("recurring_id")
        .annotate(count=Count("recurring_id"))
    )
    for row in grouped:
        counts[row["recurring_id"]] = row["count"]
    return counts


def can_cancel(membership, charges_before_cancel: int) -> bool:
    """Whether the membership has taken enough successful charges to be cancelled.

    Step 11 and step 12 read the same count, so the button the patient or
    staff sees and the check the server enforces never disagree. The count
    is passed in rather than read here, so a route cannot render a button
    against one commitment while the server enforces another.
    """
    return successful_charge_count(membership) >= charges_before_cancel


def cancellation_opens_on(membership, charges_before_cancel: int) -> str:
    """The date cancellation opens, the committed charge's own date once it has landed, otherwise projected.

    Before the commitment is met, the date is projected forward from
    next_payment_date by the number of charges still owed, using the
    subscription's own payment_interval, per step 11.
    """
    charges = list(
        MembershipCharge.objects.filter(
            recurring_id=membership.recurring_id,
            outcome=ChargeOutcome.SUCCESS,
        ).order_by("received_at")
    )
    if len(charges) >= charges_before_cancel:
        return charges[charges_before_cancel - 1].transaction_date
    charges_owed = charges_before_cancel - len(charges)
    return _advance_date(membership.next_payment_date, membership.payment_interval, charges_owed)


def charge_rows(patient_key: str) -> list[dict[str, str]]:
    """Every MembershipCharge row a patient has ever produced, newest first.

    Reads on patient_key rather than on recurring_id on purpose. A
    membership's recurring_id goes empty once the membership ends, step 34,
    health_check.py line 306 is the line that clears it, so a read keyed on
    recurring_id returns nothing at all for a former member. The members
    page lists ended members and offers an Ended filter, so the history
    modal is the first surface that shows one, and a silently empty history
    for a former member is exactly what this function exists to avoid.
    Keying on patient_key keeps a former member's history reachable across
    that boundary. The one side effect worth naming is that the chart panel,
    which now calls this through charge_history_context below, shows every
    charge a patient has ever produced, including one from an earlier,
    separately joined membership, rather than only the current
    subscription's own charges. That is the intended reading, a patient's
    history belongs to the patient rather than to whichever subscription
    happens to be current.
    """
    rows = []
    for charge in MembershipCharge.objects.filter(patient_key=patient_key).order_by(
        "-received_at"
    ):
        is_success = charge.outcome == ChargeOutcome.SUCCESS
        rows.append(
            {
                "date_display": format_iso_date(charge.transaction_date),
                "amount_display": format_charge_amount(charge.amount_cents),
                "outcome_label": "Paid" if is_success else "Declined",
                "badge_color": "green" if is_success else "red",
                "reason": "" if is_success else charge.failure_reasons,
            }
        )
    return rows


def charge_history_context(
    membership, *, patient_name: str, history_label: str = ""
) -> dict:
    """Build the six keys templates/_charge_history.html reads, shared by the chart panel and the members history surfaces.

    history_label, patient_name, charges, cancelled_by, cancelled_by_label,
    cancelled_display and ends_display, in that order, are the whole of the
    contract. history_label is the table's accessible name and it defaults to
    naming the patient, which is what both staff surfaces want. The portal
    passes its own, because a patient reading their own page is not told
    their own name back. Passing
    membership as None returns the six keys empty aside from patient_name,
    which the chart panel already relies on for an ended membership it
    chooses to treat as absent, and which the members history route never
    reaches since a missing membership answers 404 before this is called.
    The cancellation trail keys come straight off the membership, exactly as
    the chart panel already read them before this function existed.
    """
    context: dict = {
        "history_label": history_label or f"Membership charges for {patient_name}",
        "patient_name": patient_name,
        "charges": [],
        "cancelled_by": "",
        "cancelled_by_label": "",
        "cancelled_display": "",
        "ends_display": "",
    }
    if membership is None:
        return context

    context["charges"] = charge_rows(membership.patient_key)
    context["ends_display"] = format_iso_date(membership.ends_at)

    if membership.cancelled_at:
        context["cancelled_by"] = membership.cancelled_by
        context["cancelled_by_label"] = CANCELLED_BY_LABELS.get(
            membership.cancelled_by, membership.cancelled_by
        )
        context["cancelled_display"] = format_epoch(membership.cancelled_at)

    return context


def cancel_membership(
    secrets: dict, *, patient_key: str, cancelled_by: str, override_reason: str = ""
) -> str:
    """Cancel one patient's membership, patient or staff initiated, empty string on success or a reason code otherwise.

    Runs the checks in the order the chart route has always run them, the
    status check, then can_cancel, then the provider call, then the write,
    and the ordering is the whole point. Pay Theory is reached only once
    both the status check and can_cancel have passed, so a request that
    could never succeed never reaches the provider at all, and when the
    provider cannot be reached this writes nothing, the membership is left
    exactly as it was. cancelled_by is passed through rather than decided
    here, staff from the chart and the members page, patient from the
    portal, so this function carries no opinion about which surface called
    it.

    override_reason is the staff early cancellation, from the engineer
    direction of 2026-09-08 at 00-inputs. A non empty reason skips the
    can_cancel check and nothing else, so the status check still runs
    first, the provider is still reached only after it passes, and a
    membership that has already ended or is already cancelling is still
    refused. The override is implied by the reason rather than declared by
    a flag beside it, which is why there is one argument here and one
    column on the row, with no second value that could disagree with it.
    It is staff only by construction rather than by a check, the portal
    route never passes this argument at all and both routes that do carry
    StaffSessionAuthMixin.
    """
    override_reason = (override_reason or "").strip()[:CANCEL_REASON_MAX_LENGTH]
    membership = Membership.objects.filter(patient_key=patient_key).first()
    if membership is None or membership.status not in CANCEL_ELIGIBLE_STATUSES:
        return CANCEL_NOT_ELIGIBLE
    if not override_reason and not can_cancel(membership, commitment_charges(secrets)):
        return CANCEL_TOO_EARLY

    try:
        cancelled = cancel_recurring_payment(secrets, recurring_id=membership.recurring_id)
    except PayTheoryError:
        cancelled = False

    if not cancelled:
        return CANCEL_PROVIDER_UNREACHABLE

    now = int(datetime.now(timezone.utc).timestamp())
    membership.status = MembershipStatus.CANCELLING
    membership.cancelled_at = now
    membership.cancelled_by = cancelled_by
    membership.cancel_override_reason = override_reason
    membership.ends_at = membership.next_payment_date
    membership.updated_at = now
    membership.save()

    return ""


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
