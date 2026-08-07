from http import HTTPStatus

import arrow

from canvas_sdk.effects.simple_api import HTMLResponse, JSONResponse, Response
from canvas_sdk.handlers.simple_api import SimpleAPI, StaffSessionAuthMixin, api
from canvas_sdk.templates import render_to_string

from provider_note_dashboard.access import resolve_scope
from provider_note_dashboard.aggregation.detail import build_detail
from provider_note_dashboard.aggregation.engine import aggregate_counts
from provider_note_dashboard.aggregation.registry import get_strategy

# The manifest variable that holds the comma separated allowlist of lead staff
# keys. This handler is the one composition root that names it, the access
# policy receives its value and never names it.
LEAD_ALLOWLIST_NAME = "lead_staff_keys"


class NoteDashboardAPI(StaffSessionAuthMixin, SimpleAPI):
    """Serves the dashboard page, its assets, and its aggregated data.

    Every route is gated on a logged in staff session by the mixin, which is
    also what lets the authenticated Canvas iframe load the css and js. The
    counts route and the detail route hang the aggregation and detail builders
    off this same handler, so the surface stays stable while behaviour grows
    behind it. Both data routes resolve their view and window the same way, so
    the detail spans exactly the window the count summed. This handler is also
    the composition root for the access policy, it reads the allowlist and the
    caller identity and hands them to the policy, and both routes apply the
    scope the policy returns so a non lead only ever receives their own output.
    """

    PREFIX = "/app"

    def _scope(self):
        """Build the access scope for the calling staff member.

        Reads the caller key from the session header and the allowlist from
        configuration, the only place either is named, and hands both to the
        access policy. Returns the scope the routes apply.
        """
        caller_key = self.request.headers.get("canvas-logged-in-user-id", "")
        allowlist_raw = self.secrets.get(LEAD_ALLOWLIST_NAME, "")
        return resolve_scope(caller_key, allowlist_raw)

    def _resolve_window(self, params):
        """Resolve the view strategy, anchor, periods, and zone from query params.

        Returns a pair, the resolved values and an error response, with exactly
        one set. The error is a bad request when the requested view is unknown.
        Both data routes call this so their window contract is one thing, not
        two copies that could drift.
        """
        view = params.get("view", "week")
        strategy = get_strategy(view)
        if strategy is None:
            return None, JSONResponse(
                {"error": "unknown view '{0}'".format(view)},
                status_code=HTTPStatus.BAD_REQUEST,
            )

        zone = params.get("tz", "UTC")

        anchor_param = params.get("anchor")
        if anchor_param:
            anchor = arrow.get(anchor_param, tzinfo=zone)
        else:
            anchor = arrow.now(zone)

        periods = strategy.default_periods
        periods_param = params.get("periods")
        if periods_param:
            try:
                parsed = int(periods_param)
            except ValueError:
                parsed = strategy.default_periods
            if parsed >= 1:
                periods = parsed

        resolved = {
            "strategy": strategy,
            "anchor": anchor,
            "periods": periods,
            "zone": zone,
        }
        return resolved, None

    @api.get("/dashboard")
    def page(self) -> list[Response]:
        """Return the dashboard page shell."""
        return [
            HTMLResponse(
                render_to_string("static/dashboard.html"),
                status_code=HTTPStatus.OK,
            )
        ]

    @api.get("/data")
    def data(self) -> list[Response]:
        """Return counts of notes per provider for a chosen view and window."""
        resolved, error = self._resolve_window(self.request.query_params)
        if error is not None:
            return [error]

        result = aggregate_counts(
            resolved["strategy"],
            resolved["anchor"],
            resolved["periods"],
            resolved["zone"],
        )

        scope = self._scope()
        result["providers"] = [
            row for row in result["providers"] if scope.allows(row["provider_id"])
        ]
        return [JSONResponse(result, status_code=HTTPStatus.OK)]

    @api.get("/detail")
    def detail(self) -> list[Response]:
        """Return the per note detail rows for one provider over a view and window."""
        params = self.request.query_params

        provider_id = params.get("provider_id")
        if not provider_id:
            return [
                JSONResponse(
                    {"error": "provider_id is required"},
                    status_code=HTTPStatus.BAD_REQUEST,
                )
            ]

        resolved, error = self._resolve_window(params)
        if error is not None:
            return [error]

        scope = self._scope()
        if not scope.allows(provider_id):
            return [
                JSONResponse(
                    {"error": "not permitted to view this provider"},
                    status_code=HTTPStatus.FORBIDDEN,
                )
            ]

        start, end = resolved["strategy"].window(
            resolved["anchor"], resolved["periods"]
        )
        rows = build_detail(provider_id, start, end, resolved["zone"])
        return [
            JSONResponse(
                {
                    "provider_id": provider_id,
                    "window": {"start": start.isoformat(), "end": end.isoformat()},
                    "rows": rows,
                },
                status_code=HTTPStatus.OK,
            )
        ]

    @api.get("/canvas-plugin-ui.css")
    def plugin_ui_css(self) -> list[Response]:
        """Serve the design system stylesheet."""
        return [
            Response(
                render_to_string("static/canvas-plugin-ui.css").encode(),
                status_code=HTTPStatus.OK,
                content_type="text/css",
            )
        ]

    @api.get("/canvas-plugin-ui.js")
    def plugin_ui_js(self) -> list[Response]:
        """Serve the design system component bundle."""
        return [
            Response(
                render_to_string("static/canvas-plugin-ui.js").encode(),
                status_code=HTTPStatus.OK,
                content_type="application/javascript",
            )
        ]
