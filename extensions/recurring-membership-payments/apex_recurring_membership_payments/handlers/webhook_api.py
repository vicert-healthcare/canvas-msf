"""Receives Pay Theory webhook deliveries on a secret path and records each charge outcome.

02-spec/SPEC.md Behaviour steps 20 to 25, 28 and 39. This route is the one
place charge outcomes reach the plugin, and it shares every transition and
every effect with the join route and the daily health check through
membership_logic, so a failure or a recovery never means two things
depending on where it was observed from.

This module imports nothing from apex_recurring_membership_payments.logic.paytheory
beyond the SUCCESS_STATUSES constant, a fact about which provider status
strings count as a successful charge, never a call. Step 28 of the
specification is the refusal this module exists to keep, a webhook delivery
is answered from the row it carries and from membership_logic alone, and
calling Pay Theory mid delivery belongs nowhere in this file.
"""

from datetime import datetime, timezone
from hmac import compare_digest
from typing import Any

from canvas_sdk.effects import Effect
from canvas_sdk.effects.simple_api import JSONResponse, Response
from canvas_sdk.handlers.simple_api import Credentials, SimpleAPI, api

from apex_recurring_membership_payments.logic.membership_logic import (
    failure_effects,
    recovery_effects,
)
from apex_recurring_membership_payments.logic.paytheory import SUCCESS_STATUSES
from apex_recurring_membership_payments.models.membership import Membership, MembershipStatus
from apex_recurring_membership_payments.models.membership_charge import (
    ChargeOutcome,
    ChargeSource,
    MembershipCharge,
)

_WEBHOOK_SECRET_NAME = "PAYTHEORY_WEBHOOK_SECRET"


def _apply_all(effects: list) -> list[Effect]:
    """Turn a list of membership_logic's builder objects into applied Effect objects.

    membership_logic returns AddTask, AddTaskComment, AddBannerAlert,
    RemoveBannerAlert and UpdateTask unapplied, so each carries its fields
    for a caller or a test to read before it becomes the wire shaped Effect
    a SimpleAPI route has to return. The one exception is the portal
    message, already an Effect because Message.create_and_send() applies
    itself, so this leaves anything already of that type alone rather than
    calling apply a second time.
    """
    return [effect if isinstance(effect, Effect) else effect.apply() for effect in effects]


def _joined_failure_reasons(raw: Any) -> str:
    """The delivered failure_reasons as one comma joined string, per the MembershipCharge field."""
    if isinstance(raw, list):
        return ", ".join(str(reason) for reason in raw)
    if raw:
        return str(raw)
    return ""


