"""One row per patient who has ever joined the membership."""

from django.db.models import (
    BigIntegerField,
    CharField,
    DO_NOTHING,
    ForeignKey,
    Index,
    IntegerField,
)

from canvas_sdk.v1.data.base import CustomModel

from apex_recurring_membership_payments.models.proxy import PatientProxy


class MembershipStatus:
    """The four states a membership can be in."""

    ACTIVE = "ACTIVE"
    PAYMENT_FAILED = "PAYMENT_FAILED"
    CANCELLING = "CANCELLING"
    ENDED = "ENDED"

    CHOICES = [
        (ACTIVE, "Active"),
        (PAYMENT_FAILED, "Payment failed"),
        (CANCELLING, "Cancelling"),
        (ENDED, "Ended"),
    ]


class Membership(CustomModel):
    """One row per patient who has ever joined, the record that outlives any single subscription."""

    patient = ForeignKey(
        PatientProxy,
        to_field="dbid",
        on_delete=DO_NOTHING,
        db_column="patient_dbid",
    )
    patient_key = CharField(max_length=64)
    recurring_id = CharField(max_length=128, default="", blank=True)
    payor_id = CharField(max_length=128, default="", blank=True)
    payment_method_id = CharField(max_length=128, default="", blank=True)
    card_brand = CharField(max_length=32, default="", blank=True)
    card_last_four = CharField(max_length=4, default="", blank=True)
    status = CharField(max_length=16, choices=MembershipStatus.CHOICES)
    amount_cents = IntegerField()
    payment_interval = CharField(max_length=16)
    enrolled_at = BigIntegerField()
    next_payment_date = CharField(max_length=10, default="", blank=True)
    ends_at = CharField(max_length=10, default="", blank=True)
    cancelled_at = BigIntegerField(default=0)
    cancelled_by = CharField(max_length=16, default="", blank=True)
    consent_at = BigIntegerField()
    failure_task_id = CharField(max_length=64, default="", blank=True)
    updated_at = BigIntegerField()

    class Meta:
        """Index every field the handlers filter or order on directly.

        patient_key is looked up alone across members_api, chart_api,
        portal_api and webhook_api. status is looked up alone in the
        health check's silence gate and in the expired cancellation
        sweep, neither of which also names recurring_id, so a composite
        index rooted at recurring_id would not serve them. recurring_id
        and status are filtered together in the health check's two
        reconciliation queries, so that pair gets one composite index
        rather than two separate ones, and the same index also serves a
        recurring_id only lookup through its leading column, which is
        what webhook_api reads when it resolves a delivery back to its
        membership.
        """

        indexes = [
            Index(fields=["patient_key"], name="membership_patient_key_idx"),
            Index(fields=["status"], name="membership_status_idx"),
            Index(fields=["recurring_id", "status"], name="membership_recur_status_idx"),
        ]

    def __str__(self) -> str:
        return f"membership for patient {self.patient_key}, {self.status}"
