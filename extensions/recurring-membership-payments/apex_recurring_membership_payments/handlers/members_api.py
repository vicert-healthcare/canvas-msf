"""The members page, staff only, its search, status filter, view history modal and cancel action.

02-spec/SPEC.md Behaviour steps 44 to 50, revised by the 2026-09-07 engineer
direction on the members page redesign. MembersAPI is the whole HTTP surface
behind the staff left menu entry. GET / renders the searchable, filterable
list of every membership as a full page. GET /rows renders only the results
fragment, the debounced search and filter's own target, so a query typed
into the page never re-renders the title, the search field or the status
filter around it. GET /history/<patient_key> renders one member's charge
history for the view history modal. POST /cancel runs the shared
cancel_membership check with cancelled_by staff.

Every route here reaches a handler only once StaffSessionAuthMixin has
already confirmed the session belongs to a staff member, step 45, so no
route in this module reads a session type for itself.
"""

from datetime import date, datetime, timezone
from http import HTTPStatus
from typing import Any

from django.db.models import Q

from canvas_sdk.effects import Effect
from canvas_sdk.effects.simple_api import HTMLResponse, JSONResponse, Response
from canvas_sdk.handlers.simple_api import SimpleAPI, StaffSessionAuthMixin, api
from canvas_sdk.templates import render_to_string

from apex_recurring_membership_payments.logic.membership_logic import (
    CANCEL_ELIGIBLE_STATUSES,
    CANCEL_MESSAGES,
    CANCEL_NOT_ELIGIBLE,
    CANCEL_PROVIDER_UNREACHABLE,
    CANCEL_TOO_EARLY,
    CHARGES_BEFORE_CANCEL,
    cancel_membership,
    charge_history_context,
    format_amount,
    format_iso_date,
    success_count_by_recurring_id,
)
from apex_recurring_membership_payments.models.membership import Membership, MembershipStatus
from apex_recurring_membership_payments.models.membership_charge import (
    ChargeOutcome,
    MembershipCharge,
)

# The status values a row may actually carry. The members page also accepts
# DECLINED on the same query parameter, step 46, which is not a Membership
# status at all but a filter over each membership's newest charge, so it is
# kept out of this set and handled on its own branch.
_CACHE_BUST = str(int(datetime.now(timezone.utc).timestamp()))

_KNOWN_STATUSES = {
    MembershipStatus.ACTIVE,
    MembershipStatus.PAYMENT_FAILED,
    MembershipStatus.CANCELLING,
    MembershipStatus.ENDED,
}

_DECLINED_FILTER = "DECLINED"

# The page size step 46 names, with no number of its own, fifty a page.
PAGE_SIZE = 50

# The status each cancel_membership reason code answers with, FORBIDDEN for
# the two checks a request can fail before Pay Theory is ever reached, and
# BAD_GATEWAY for a provider that could not be reached.
_CANCEL_STATUS_BY_REASON = {
    CANCEL_NOT_ELIGIBLE: HTTPStatus.FORBIDDEN,
    CANCEL_TOO_EARLY: HTTPStatus.FORBIDDEN,
    CANCEL_PROVIDER_UNREACHABLE: HTTPStatus.BAD_GATEWAY,
}


def _positive_int(raw: str | None, default: int) -> int:
    """A query parameter read as a whole number of at least one, falling back rather than raising."""
    try:
        value = int(raw or "")
    except (TypeError, ValueError):
        return default
    return value if value >= 1 else default


def _newest_charge_by_patient_key(patient_keys: list[str]) -> dict[str, MembershipCharge]:
    """The newest MembershipCharge row for every patient key given, in one query.

    Ordering by patient_key then by received_at descending and keeping only
    the first row seen per key is the one query answer to what the newest
    charge is for a whole page of members, rather than one query per row,
    which is the shape the specification's own cited precedent avoids in
    step 46. patient_key rather than recurring_id is what this reads on,
    because a membership's recurring_id goes empty once it ends, step 34,
    and the history has to stay reachable after that.
    """
    if not patient_keys:
        return {}
    newest: dict[str, MembershipCharge] = {}
    charges = (
        MembershipCharge.objects.filter(patient_key__in=patient_keys)
        .order_by("patient_key", "-received_at")
        .only("patient_key", "transaction_date", "outcome")
    )
    for charge in charges:
        if charge.patient_key not in newest:
            newest[charge.patient_key] = charge
    return newest


