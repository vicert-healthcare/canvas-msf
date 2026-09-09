"""The daily health check.

Keeps the Pay Theory webhook registered and active, reconciles subscription
status for the case a delivery never arrived, and ends cancelled
memberships whose paid period is over. 02-spec/SPEC.md Behaviour steps 29
to 35, and step 42.

Steps 30 to 34 each run in their own try block, per step 35, so a Pay
Theory outage or a bad response on one step never stops the others,
step 34 above all, which touches no network and always has to run to end
a membership whose paid period has passed.

Step 42 asks for nothing further here beyond what steps 31 and 32 already
do. After a member replaces a payment method on a decline, the update
call's own response may or may not confirm the subscription left
INSTRUMENT_FAILURE, open item 13 in the specification, so step 41 never
assumes it did. What actually confirms the subscription's status is this
handler's ordinary daily query, and a subscription still failing shows up
here again on its own schedule with no special case for how it got there.
"""

from __future__ import annotations

from datetime import datetime, timezone

from canvas_sdk.effects import Effect
from canvas_sdk.handlers.cron_task import CronTask

from recurring_membership_payments.logic.membership_logic import (
    failure_effects,
    remove_member_banner_effect,
)
from recurring_membership_payments.logic.paytheory import (
    PayTheoryError,
    create_webhook,
    recurring_payments,
    update_webhook,
    webhooks,
)
from recurring_membership_payments.models.membership import Membership, MembershipStatus
from recurring_membership_payments.models.membership_charge import (
    ChargeOutcome,
    ChargeSource,
    MembershipCharge,
)

_WEBHOOK_NAME = "canvas-membership"

# The filter step 31 hands to recurringPayments, read exactly as the
# specification documents it. Whether "status" is an accepted key on this
# query is open item 5 in the specification, verified against the sandbox
# rather than here.
_NON_SUCCESS_QUERY = {
    "query_list": [
        {
            "key": "status",
            "value": "SUCCESS",
            "operator": "NOT_EQUAL",
            "conjunctive_operator": "NONE_NEXT",
        }
    ]
}

# Step 33 runs its unfiltered sweep only once this many days have passed
# with no MembershipCharge row of any kind, which is what marks the
# webhook as having gone silent rather than the membership simply being
# quiet between charges.
_SILENCE_DAYS = 32
_SILENCE_SECONDS = _SILENCE_DAYS * 24 * 60 * 60


def _now() -> int:
    """The current moment as Unix epoch seconds, UTC, matching every timestamp field in this plugin."""
    return int(datetime.now(timezone.utc).timestamp())


def _today() -> str:
    """Today's date in UTC as an ISO string, matching every date field in this plugin."""
    return datetime.now(timezone.utc).date().isoformat()


def _apply_all(effects: list) -> list[Effect]:
    """Turn a list of membership_logic's builder objects into applied Effect objects.

    membership_logic returns AddTask, AddTaskComment, AddBannerAlert and
    RemoveBannerAlert unapplied, so each carries its fields for a caller or
    a test to read before it becomes the wire shaped Effect the runner
    reads type and payload off. The same helper, in the same shape, lives
    in handlers/webhook_api.py, since the two must never drift apart on
    what turns a builder into an Effect.
    """
    return [effect if isinstance(effect, Effect) else effect.apply() for effect in effects]


