from canvas_sdk.effects import Effect
from canvas_sdk.effects.launch_modal import LaunchModalEffect
from canvas_sdk.handlers.application import Application

# The page served by the SimpleAPI handler in handlers/api.py. The plugin name
# comes from CANVAS_MANIFEST.json and the app prefix is defined on that handler,
# so this url is the one seam that ties the menu entry to the page it opens.
DASHBOARD_PAGE_URL = "/plugin-io/api/provider_note_dashboard/app/dashboard"


class NoteDashboardApplication(Application):
    """Provider menu entry that opens the note dashboard as a full page."""

    def on_open(self) -> Effect:
        """Open the dashboard page when the provider clicks the menu item."""
        return LaunchModalEffect(
            url=DASHBOARD_PAGE_URL,
            target=LaunchModalEffect.TargetType.PAGE,
        ).apply()
