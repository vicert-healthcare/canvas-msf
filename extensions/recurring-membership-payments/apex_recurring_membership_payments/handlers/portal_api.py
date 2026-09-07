"""Serves the portal membership page and its join, payment method and cancel actions, patient session only.

02-spec/SPEC.md Behaviour steps 2, 3, 7, 8, 9, 12, 13, 38 and 41, this file's
own work ledger rows 8, 9, 13, 14, 15, 18, 19, 43 and 46. GET /portal/ renders
the join, active, payment failed or cancelling page through
templates/portal.html, whose own context contract is documented at the top of
that file. POST /portal/join starts a membership, POST /portal/cancel enforces
the three charge rule before it stops future charges, and POST
/portal/payment-method replaces a payment method after a decline and collects
the missed charge along with it.

The two routes serving canvas-plugin-ui.css and canvas-plugin-ui.js under
this handler's /portal prefix are the orchestrator's own pass to add, per the
handover brief, and are deliberately not written here.

No card number, expiry or verification code is ever received, stored or
logged by this file, step 38. The payment details are typed into Pay
Theory's own hosted fields on the portal page, Pay Theory's own iframes, and
Pay Theory stores them. The only things any route in this file ever reads
are a payment method token, a payor id, a brand and last four digits, all
handed back from a tokenize response the patient's own browser already
produced, and none of that is ever logged.
"""

from __future__ import annotations

from datetime import datetime, timezone
from http import HTTPStatus
from uuid import uuid4

from canvas_sdk.effects import Effect
from canvas_sdk.effects.simple_api import HTMLResponse, JSONResponse, Response
from canvas_sdk.handlers.simple_api import PatientSessionAuthMixin, SimpleAPI, api
from canvas_sdk.templates import render_to_string
from canvas_sdk.v1.data.patient import Patient

from apex_recurring_membership_payments.logic.membership_logic import (
    can_cancel,
    cancellation_opens_on,
    failure_effects,
    member_banner_effect,
    recovery_effects,
)
from apex_recurring_membership_payments.logic.paytheory import (
    PayTheoryError,
    cancel_recurring_payment,
    create_recurring_payment,
    update_recurring_payment,
)
from apex_recurring_membership_payments.models.membership import Membership, MembershipStatus
from apex_recurring_membership_payments.models.membership_charge import (
    ChargeOutcome,
    MembershipCharge,
)

_CACHE_BUST = str(int(datetime.now(timezone.utc).timestamp()))

_CANCEL_ELIGIBLE_STATUSES = (MembershipStatus.ACTIVE, MembershipStatus.PAYMENT_FAILED)
_JOIN_BLOCKED_STATUSES = (
    MembershipStatus.ACTIVE,
    MembershipStatus.PAYMENT_FAILED,
    MembershipStatus.CANCELLING,
)
_DEFAULT_PRICE_CENTS = 11900
_DEFAULT_INTERVAL = "MONTHLY"

# The status the provider's own RecurringPayment carries, distinct from the
# transaction level SUCCESS_STATUSES set paytheory.py exposes for a webhook
# delivery's status field. A subscription create or update answers with one
# of SUCCESS, INSTRUMENT_FAILURE or SYSTEM_FAILURE, section 3 of the
# specification, and only the literal value SUCCESS counts as the first or
# the replacement charge landing.
_RECURRING_PAYMENT_SUCCESS = "SUCCESS"


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

    The same shape templates/chart_panel.html reads through ChartAPI's own
    helper, kept as this file's own copy rather than an import across
    handlers, so a change to one panel's history never silently reaches the
    other.
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