class WebhookAPI(SimpleAPI):
    """Accepts Pay Theory deliveries on POST /webhook/<secret>, staff and patient sessions alike refused.

    No auth mixin here, step 20. The path secret is the only authentication
    a delivery carries, so authenticate is written against the base
    Credentials class rather than against a session at all.
    """

    PREFIX = "/webhook"

    def authenticate(self, credentials: Credentials) -> bool:
        """Compare the path secret against PAYTHEORY_WEBHOOK_SECRET, step 20.

        An unset secret refuses every delivery rather than matching an
        empty path segment against an empty configured value, which is
        the trivial pass compare_digest would otherwise allow.
        """
        expected = self.secrets.get(_WEBHOOK_SECRET_NAME) or ""
        if not expected:
            return False
        provided = self.request.path_params.get("secret") or ""
        return compare_digest(provided, expected)

    @api.post("/<secret>")
    def receive_delivery(self) -> list[Response | Effect]:
        """Record one delivery's outcome and apply the failure or recovery it triggers."""
        try:
            body = self.request.json()
        except (ValueError, TypeError):
            body = {}
        if not isinstance(body, dict):
            body = {}

        payload = body.get("payload")
        payload = payload if isinstance(payload, dict) else {}
        recurring = payload.get("recurring")
        recurring = recurring if isinstance(recurring, dict) else None

        # Step 21 and step 39 in one refusal. Anything that is not a PAYMENT
        # event, PAYMENT_METHOD among them, or that names no recurring
        # subscription this plugin knows about, is ignored rather than acted
        # on. The previous plugin's intake charges arrive on the same
        # merchant webhook with no recurring object, which is exactly the
        # shape this filter exists to pass through untouched.
        recurring_id = recurring.get("recurring_id") if recurring is not None else None
        membership = (
            Membership.objects.filter(recurring_id=recurring_id).first() if recurring_id else None
        )
        if body.get("event") != "PAYMENT" or membership is None:
            return [JSONResponse({"ignored": True})]

        # Step 22. A redelivery is harmless because the idempotency key is
        # the provider's own transaction_id, and a second delivery of one
        # that already landed writes no second row and applies nothing.
        transaction_id = payload.get("transaction_id") or ""
        if transaction_id and MembershipCharge.objects.filter(transaction_id=transaction_id).exists():
            return [JSONResponse({"ok": True})]

        status = payload.get("status") or ""
        outcome = ChargeOutcome.SUCCESS if status in SUCCESS_STATUSES else ChargeOutcome.FAILED

        # Step 23. Every delivery that reaches this point writes its row and
        # copies the subscription's own next_payment_date onto the
        # membership, whichever way step 24 or step 25 then reads it.
        MembershipCharge.objects.create(
            patient_key=membership.patient_key,
            recurring_id=recurring_id,
            transaction_id=transaction_id,
            status=status,
            outcome=outcome,
            amount_cents=int(payload.get("gross_amount") or 0),
            failure_reasons=_joined_failure_reasons(payload.get("failure_reasons")),
            transaction_date=payload.get("transaction_date") or "",
            source=ChargeSource.WEBHOOK,
            received_at=int(datetime.now(timezone.utc).timestamp()),
        )
        membership.next_payment_date = recurring.get("next_payment_date") or membership.next_payment_date

        effects: list = []

        if outcome == ChargeOutcome.FAILED:
            # Step 24. Only a membership still ACTIVE moves to PAYMENT_FAILED
            # and raises the failure effects. One already PAYMENT_FAILED
            # keeps that status and raises nothing a second time, so a run
            # of declines does not raise a second task on top of the first.
            if membership.status == MembershipStatus.ACTIVE:
                built, task_id = failure_effects(
                    patient_key=membership.patient_key,
                    first_name=membership.patient.first_name,
                    last_name=membership.patient.last_name,
                    assignee_id=self.secrets.get("FAILURE_TASK_ASSIGNEE_ID"),
                    team_id=self.secrets.get("FAILURE_TASK_TEAM_ID"),
                    amount_cents=membership.amount_cents,
                    transaction_date=payload.get("transaction_date") or "",
                    failure_reasons=_joined_failure_reasons(payload.get("failure_reasons")),
                )
                membership.status = MembershipStatus.PAYMENT_FAILED
                membership.failure_task_id = task_id
                effects = built
        elif outcome == ChargeOutcome.SUCCESS:
            # Step 25. Only a membership in PAYMENT_FAILED recovers. One
            # already ACTIVE or CANCELLING keeps its status and raises
            # nothing, an ordinary month landing on a membership that was
            # never in trouble.
            if membership.status == MembershipStatus.PAYMENT_FAILED:
                built = recovery_effects(
                    patient_key=membership.patient_key,
                    enrolled_at=membership.enrolled_at,
                    failure_task_id=membership.failure_task_id,
                )
                membership.status = MembershipStatus.ACTIVE
                membership.failure_task_id = ""
                effects = built

        membership.updated_at = int(datetime.now(timezone.utc).timestamp())
        membership.save()

        return [JSONResponse({"ok": True})] + _apply_all(effects)


# No __exports__ here on purpose. The plugin sandbox refuses a module level
# __exports__ assignment in plugin code, and the handler is found through
# CANVAS_MANIFEST.json rather than through a module export list.
