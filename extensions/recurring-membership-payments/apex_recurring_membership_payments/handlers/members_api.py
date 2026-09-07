"""The members page, staff only, its search and status filter, and the open chart action.

02-spec/SPEC.md Behaviour steps 44 to 50. MembersAPI is the whole HTTP surface
behind the staff left menu entry, GET / renders the searchable, filterable
list of every membership and POST /open-chart hands back the redirect that
moves the whole staff application to a patient's chart.

Every route here reaches a handler only once StaffSessionAuthMixin has
already confirmed the session belongs to a staff member, step 45, so no
route in this module reads a session type for itself.
"""

from datetime import datetime, timezone
from http import HTTPStatus
from typing import Any

from django.db.models import Q

from canvas_sdk.effects import Effect
from canvas_sdk.effects.redirect import RedirectEffect
from canvas_sdk.effects.simple_api import HTMLResponse, JSONResponse, Response
from canvas_sdk.handlers.simple_api import SimpleAPI, StaffSessionAuthMixin, api
from canvas_sdk.templates import render_to_string

from apex_recurring_membership_payments.logic.membership_logic import format_amount
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


def _format_date(raw: str) -> str:
    """An ISO date string as the three letter month DESIGN.md calls for, blank when there is none."""
    if not raw:
        return ""
    try:
        parsed = datetime.strptime(raw, "%Y-%m-%d")
    except ValueError:
        return raw
    return f"{parsed.strftime('%b')} {parsed.day}, {parsed.year}"


def _payment_method(membership: Membership) -> str:
    """The brand ending the last four, blank when no card has ever been tokenized."""
    if not membership.card_last_four:
        return ""
    brand = membership.card_brand or "Card"
    return f"{brand} ending {membership.card_last_four}"


def _row(membership: Membership, charge: MembershipCharge | None) -> dict[str, Any]:
    """One member's row shape, per step 47.

    The patient's name is read off the joined proxy rather than queried
    again per row, select_related in the caller is what makes that one
    query rather than one per row.
    """
    patient = membership.patient
    return {
        "patient_key": membership.patient_key,
        "first_name": patient.first_name if patient is not None else "",
        "last_name": patient.last_name if patient is not None else "",
        "status": membership.status,
        "status_label": membership.get_status_display(),
        "next_payment_date": _format_date(membership.next_payment_date),
        "amount": format_amount(membership.amount_cents),
        "charge_date": _format_date(charge.transaction_date) if charge is not None else "",
        "charge_outcome": charge.outcome if charge is not None else "",
        "payment_method": _payment_method(membership),
    }


class MembersAPI(StaffSessionAuthMixin, SimpleAPI):
    """Serves the members page and the open chart action, staff session only.

    02-spec/SPEC.md components table, the class carrying MembersPageApp's
    own route.
    """

    PREFIX = "/members"

    @api.get("/")
    def get_members(self) -> list[Response | Effect]:
        """Render the members page, filtered and searched per step 46.

        DECLINED is resolved over the whole matching set before paging,
        because whether a membership belongs on that filter depends on its
        newest charge, a fact paging must not cut off before it is applied.
        Every other status filters at the database and pages the ordinary
        way.
        """
        q = (self.request.query_params.get("q") or "").strip()
        status = self.request.query_params.get("status") or ""
        page_number = _positive_int(self.request.query_params.get("page"), 1)

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

        html = render_to_string(
            "templates/members.html",
            {
                "rows": [_row(membership, newest_by_key.get(membership.patient_key)) for membership in page_rows],
                "q": q,
                "status": status,
                "total": total,
                "page": page_number,
                "page_size": PAGE_SIZE,
                "cache_bust": _CACHE_BUST,
            },
        )
        return [HTMLResponse(html, status_code=HTTPStatus.OK)]

    @api.post("/open-chart")
    def open_chart(self) -> list[Response | Effect]:
        """Return the redirect that moves the staff application to a member's chart, step 48."""
        try:
            body = self.request.json()
        except (ValueError, TypeError):
            body = {}
        patient_key = (body or {}).get("patient_key") or ""

        if not Membership.objects.filter(patient_key=patient_key).exists():
            return [
                JSONResponse(
                    {"error": "No membership names that patient."},
                    status_code=HTTPStatus.BAD_REQUEST,
                )
            ]

        return [
            JSONResponse({"ok": True}),
            RedirectEffect(
                url=f"/patient/{patient_key}",
                target=RedirectEffect.TargetType.SAME_TAB,
            ).apply(),
        ]

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