def _newest_outcome_by_patient_key(patient_keys: list[str]) -> dict[str, str]:
    """The newest charge outcome alone for every patient key given, in one query.

    The DECLINED filter only ever asks whether the newest charge's outcome
    is FAILED, never anything else a MembershipCharge row carries, so this
    reads two columns through values() rather than instantiating a whole
    MembershipCharge per row the way _newest_charge_by_patient_key does for
    the page that actually renders. Same ordering trick, patient_key then
    received_at descending, first row seen per key kept.
    """
    if not patient_keys:
        return {}
    newest: dict[str, str] = {}
    charge_rows = (
        MembershipCharge.objects.filter(patient_key__in=patient_keys)
        .order_by("patient_key", "-received_at")
        .values("patient_key", "outcome")
    )
    for charge in charge_rows:
        key = charge["patient_key"]
        if key not in newest:
            newest[key] = charge["outcome"]
    return newest


def _format_birth_date(value: date | None) -> str:
    """A patient's birth_date field, a real date rather than an ISO string, in the design system form.

    Built through the parsed day, month and year fields the same way
    format_iso_date is, rather than through a platform specific strftime
    directive, and blank when there is no date on file at all.
    """
    if value is None:
        return ""
    return f"{value.strftime('%b')} {value.day}, {value.year}"


def _row(
    membership: Membership,
    charge: MembershipCharge | None,
    success_counts: dict[str, int],
) -> dict[str, Any]:
    """One member's row shape, per step 47 plus the history and cancel controls the redesign adds.

    The patient's name is read off the joined proxy rather than queried
    again per row, select_related in the caller is what makes that one
    query rather than one per row. success_counts is the whole page's own
    grouped read from success_count_by_recurring_id, so deciding can_cancel
    here costs no query of its own.
    """
    patient = membership.patient
    can_show_cancel = membership.status in CANCEL_ELIGIBLE_STATUSES
    successes = success_counts.get(membership.recurring_id, 0)
    can_cancel = can_show_cancel and successes >= CHARGES_BEFORE_CANCEL
    # Why the button is disabled, worked out from what this row already holds
    # rather than from a query of its own. The chart panel prints its reason
    # as a line under the button and a table row has nowhere to put one, so
    # the row carries it as the control's own title instead. Empty when the
    # button is live, because a tooltip on an enabled control is noise.
    cancel_blocked_reason = ""
    if not can_cancel:
        if membership.status == MembershipStatus.ENDED:
            cancel_blocked_reason = "This membership has already ended."
        elif membership.status == MembershipStatus.CANCELLING:
            cancel_blocked_reason = "This membership is already cancelling."
        else:
            cancel_blocked_reason = (
                "Cancellation opens once the third charge has been taken, "
                f"{successes} of {CHARGES_BEFORE_CANCEL} so far."
            )
    return {
        "patient_key": membership.patient_key,
        "first_name": patient.first_name if patient is not None else "",
        "last_name": patient.last_name if patient is not None else "",
        "status": membership.status,
        "status_label": membership.get_status_display(),
        "next_payment_date": format_iso_date(membership.next_payment_date),
        "amount": format_amount(membership.amount_cents),
        "charge_date": format_iso_date(charge.transaction_date) if charge is not None else "",
        "charge_outcome": charge.outcome if charge is not None else "",
        "card_brand": membership.card_brand or "",
        "card_last_four": membership.card_last_four or "",
        "can_show_cancel": can_show_cancel,
        "can_cancel": can_cancel,
        "cancel_blocked_reason": cancel_blocked_reason,
    }


def _rows_context(request) -> dict[str, Any]:
    """The whole of the members query, its filters, paging and per row disable state, per step 46.

    Held as a module level function taking the request object directly
    rather than a method, so both get_members, which renders the full page,
    and get_rows, which renders only the results fragment for the debounced
    search and filter to swap in, build the same rows the same way and can
    never drift into two different orders or two different disable rules.
    """
    q = (request.query_params.get("q") or "").strip()
    status = request.query_params.get("status") or ""
    page_number = _positive_int(request.query_params.get("page"), 1)

    memberships = Membership.objects.select_related("patient")
    if q:
        memberships = memberships.filter(
            Q(patient__first_name__icontains=q) | Q(patient__last_name__icontains=q)
        )

    if status == _DECLINED_FILTER:
        # Whether a membership belongs on this filter turns on its own
        # newest charge, a fact paging must not cut off before it is
        # applied, so every matching patient_key still has to be read
        # and every one of those patients' newest charges still has to
        # be found before the page can be cut. What is bounded here is
        # the shape of that read rather than its row count, patient_key
        # alone rather than a full Membership joined to its patient,
        # and outcome alone rather than a whole MembershipCharge, so
        # nothing beyond the fifty rows a page shows is ever turned
        # into a full model instance. Every other branch below filters
        # status at the database too and only ever reads a charge for
        # the fifty rows a page actually shows.
        ordered_keys = list(
            memberships.order_by(
                "patient__last_name", "patient__first_name"
            ).values_list("patient_key", flat=True)
        )
        newest_outcome = _newest_outcome_by_patient_key(ordered_keys)
        declined_keys = [
            patient_key
            for patient_key in ordered_keys
            if newest_outcome.get(patient_key) == ChargeOutcome.FAILED
        ]
        total = len(declined_keys)
        start = (page_number - 1) * PAGE_SIZE
        page_keys = declined_keys[start : start + PAGE_SIZE]
        page_order = {patient_key: index for index, patient_key in enumerate(page_keys)}
        page_rows = sorted(
            memberships.filter(patient_key__in=page_keys),
            key=lambda membership: page_order[membership.patient_key],
        )
    else:
        if status in _KNOWN_STATUSES:
            memberships = memberships.filter(status=status)
        ordered = memberships.order_by("patient__last_name", "patient__first_name")
        total = ordered.count()
        start = (page_number - 1) * PAGE_SIZE
        page_rows = list(ordered[start : start + PAGE_SIZE])

    newest_by_key = _newest_charge_by_patient_key(
        [membership.patient_key for membership in page_rows]
    )
    success_counts = success_count_by_recurring_id(
        [membership.recurring_id for membership in page_rows if membership.recurring_id]
    )

    return {
        "rows": [
            _row(membership, newest_by_key.get(membership.patient_key), success_counts)
            for membership in page_rows
        ],
        "q": q,
        "status": status,
        "total": total,
        "page": page_number,
        "page_size": PAGE_SIZE,
    }


