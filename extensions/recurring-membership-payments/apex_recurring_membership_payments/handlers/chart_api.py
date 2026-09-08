"""Serves the chart membership panel and the staff Cancel action, staff session only.

02-spec/SPEC.md Behaviour steps 15 to 18, this file's own work ledger rows 21
and 24. GET /chart/ renders the patient's membership status, history and
cancellation trail through templates/chart_panel.html, whose own context
contract is documented at the top of that file. POST /chart/cancel runs the
same three charge rule the portal enforces, step 12 and step 13, with
cancelled_by set to staff rather than patient, step 18, thin over the shared
cancel_membership.

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
    CANCEL_ELIGIBLE_STATUSES,
    CANCEL_MESSAGES,
    CANCEL_NOT_ELIGIBLE,
    CANCEL_PROVIDER_UNREACHABLE,
    CANCEL_TOO_EARLY,
    can_cancel,
    cancel_membership,
    cancellation_opens_on,
    charge_history_context,
    format_charge_amount,
    format_epoch,
    format_iso_date,
)
from apex_recurring_membership_payments.models.membership import Membership, MembershipStatus

_CACHE_BUST = str(int(datetime.now(timezone.utc).timestamp()))

_STATUS_LABELS = {
    MembershipStatus.ACTIVE: "Active",
    MembershipStatus.PAYMENT_FAILED: "Payment failed",
    MembershipStatus.CANCELLING: "Cancelling",
    MembershipStatus.ENDED: "Ended",
}

# The status each cancel_membership reason code answers with, FORBIDDEN for
# the two checks a request can fail before Pay Theory is ever reached, and
# BAD_GATEWAY for a provider that could not be reached.
_CANCEL_STATUS_BY_REASON = {
    CANCEL_NOT_ELIGIBLE: HTTPStatus.FORBIDDEN,
    CANCEL_TOO_EARLY: HTTPStatus.FORBIDDEN,
    CANCEL_PROVIDER_UNREACHABLE: HTTPStatus.BAD_GATEWAY,
}


def _panel_context(
    patient: Patient, membership: Membership | None, price_cents: int
) -> dict:
    """Build the full context dict templates/chart_panel.html reads, documented at its own top.

    Merges charge_history_context in whole rather than building the
    charges list or the cancellation trail itself, so the panel and the
    members page history modal read the same rows the same way.
    """
    patient_name = f"{patient.first_name} {patient.last_name}"
    context: dict = {
        "price_display": format_charge_amount(price_cents),
        "is_member": membership is not None,
        "status": membership.status if membership else "",
        "status_label": _STATUS_LABELS.get(membership.status, "") if membership else "",
        "enrolled_display": format_epoch(membership.enrolled_at) if membership else "",
        "next_payment_display": "",
        "card_display": "",
        "can_show_cancel": False,
        "can_cancel": False,
        "cancel_opens_display": "",
        "patient_id": patient.id,
        "cache_bust": _CACHE_BUST,
    }
    context.update(charge_history_context(membership, patient_name=patient_name))
    if membership is None:
        return context

    if membership.card_brand or membership.card_last_four:
        context["card_display"] = f"{membership.card_brand} ending {membership.card_last_four}"

    if membership.status != MembershipStatus.CANCELLING:
        context["next_payment_display"] = format_iso_date(membership.next_payment_date)

    if membership.status in CANCEL_ELIGIBLE_STATUSES:
        context["can_show_cancel"] = True
        context["can_cancel"] = can_cancel(membership)
        if not context["can_cancel"]:
            context["cancel_opens_display"] = format_iso_date(cancellation_opens_on(membership))

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

        Thin over the shared cancel_membership, cancelled_by staff. This
        route reads patient_id from the posted body while the members page
        route reads patient_key, on purpose, because the chart template
        already posts patient_id and there is no benefit in renaming a word
        an already working template sends.
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

        reason = cancel_membership(self.secrets, patient_key=patient_id, cancelled_by="staff")
        if reason:
            return [
                JSONResponse(
                    {"error": CANCEL_MESSAGES[reason]},
                    status_code=_CANCEL_STATUS_BY_REASON[reason],
                )
            ]
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
