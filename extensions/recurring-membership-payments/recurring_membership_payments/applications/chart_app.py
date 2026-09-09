"""The chart application, opening the membership panel for the patient in the chart header."""

from __future__ import annotations

from datetime import datetime, timezone

from canvas_sdk.effects import Effect
from canvas_sdk.effects.launch_modal import LaunchModalEffect
from canvas_sdk.handlers.application import Application

_CACHE_BUST = str(int(datetime.now(timezone.utc).timestamp()))
_CHART_PATH = "/plugin-io/api/recurring_membership_payments/chart/"


class MembershipChartApp(Application):
    """Opens the membership panel for the patient in view, served by ChartAPI.

    02-spec/SPEC.md Behaviour step 14, the patient_specific application shown
    in the chart header panel.
    """

    def on_open(self) -> Effect:
        """Return the launch effect for the chart membership panel.

        The patient id comes from the event context the platform hands to
        every patient_specific application click, and it is carried on the
        query string because the right chart pane target takes no context
        of its own, so the route reads it back from the request.
        """
        patient_id = self.event.context.get("patient")["id"]
        return LaunchModalEffect(
            url=f"{_CHART_PATH}?patient_id={patient_id}&v={_CACHE_BUST}",
            target=LaunchModalEffect.TargetType.RIGHT_CHART_PANE,
            title="Membership",
        ).apply()
