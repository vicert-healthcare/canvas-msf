"""One row per charge outcome received, from a webhook delivery or from the health check."""

from django.db.models import BigIntegerField, CharField, Index, IntegerField

from canvas_sdk.v1.data.base import CustomModel


class ChargeOutcome:
    """The two outcomes a charge can settle to."""

    SUCCESS = "SUCCESS"
    FAILED = "FAILED"

    CHOICES = [
        (SUCCESS, "Success"),
        (FAILED, "Failed"),
    ]


class ChargeSource:
    """The two ways a charge outcome reaches this plugin."""

    WEBHOOK = "webhook"
    HEALTH_CHECK = "health_check"

    CHOICES = [
        (WEBHOOK, "Webhook"),
        (HEALTH_CHECK, "Health check"),
    ]


class MembershipCharge(CustomModel):
    """One row per charge outcome received, the history the panels show and the idempotency key for deliveries."""

    patient_key = CharField(max_length=64)
    recurring_id = CharField(max_length=128)
    transaction_id = CharField(max_length=128, default="", blank=True)
    status = CharField(max_length=32)
    outcome = CharField(max_length=8, choices=ChargeOutcome.CHOICES)
    amount_cents = IntegerField(default=0)
    failure_reasons = CharField(max_length=512, default="", blank=True)
    transaction_date = CharField(max_length=32, default="", blank=True)
    source = CharField(max_length=16, choices=ChargeSource.CHOICES)
    received_at = BigIntegerField()

    class Meta:
        """Index every field the handlers filter or order on directly.

        patient_key and recurring_id are each read together with
        received_at, patient_key in members_api's newest charge per
        member lookup and recurring_id in chart_api, portal_api and the
        health check's silence reconciliation, always ordered by
        received_at in the same query, so each pair gets one composite
        index rather than two, and the leading column of each also
        serves a lookup on patient_key or recurring_id alone.
        transaction_id and received_at are each filtered with nothing
        else beside them, transaction_id in webhook_api's idempotency
        check and received_at in the health check's silence gate, so
        each gets its own single column index.
        """

        indexes = [
            Index(
                fields=["patient_key", "-received_at"],
                name="charge_patient_received_idx",
            ),
            Index(
                fields=["recurring_id", "-received_at"],
                name="charge_recurring_received_idx",
            ),
            Index(fields=["transaction_id"], name="charge_transaction_id_idx"),
            Index(fields=["received_at"], name="charge_received_at_idx"),
        ]

    def __str__(self) -> str:
        return f"charge for patient {self.patient_key}, {self.outcome}"