class MembersAPI(StaffSessionAuthMixin, SimpleAPI):
    """Serves the members page, its search and filter, the history modal and the cancel action, staff session only.

    02-spec/SPEC.md components table, the class carrying MembersPageApp's
    own route.
    """

    PREFIX = "/members"

    @api.get("/")
    def get_members(self) -> list[Response | Effect]:
        """Render the members page, the full page rather than the fragment alone.

        Thin over _rows_context, which holds every rule about what a row is
        and which rows page in, so this only adds the cache busting query
        string the page's own script and stylesheet tags read.
        """
        context = _rows_context(self.request)
        context["cache_bust"] = _CACHE_BUST
        html = render_to_string("templates/members.html", context)
        return [HTMLResponse(html, status_code=HTTPStatus.OK)]

    @api.get("/rows")
    def get_rows(self) -> list[Response | Effect]:
        """Render only the results fragment, the debounced search and filter's own target.

        No cache_bust here, a fragment swapped into an already loaded page
        carries no script or stylesheet tag of its own to bust.
        """
        context = _rows_context(self.request)
        html = render_to_string("templates/_members_rows.html", context)
        return [HTMLResponse(html, status_code=HTTPStatus.OK)]

    @api.get("/history/<patient_key>")
    def get_history(self) -> list[Response | Effect]:
        """Render one member's charge history for the view history modal.

        Reads patient_key off self.request.path_params the way webhook_api
        reads its own path secret, rather than off a query string. Ended
        memberships answer here exactly like any other, since the members
        page lists ended members too and the whole point of reading
        charge_history_context on patient_key rather than on recurring_id is
        that a former member's history stays reachable. A key naming no
        membership at all, active or ended, answers 404 with a JSON error.
        """
        patient_key = self.request.path_params.get("patient_key") or ""
        membership = (
            Membership.objects.filter(patient_key=patient_key)
            .select_related("patient")
            .first()
        )
        if membership is None:
            return [
                JSONResponse(
                    {"error": "No membership names that patient."},
                    status_code=HTTPStatus.NOT_FOUND,
                )
            ]

        patient = membership.patient
        patient_name = f"{patient.first_name} {patient.last_name}" if patient is not None else ""
        context = charge_history_context(membership, patient_name=patient_name)
        context["patient_dob_display"] = _format_birth_date(
            patient.birth_date if patient is not None else None
        )
        html = render_to_string("templates/members_history.html", context)
        return [HTMLResponse(html, status_code=HTTPStatus.OK)]

    @api.post("/cancel")
    def cancel(self) -> list[Response | Effect]:
        """Cancel one member's membership from a members page row, staff initiated.

        Thin over the shared cancel_membership, cancelled_by staff, mapping
        the reason code it returns to a status through _CANCEL_STATUS_BY_REASON
        rather than repeating the check order here.
        """
        try:
            body = self.request.json()
        except (ValueError, TypeError):
            body = {}
        patient_key = ((body or {}).get("patient_key") or "").strip()
        if not patient_key:
            return [
                JSONResponse(
                    {"error": "A patient key is required."},
                    status_code=HTTPStatus.BAD_REQUEST,
                )
            ]

        reason = cancel_membership(self.secrets, patient_key=patient_key, cancelled_by="staff")
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


# No __exports__ here on purpose. The plugin sandbox refuses a module level
# __exports__ assignment in plugin code, and the handler is found through
# CANVAS_MANIFEST.json rather than through a module export list.