def _portal_context(
    *,
    patient_id: str,
    membership: Membership | None,
    payor_id: str,
    price_cents: int,
    interval_code: str,
    sdk_url: str,
    public_key: str,
) -> dict:
    """Build the full context dict templates/portal.html reads, documented at its own top.

    price_display and interval_code describe the current subscription's own
    amount_cents and payment_interval once a membership exists, since a
    price change applies to new subscriptions only and an existing member
    keeps the amount they joined at, section 6. payor_id is read from
    membership regardless of membership_state, since an ended row can still
    carry one and a rejoining member is kept on the same payor, the model's
    own field note.
    """
    effective_price_cents = membership.amount_cents if membership else price_cents
    effective_interval = membership.payment_interval if membership else interval_code

    context: dict = {
        "patient_key": patient_id,
        "paytheory_sdk_url": sdk_url,
        "paytheory_public_key": public_key,
        "csp_nonce": uuid4().hex,
        "price_display": _amount_display(effective_price_cents),
        "interval_code": effective_interval,
        "membership_state": membership.status if membership else "join",
        "payor_id": payor_id,
        "enrolled_display": "",
        "card_display": "",
        "next_payment_display": "",
        "ends_display": "",
        "can_cancel": False,
        "cancel_opens_display": "",
        "cancelled_by": "",
        "cancelled_display": "",
        "charges": [],
        "cache_bust": _CACHE_BUST,
    }
    if membership is None:
        return context

    context["enrolled_display"] = _format_epoch(membership.enrolled_at)
    if membership.card_brand or membership.card_last_four:
        context["card_display"] = f"{membership.card_brand} ending {membership.card_last_four}"

    if membership.status == MembershipStatus.CANCELLING:
        context["ends_display"] = _format_iso_date(membership.ends_at)
    else:
        context["next_payment_display"] = _format_iso_date(membership.next_payment_date)

    context["charges"] = _charge_rows(membership)

    if membership.cancelled_at:
        context["cancelled_by"] = membership.cancelled_by
        context["cancelled_display"] = _format_epoch(membership.cancelled_at)
        context["ends_display"] = context["ends_display"] or _format_iso_date(membership.ends_at)

    if membership.status in _CANCEL_ELIGIBLE_STATUSES:
        context["can_cancel"] = can_cancel(membership)
        if not context["can_cancel"]:
            context["cancel_opens_display"] = _format_iso_date(cancellation_opens_on(membership))

    return context


