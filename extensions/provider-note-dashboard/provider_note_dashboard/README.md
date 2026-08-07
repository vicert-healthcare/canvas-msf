provider_note_dashboard
=======================

## What it does

This plugin gives clinical leadership a dashboard of how many notes each provider
wrote, grouped by day, by week, or by month. It opens as a full page from the
provider left navigation. The page leads with a count per provider across the
chosen grouping, sorted so the busiest provider reads first, and each provider
row expands in place to the per note detail behind its count. The detail names
the patient, the time of service, the CPT codes charged, the note type, and the
reason for visit.

Only billable clinical visit notes are counted. Notes in a deleted, canceled, or
no show state are excluded, so the numbers reflect visits that actually happened.
Every count and every time is placed in the viewer's own time zone, which the
page reads from the browser and sends with each request.

## The surface

One provider menu application opens the dashboard as a full page over the plugin
HTTP interface. The application is a thin launcher. It returns a modal effect
pointed at the page route, and everything the viewer sees is served and driven by
the one API handler behind that route.

## How it is built

The plugin has two seams, each isolating the part that varies from the part that
stays fixed.

The first seam is the aggregation. The three time groupings are the varying part
of one fixed counting engine, which is a Strategy. The engine depends only on a
small bucketing contract, a way to place a moment into a period and a way to
describe the window a view spans. It never names a concrete grouping. The three
concrete strategies for day, week, and month each satisfy that contract in their
own unit, and a single composition root maps each view key to its strategy and
hands the engine whichever one the request selected. Adding a fourth grouping is
a new strategy file plus one line in that root, with no change to the engine.

The second seam is access. A single pure policy reads the lead allowlist and the
calling staff identity and returns the scope the viewer is allowed, either every
provider or only their own output. Nothing else in the code reasons about who is
a lead. The two data routes apply the returned scope, so a non lead only ever
receives their own numbers.

One shared note selection sits under both data routes. It owns the date of
service window, the billable clinical visit filter, and the excluded state set,
so the count behind a provider and the rows an expanded provider shows are always
drawn from exactly the same population and can never drift.

## Package layout

- `applications/dashboard_application.py`, the provider menu launcher that opens
  the page.
- `handlers/api.py`, the one API handler. It serves the page and the design
  system assets, resolves the view and window, applies the access scope, and
  exposes the two data routes. It is the composition root that names the
  allowlist variable and the caller identity header.
- `aggregation/bucketing.py`, the bucketing contract the engine depends on.
- `aggregation/strategies.py`, the day, week, and month strategies.
- `aggregation/registry.py`, the composition root mapping view keys to
  strategies.
- `aggregation/notes.py`, the shared note selection.
- `aggregation/engine.py`, the counting engine.
- `aggregation/detail.py`, the per note detail builder.
- `access.py`, the access policy.
- `static/dashboard.html`, the page.

## The data routes

Both routes sit behind a logged in staff session and resolve their view and
window the same way, so an expanded provider's detail spans exactly the window
its count summed. Both accept a `view` of day, week, or month, an optional `tz`
zone name defaulting to UTC, an optional `anchor` date, and an optional `periods`
count that defaults to the view's own span, fourteen days, eight weeks, or six
months.

`GET /data` returns the counts. The body carries the view, the window as an
inclusive start and an exclusive end, the ordered buckets each with a key and a
label, and the provider rows sorted by total with the busiest first. Each row
carries the provider id, the provider name, the total, and a map from bucket key
to count.

`GET /detail` returns the drill down for one provider and additionally requires a
`provider_id`. The body carries the window and one row per note, most recent
first, each with the note id, the patient id and name, the localized time of
service, the note type, the active CPT codes as code and description pairs, and
the reason for visit or an empty string when it is genuinely absent.

## Configuration

The plugin reads one variable, `lead_staff_keys`, a comma separated list of the
staff keys allowed to see across all providers.

When the list is empty the tool is open, every provider sees every provider. When
the list is populated the cross provider view is restricted to the listed leads,
and everyone else is scoped to their own output. The menu item still opens for a
non lead, onto their own scoped numbers. A staff key is matched without regard to
dashes or case, so a value pasted with either still resolves.

## Development

The `pyproject.toml` and `mypy.ini` are for local development and testing only.
The Canvas packaging process does not use them.

```
uv sync --dev
uv run pytest --cov
uv run mypy provider_note_dashboard
```

The unit tests cover the aggregation, the bucketing strategies, the access
policy, and the handler branches. They replace the note selection with stand in
objects, so they run without a database.
