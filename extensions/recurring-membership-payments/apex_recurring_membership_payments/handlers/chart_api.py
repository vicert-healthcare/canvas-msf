"""Serves the chart membership panel and the staff Cancel action, staff session only.

02-spec/SPEC.md Behaviour steps 15 to 18, this file's own work ledger rows 21
and 24. GET /chart/ renders the patient's membership status, history and
cancellation trail through templates/chart_panel.html, whose own context
contract is documented at the top of that file. POST /chart/cancel runs the
same three charge rule the portal enforces, step 12 and step 13, with
cancelled_by set to staff rather than patient, step 18.

The two routes serving canvas-plugin-ui.css and canvas-plugin-ui.js under
this handler's /chart prefix are the orchestrator's own pass to add, per the
handover brief, and are deliberately not written here.

No card number, expiry or verification code is ever received, stored or
logged by this file, step 38. Staff never enrol a patient from this panel,
step 17, the only path to a payment method is Pay Theory's own fields on the
portal page.
"""

from __future__ import annotations

from datetime import datetime, timezone
from http import HTTPStatus

from canvas_sdk.effects import Effect
from canvas_sdk.effects.simple_api import HTMLResponse, JSONResponse, Response
from canvas_sdk.handlers.simple_api import SimpleAPI, StaffSessionAuthMixin, api
from canvas_sdk.templates import render_to_string
from canvas_sdk.v1.data.patient import Patient

from apex_recurring_membership_payments.logic.membership_logic import (
    can_cancel,
    cancellation_opens_on,
)
from apex_recurring_membership_payments.logic.paytheory import (
    PayTheoryError,
    cancel_recurring_payment,
)
from apex_recurring_membership_payments.models.membership import Membership, MembershipStatus
from apex_recurring_membership_payments.models.membership_charge import (
    ChargeOutcome,
    MembershipCharge,
)

_CACHE_BUST = str(int(datetime.now(timezone.utc).timestamp()))

_STATUS_LABELS = {
    MembershipStatus.ACTIVE: "Active",
    MembershipStatus.PAYMENT_FAILED: "Payment failed",
    MembershipStatus.CANCELLING: "Cancelling",
    MembershipStatus.ENDED: "Ended",
}

_CANCEL_ELIGIBLE_STATUSES = (MembershipStatus.ACTIVE, MembershipStatus.PAYMENT_FAILED)

_CANCELLED_BY_LABELS = {
    "patient": "The member",
    "staff": "Staff",
}


def _format_iso_date(value: str) -> str:
    """Format a "YYYY-MM-DD" date string as "9 Sep 2026", falling back to the raw value.

    Pay Theory's own delivered date shapes are Documented rather than Read, so a
    value that does not parse is shown as delivered rather than dropped, which
    keeps a charge row informative even when the provider's format surprises us.
    """
    if not value:
        return ""
    for pattern in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return datetime.strptime(value, pattern).strftime("%-d %b %Y")
        except ValueError:
            continue
    return value


def _format_epoch(value: int) -> str:
    """Format epoch seconds as "9 Sep 2026"."""
    if not value:
        return ""
    return datetime.fromtimestamp(value, tz=timezone.utc).strftime("%-d %b %Y")


def _amount_display(cents: int) -> str:
    """Format an integer cent amount as a plain number string, for example "119.00"."""
    return f"{cents / 100:,.2f}"


def _charge_rows(membership: Membership) -> list[dict[str, str]]:
    """Every MembershipCharge row for this membership's current subscription, newest first.

    Reads every row this membership has ever produced, across a replaced
    payment method or a reconciled failure, so the chart panel's history
    never loses a charge a payment method swap or a health check reconcile
    wrote under the same recurring_id. Ledger row 22, step 16.
    """
    rows = []
    for charge in MembershipCharge.objects.filter(
        recurring_id=membership.recurring_id
    ).order_by("-received_at"):
        is_success = charge.outcome == ChargeOutcome.SUCCESS
        rows.append(
            {
                "date_display": _format_iso_date(charge.transaction_date),
                "amount_display": _amount_display(charge.amount_cents),
                "outcome_label": "Paid" if is_success else "Declined",
                "badge_color": "green" if is_success else "red",
                "reason": "" if is_success else charge.failure_reasons,
            }
        )
    return rows