class DailyHealthCheck(CronTask):
    """Once a day, keeps the webhook registered and reconciles subscription status.

    02-spec/SPEC.md Behaviour steps 29 to 35.
    """

    SCHEDULE = "15 6 * * *"

    def execute(self) -> list[Effect]:
        """Run steps 30 to 34 in order, each isolated so a provider outage on one never blocks another."""
        effects: list[Effect] = []

        try:
            self._register_or_reactivate_webhook()
        except PayTheoryError:
            pass

        try:
            effects.extend(self._reconcile_undelivered_failures())
        except PayTheoryError:
            pass

        try:
            self._reconcile_after_silence()
        except PayTheoryError:
            pass

        try:
            effects.extend(self._end_expired_cancellations())
        except Exception:
            pass

        return effects

    def _webhook_endpoint(self) -> str | None:
        """The endpoint Pay Theory should call, or nothing when CANVAS_PUBLIC_URL is unset.

        Step 30 skips registration entirely rather than registering a
        relative address Pay Theory could never reach.
        """
        public_url = (self.secrets.get("CANVAS_PUBLIC_URL") or "").strip()
        if not public_url:
            return None
        secret = self.secrets.get("PAYTHEORY_WEBHOOK_SECRET") or ""
        return f"{public_url}/plugin-io/api/recurring_membership_payments/webhook/{secret}"

    def _register_or_reactivate_webhook(self) -> None:
        """Step 30, create the webhook on first run or reactivate it once Pay Theory reports it inactive."""
        endpoint = self._webhook_endpoint()
        if endpoint is None:
            return
        existing = webhooks(self.secrets, endpoint=endpoint)
        if not existing:
            create_webhook(self.secrets, endpoint=endpoint, name=_WEBHOOK_NAME)
            return
        if existing[0].get("is_active") is False:
            update_webhook(self.secrets, endpoint=endpoint, name=_WEBHOOK_NAME, is_active=True)

    def _reconcile_undelivered_failures(self) -> list[Effect]:
        """Step 31 and step 32, catching a failed charge whose webhook never arrived."""
        result = recurring_payments(self.secrets, query=_NON_SUCCESS_QUERY, limit=100)
        items = [item for item in result["items"] if item.get("recurring_id")]
        if not items:
            return []

        recurring_ids = [item["recurring_id"] for item in items]
        memberships = {
            membership.recurring_id: membership
            for membership in Membership.objects.select_related("patient").filter(
                recurring_id__in=recurring_ids, status=MembershipStatus.ACTIVE
            )
        }

        effects: list[Effect] = []
        for item in items:
            membership = memberships.get(item["recurring_id"])
            if membership is None:
                continue
            effects.extend(self._apply_undelivered_failure(membership, item))
        return effects

    def _apply_undelivered_failure(self, membership: Membership, item: dict) -> list[Effect]:
        """Write the reconciled charge and apply step 24's failure transition, treating it as step 26 would."""
        now = _now()
        amount_cents = int(item.get("amount_per_payment") or membership.amount_cents or 0)
        transaction_date = item.get("prev_payment_date") or ""

        MembershipCharge.objects.create(
            patient_key=membership.patient_key,
            recurring_id=membership.recurring_id,
            transaction_id="",
            status=item.get("status") or "",
            outcome=ChargeOutcome.FAILED,
            amount_cents=amount_cents,
            transaction_date=transaction_date,
            source=ChargeSource.HEALTH_CHECK,
            received_at=now,
        )

        patient = membership.patient
        effects, task_id = failure_effects(
            patient_key=membership.patient_key,
            first_name=patient.first_name,
            last_name=patient.last_name,
            assignee_id=self.secrets.get("FAILURE_TASK_ASSIGNEE_ID") or None,
            team_id=self.secrets.get("FAILURE_TASK_TEAM_ID") or None,
            amount_cents=amount_cents,
            transaction_date=transaction_date,
            failure_reasons="",
        )

        membership.status = MembershipStatus.PAYMENT_FAILED
        membership.failure_task_id = task_id
        membership.updated_at = now
        membership.save()

        return _apply_all(effects)

    def _silence_reconciliation_due(self) -> bool:
        """Whether step 33's unfiltered sweep should run, per its own precondition."""
        has_open_membership = Membership.objects.filter(
            status__in=[MembershipStatus.ACTIVE, MembershipStatus.PAYMENT_FAILED]
        ).exists()
        if not has_open_membership:
            return False
        cutoff = _now() - _SILENCE_SECONDS
        return not MembershipCharge.objects.filter(received_at__gte=cutoff).exists()

    def _reconcile_after_silence(self) -> None:
        """Step 33, an unfiltered sweep run only after the webhook has gone quiet for 32 days.

        Step 33 writes a MembershipCharge row and copies next_payment_date
        and never builds a task, a comment or a banner, so nothing here
        ever returns an unapplied effect and there is nothing to normalise.
        """
        if not self._silence_reconciliation_due():
            return

        result = recurring_payments(self.secrets, limit=100)
        items = [item for item in result["items"] if item.get("recurring_id")]
        if not items:
            return

        recurring_ids = [item["recurring_id"] for item in items]
        memberships = {
            membership.recurring_id: membership
            for membership in Membership.objects.filter(
                recurring_id__in=recurring_ids,
                status__in=[MembershipStatus.ACTIVE, MembershipStatus.PAYMENT_FAILED],
            )
        }
        newest_transaction_dates = self._newest_transaction_dates(recurring_ids)
        now = _now()

        for item in items:
            membership = memberships.get(item["recurring_id"])
            if membership is None:
                continue
            prev_payment_date = item.get("prev_payment_date") or ""
            if not prev_payment_date:
                continue
            newest_date = newest_transaction_dates.get(membership.recurring_id, "")
            if prev_payment_date <= newest_date:
                continue

            outcome = ChargeOutcome.SUCCESS if item.get("status") == "SUCCESS" else ChargeOutcome.FAILED
            MembershipCharge.objects.create(
                patient_key=membership.patient_key,
                recurring_id=membership.recurring_id,
                transaction_id="",
                status=item.get("status") or "",
                outcome=outcome,
                amount_cents=int(item.get("amount_per_payment") or 0),
                transaction_date=prev_payment_date,
                source=ChargeSource.HEALTH_CHECK,
                received_at=now,
            )
            membership.next_payment_date = item.get("next_payment_date") or membership.next_payment_date
            membership.updated_at = now
            membership.save()

    @staticmethod
    def _newest_transaction_dates(recurring_ids: list[str]) -> dict[str, str]:
        """The newest MembershipCharge transaction_date per recurring_id, the comparison step 33 reads.

        Only recurring_id and transaction_date are ever read out of a row here,
        so the query is narrowed to those two columns through values() rather
        than hydrating a whole MembershipCharge per row. The ordering stays by
        received_at ascending, so the last write per recurring_id seen in the
        loop below is still the newest one, exactly as before.
        """
        newest: dict[str, str] = {}
        charges = (
            MembershipCharge.objects.filter(recurring_id__in=recurring_ids)
            .order_by("received_at")
            .values("recurring_id", "transaction_date")
        )
        for charge in charges:
            newest[charge["recurring_id"]] = charge["transaction_date"] or ""
        return newest

    def _end_expired_cancellations(self) -> list[Effect]:
        """Step 34, ending every CANCELLING membership whose paid period is over."""
        today = _today()
        effects: list[Effect] = []
        now = _now()
        memberships = Membership.objects.filter(status=MembershipStatus.CANCELLING).exclude(ends_at="")
        for membership in memberships:
            if membership.ends_at > today:
                continue
            membership.status = MembershipStatus.ENDED
            membership.recurring_id = ""
            membership.updated_at = now
            membership.save()
            effects.append(remove_member_banner_effect(membership.patient_key))
        return _apply_all(effects)