class PortalAPI(PatientSessionAuthMixin, SimpleAPI):
    """Serves the portal membership page and its join, payment method and cancel actions, patient session only."""

    PREFIX = "/portal"

    @api.get("/")
    def index(self) -> list[Response | Effect]:
        """GET /portal/, step 2 and step 3.

        The requesting patient's own Membership row is loaded by patient_key
        from the session header PatientSessionAuthMixin has already
        confirmed belongs to a logged in patient, never from a query string
        or a body the caller could forge, step 2. The response carries a
        fresh Content Security Policy header per request naming the
        configured Pay Theory origins in script-src, frame-src and
        connect-src, and 'self' in script-src and style-src as well, since a
        design system asset loaded through a link or a script tag is not
        covered by 'unsafe-inline', step 3.
        """
        patient_id = self.request.headers.get("canvas-logged-in-user-id", "")
        raw_membership = Membership.objects.filter(patient_key=patient_id).first()
        membership = (
            None
            if raw_membership is None or raw_membership.status == MembershipStatus.ENDED
            else raw_membership
        )

        price_cents = int(self.secrets.get("MEMBERSHIP_PRICE_CENTS") or _DEFAULT_PRICE_CENTS)
        interval_code = self.secrets.get("MEMBERSHIP_INTERVAL") or _DEFAULT_INTERVAL
        context = _portal_context(
            patient_id=patient_id,
            membership=membership,
            payor_id=raw_membership.payor_id if raw_membership else "",
            price_cents=price_cents,
            interval_code=interval_code,
            sdk_url=self.secrets.get("PAYTHEORY_SDK_URL") or "",
            public_key=self.secrets.get("PAYTHEORY_PUBLIC_KEY") or "",
        )

        origins = self.secrets.get("PAYTHEORY_BROWSER_ORIGINS") or ""
        policy = (
            "default-src 'self'; "
            f"script-src 'self' 'nonce-{context['csp_nonce']}' {origins}; "
            f"frame-src {origins}; "
            f"connect-src 'self' {origins}; "
            # The design system's Lato face is fetched from Google Fonts by a
            # link tag the canvas-plugin-ui skill's own head boilerplate fixes,
            # so the stylesheet origin belongs in style-src and the font file
            # origin in font-src. Both are hardcoded rather than configured,
            # because the template hardcodes the same link and a policy that
            # forbids what the markup requests renders every page in the
            # browser's default serif. font-src is stated explicitly since it
            # would otherwise fall back to default-src 'self' and block the
            # woff2 even once the stylesheet is allowed.
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com; "
            "img-src 'self' data:"
        )
        return [
            HTMLResponse(
                render_to_string("templates/portal.html", context),
                headers={"Content-Security-Policy": policy},
            )
        ]

    @api.post("/join")
    def join(self) -> list[Response | Effect]:
        """POST /portal/join, step 7, step 8 and step 9.

        Consent and a token are required before the provider is ever
        called, step 7, and a membership already ACTIVE, PAYMENT_FAILED or
        CANCELLING refuses a second join rather than starting a second
        subscription over the first. A recurring_id coming back always
        writes the row and always carries the member banner, step 8, and
        when the same response's own status is not SUCCESS the same
        failure effects step 24 builds are applied at once, on top of that
        banner, so the front desk hears about a declined first charge the
        moment it happens rather than waiting on a delivery. No
        MembershipCharge row is ever written here, the first month's row
        arrives through the PAYMENT webhook exactly like every later one,
        step 21. A call that raises, times out or answers with no
        recurring_id at all means nothing was created and nothing was
        charged, so the route writes nothing and reports the plain
        sentence, step 9.
        """
        patient_id = self.request.headers.get("canvas-logged-in-user-id", "")
        body = self.request.json() or {}
        consent = body.get("consent") is True
        payment_method_id = (body.get("payment_method_id") or "").strip()
        if not consent or not payment_method_id:
            return [
                JSONResponse(
                    {"error": "Consent and a payment method are required."},
                    status_code=HTTPStatus.BAD_REQUEST,
                )
            ]

        existing = Membership.objects.filter(patient_key=patient_id).first()
        if existing is not None and existing.status in _JOIN_BLOCKED_STATUSES:
            return [
                JSONResponse(
                    {"error": "This patient already has a membership."},
                    status_code=HTTPStatus.CONFLICT,
                )
            ]

        patient = Patient.objects.filter(id=patient_id).first()
        if patient is None:
            return [
                JSONResponse(
                    {"error": "A valid patient session is required."},
                    status_code=HTTPStatus.BAD_REQUEST,
                )
            ]

        price_cents = int(self.secrets.get("MEMBERSHIP_PRICE_CENTS") or _DEFAULT_PRICE_CENTS)
        interval = self.secrets.get("MEMBERSHIP_INTERVAL") or _DEFAULT_INTERVAL
        posted_payor_id = (body.get("payor_id") or "").strip()

        try:
            result = create_recurring_payment(
                self.secrets,
                amount=price_cents,
                merchant_uid=self.secrets.get("PAYTHEORY_MERCHANT_ID") or "",
                payment_interval=interval,
                payment_method_id=payment_method_id,
                payor_id=posted_payor_id or None,
                recurring_name="Apex membership",
                metadata={"canvas_patient_id": patient_id},
            )
        except PayTheoryError:
            result = None

        if not result or not result.get("recurring_id"):
            return [
                JSONResponse(
                    {
                        "error": (
                            "The membership could not be started. Nothing was charged. "
                            "Try again in a moment."
                        )
                    },
                    status_code=HTTPStatus.BAD_GATEWAY,
                )
            ]

        is_new_or_ended = existing is None or existing.status == MembershipStatus.ENDED
        membership = existing if existing is not None else Membership(
            patient_id=patient.dbid, patient_key=patient_id
        )

        now = int(datetime.now(timezone.utc).timestamp())
        membership.patient_key = patient_id
        membership.recurring_id = result.get("recurring_id") or ""
        membership.payor_id = posted_payor_id
        membership.payment_method_id = payment_method_id
        membership.card_brand = (body.get("brand") or "").strip()
        membership.card_last_four = (body.get("last_four") or "").strip()
        membership.amount_cents = price_cents
        membership.payment_interval = interval
        membership.next_payment_date = result.get("next_payment_date") or ""
        membership.consent_at = now
        if is_new_or_ended:
            membership.enrolled_at = now
        membership.updated_at = now

        effects: list = [
            member_banner_effect(patient_key=patient_id, enrolled_at=membership.enrolled_at)
        ]

        provider_status = result.get("status") or ""
        if provider_status == _RECURRING_PAYMENT_SUCCESS:
            membership.status = MembershipStatus.ACTIVE
        else:
            built, task_id = failure_effects(
                patient_key=patient_id,
                first_name=patient.first_name,
                last_name=patient.last_name,
                assignee_id=self.secrets.get("FAILURE_TASK_ASSIGNEE_ID"),
                team_id=self.secrets.get("FAILURE_TASK_TEAM_ID"),
                amount_cents=price_cents,
                transaction_date=datetime.now(timezone.utc).date().isoformat(),
                failure_reasons="",
            )
            membership.status = MembershipStatus.PAYMENT_FAILED
            membership.failure_task_id = task_id
            effects.extend(built)

        membership.save()

        return [JSONResponse({"ok": True}, status_code=HTTPStatus.OK)] + _apply_all(effects)

    @api.post("/cancel")
    def cancel(self) -> list[Response | Effect]:
        """POST /portal/cancel, step 12 and step 13.

        The same three charge count step 11 shows on the page is enforced
        again here before the provider is ever called, so a request that
        cannot succeed never reaches Pay Theory. On a true result the
        membership moves to CANCELLING with no banner effect, since the
        member banner stays until the paid period ends and cancellation
        shows only in the history trail, step 13.
        """
        patient_id = self.request.headers.get("canvas-logged-in-user-id", "")
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
                    {"error": "Cancellation opens once the third charge has been taken."},
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
                    {"error": "The payment provider could not be reached. Nothing was changed."},
                    status_code=HTTPStatus.BAD_GATEWAY,
                )
            ]

        now = int(datetime.now(timezone.utc).timestamp())
        membership.status = MembershipStatus.CANCELLING
        membership.cancelled_at = now
        membership.cancelled_by = "patient"
        membership.ends_at = membership.next_payment_date
        membership.updated_at = now
        membership.save()

        return [JSONResponse({"ok": True}, status_code=HTTPStatus.OK)]

    @api.post("/payment-method")
    def payment_method(self) -> list[Response | Effect]:
        """POST /portal/payment-method, step 41.

        Only a membership in PAYMENT_FAILED accepts a replacement, and the
        update always asks Pay Theory to collect every missed payment along
        with the new payment method, pay_all_missed_payments true, so the
        missed month is never a second manual step. A response whose own
        status reads SUCCESS moves the membership back to ACTIVE and
        applies the same recovery effects step 27 builds, otherwise the row
        still keeps the new payment method and waits for the missed
        charge's own outcome to arrive as a PAYMENT webhook, step 21, since
        step 42 leaves open whether this response settles that on its own.
        A provider failure changes nothing.
        """
        patient_id = self.request.headers.get("canvas-logged-in-user-id", "")
        body = self.request.json() or {}
        payment_method_id = (body.get("payment_method_id") or "").strip()
        if not payment_method_id:
            return [
                JSONResponse(
                    {"error": "A payment method is required."},
                    status_code=HTTPStatus.BAD_REQUEST,
                )
            ]

        membership = Membership.objects.filter(patient_key=patient_id).first()
        if membership is None or membership.status != MembershipStatus.PAYMENT_FAILED:
            return [
                JSONResponse(
                    {"error": "This membership is not waiting on a payment method."},
                    status_code=HTTPStatus.CONFLICT,
                )
            ]

        try:
            result = update_recurring_payment(
                self.secrets,
                recurring_id=membership.recurring_id,
                payment_method_id=payment_method_id,
                pay_all_missed_payments=True,
            )
        except PayTheoryError:
            result = None

        if not result or not result.get("recurring_id"):
            return [
                JSONResponse(
                    {"error": "The payment method could not be saved. Try again in a moment."},
                    status_code=HTTPStatus.BAD_GATEWAY,
                )
            ]

        now = int(datetime.now(timezone.utc).timestamp())
        membership.payment_method_id = payment_method_id
        membership.card_brand = (body.get("brand") or "").strip()
        membership.card_last_four = (body.get("last_four") or "").strip()
        membership.next_payment_date = result.get("next_payment_date") or membership.next_payment_date
        membership.updated_at = now

        effects: list = []
        provider_status = result.get("status") or ""
        if provider_status == _RECURRING_PAYMENT_SUCCESS:
            built = recovery_effects(
                patient_key=patient_id,
                enrolled_at=membership.enrolled_at,
                failure_task_id=membership.failure_task_id,
            )
            membership.status = MembershipStatus.ACTIVE
            membership.failure_task_id = ""
            effects = built

        membership.save()

        return [JSONResponse({"ok": True}, status_code=HTTPStatus.OK)] + _apply_all(effects)

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


# No __exports__ here on purpose. The plugin sandbox refuses a module level
# __exports__ assignment in plugin code, and the handler is found through
# CANVAS_MANIFEST.json rather than through a module export list.