def _panel_context(
    patient: Patient, membership: Membership | None, price_cents: int
) -> dict:
    """Build the full context dict templates/chart_panel.html reads, documented at its own top."""
    context: dict = {
        "patient_name": f"{patient.first_name} {patient.last_name}",
        "price_display": _amount_display(price_cents),
        "is_member": membership is not None,
        "status": membership.status if membership else "",
        "status_label": _STATUS_LABELS.get(membership.status, "") if membership else "",
        "enrolled_display": _format_epoch(membership.enrolled_at) if membership else "",
        "next_payment_display": "",
        "ends_display": "",
        "card_display": "",
        "charges": [],
        "cancelled_by": "",
        "cancelled_by_label": "",
        "cancelled_display": "",
        "can_show_cancel": False,
        "can_cancel": False,
        "cancel_opens_display": "",
        "patient_id": patient.id,
        "cache_bust": _CACHE_BUST,
    }
    if membership is None:
        return context

    if membership.card_brand or membership.card_last_four:
        context["card_display"] = f"{membership.card_brand} ending {membership.card_last_four}"

    if membership.status == MembershipStatus.CANCELLING:
        context["ends_display"] = _format_iso_date(membership.ends_at)
    else:
        context["next_payment_display"] = _format_iso_date(membership.next_payment_date)

    context["charges"] = _charge_rows(membership)

    if membership.cancelled_at:
        context["cancelled_by"] = membership.cancelled_by
        context["cancelled_by_label"] = _CANCELLED_BY_LABELS.get(
            membership.cancelled_by, membership.cancelled_by
        )
        context["cancelled_display"] = _format_epoch(membership.cancelled_at)
        context["ends_display"] = context["ends_display"] or _format_iso_date(membership.ends_at)

    if membership.status in _CANCEL_ELIGIBLE_STATUSES:
        context["can_show_cancel"] = True
        context["can_cancel"] = can_cancel(membership)
        if not context["can_cancel"]:
            context["cancel_opens_display"] = _format_iso_date(cancellation_opens_on(membership))

    return context


class ChartAPI(StaffSessionAuthMixin, SimpleAPI):
    """Serves the chart membership panel and its staff Cancel action, staff session only."""

    PREFIX = "/chart"

    @api.get("/")
    def index(self) -> list[Response | Effect]:
        """GET /chart/, step 15 and step 16.

        Reads patient_id off the query string, the way the chart application's
        own LaunchModalEffect url carries it, and renders the panel for
        whichever of a missing membership, an ended one, or an active,
        payment failed or cancelling one applies, step 17 for the first case.
        """
        patient_id = self.request.query_params.get("patient_id", "")
        patient = Patient.objects.filter(id=patient_id).first()
        if patient is None:
            return [
                JSONResponse(
                    {"error": "A valid patient id is required."},
                    status_code=HTTPStatus.BAD_REQUEST,
                )
            ]

        membership = Membership.objects.filter(patient_key=patient_id).first()
        if membership is not None and membership.status == MembershipStatus.ENDED:
            membership = None

        price_cents = int(self.secrets.get("MEMBERSHIP_PRICE_CENTS") or 11900)
        context = _panel_context(patient, membership, price_cents)
        return [HTMLResponse(render_to_string("templates/chart_panel.html", context))]

    @api.post("/cancel")
    def cancel(self) -> list[Response | Effect]:
        """POST /chart/cancel, step 18.

        Runs the same three charge rule step 12 enforces, with cancelled_by
        set to staff rather than patient, and calls the provider only after
        both the status and the charge count checks pass, so a request that
        cannot succeed never reaches Pay Theory.
        """
        body = self.request.json() or {}
        patient_id = (body.get("patient_id") or "").strip()
        if not patient_id:
            return [
                JSONResponse(
                    {"error": "A patient id is required."},
                    status_code=HTTPStatus.BAD_REQUEST,
                )
            ]

        membership = Membership.objects.filter(patient_key=patient_id).first()
        if membership is None or membership.status not in _CANCEL_ELIGIBLE_STATUSES:
            return [
                JSONResponse(
                    {"error": "This membership cannot be cancelled."},
                    status_code=HTTPStatus.FORBIDDEN,
                )
            ]
        if not can_cancel(membership):
            return [
                JSONResponse(
                    {
                        "error": "Cancellation opens once the third charge has been taken.",
                    },
                    status_code=HTTPStatus.FORBIDDEN,
                )
            ]

        try:
            cancelled = cancel_recurring_payment(
                self.secrets, recurring_id=membership.recurring_id
            )
        except PayTheoryError:
            cancelled = False

        if not cancelled:
            return [
                JSONResponse(
                    {
                        "error": "The payment provider could not be reached. Nothing was changed.",
                    },
                    status_code=HTTPStatus.BAD_GATEWAY,
                )
            ]

        now = int(datetime.now(timezone.utc).timestamp())
        membership.status = MembershipStatus.CANCELLING
        membership.cancelled_at = now
        membership.cancelled_by = "staff"
        membership.ends_at = membership.next_payment_date
        membership.updated_at = now
        membership.save()

        return [JSONResponse({"ok": True}, status_code=HTTPStatus.OK)]

    @api.get("/canvas-plugin-ui.css")
    def plugin_ui_css(self) -> list[Response]:
        """Serves the design system stylesheet under this handler's own prefix.

        Written by the orchestrator rather than by the group that wrote the rest
        of this file. The two routes cannot be inherited from a shared base,
        because SimpleAPIBase builds its route registry from the class's own
        __dict__ and never walks the method resolution order, and SimpleAPI
        raises when a route handler name also appears on a superclass.
        """
        return [
            Response(
                render_to_string("static/canvas-plugin-ui.css").encode(),
                status_code=HTTPStatus.OK,
                content_type="text/css",
            )
        ]

    @api.get("/canvas-plugin-ui.js")
    def plugin_ui_js(self) -> list[Response]:
        """Serves the design system bundle under this handler's own prefix."""
        return [
            Response(
                render_to_string("static/canvas-plugin-ui.js").encode(),
                status_code=HTTPStatus.OK,
                content_type="application/javascript",
            )
        ]
