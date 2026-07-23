"""The per note detail builder.

The drill down behind a provider's count. Given a provider and the same window
the count summed, it returns one row per note carrying the five fields the story
names, the patient, the time of service, the CPT codes charged, the note type
label, and the reason for visit. It reads the same note population the engine
counts, through the shared note selection, so an expanded provider's rows match
the number shown above them. The two multi valued relations the engine did not
need, the billing line items and the commands, are prefetched here so the whole
detail set loads in a bounded number of queries, and the active only filter and
the reason for visit pick run in memory over the prefetched sets.
"""

import arrow

from canvas_sdk.v1.data.billing import BillingLineItemStatus

from provider_note_dashboard.aggregation.notes import select_notes

# The command schema key for a Reason For Visit command. The reason for visit
# lives in this command's data rather than on the note itself.
_REASON_FOR_VISIT_KEY = "reasonForVisit"


def _active_codes(note):
    """Return the active billing line items of a note as cpt and description pairs.

    Reads the prefetched billing line items in memory, keeping only the active
    ones, so it adds no query per note. These are the codes actually charged.
    """
    codes = []
    for item in note.billing_line_items.all():
        if item.status == BillingLineItemStatus.ACTIVE:
            codes.append({"cpt": item.cpt, "description": item.description})
    return codes


def _reason_for_visit(note):
    """Return the reason for visit text of a note, or the empty string when absent.

    Reads the prefetched commands in memory, finds the reason for visit command,
    and takes its structured coding text if present else its free text comment.
    Returns the empty string when the note has no queryable reason for visit
    command, which is the graceful degradation for notes that predate the
    commands module migration. The absence is a real blank, not an error.
    """
    for command in note.commands.all():
        if command.schema_key != _REASON_FOR_VISIT_KEY:
            continue
        data = command.data or {}
        coding = data.get("coding") or {}
        return coding.get("text") or data.get("comment") or ""
    return ""


def build_detail(provider_id, start, end, zone):
    """Return the detail rows for a provider over a half open window.

    provider_id is the provider's string id, start and end are the arrow moments
    bounding the window, and zone places each time of service in the viewer's
    calendar. Returns a list of rows, most recent note first, each with the
    patient display and id, the localized time of service, the active CPT codes,
    the note type label, and the reason for visit.
    """
    notes = (
        select_notes(start, end)
        .filter(provider__id=provider_id)
        .select_related("patient", "note_type_version")
        .prefetch_related("billing_line_items", "commands")
        .order_by("-datetime_of_service")
    )

    rows = []
    for note in notes:
        patient = note.patient
        note_type = note.note_type_version
        moment = arrow.get(note.datetime_of_service).to(zone)
        rows.append(
            {
                "note_id": str(note.id),
                "patient_id": str(patient.id) if patient is not None else "",
                "patient_name": patient.full_name if patient is not None else "",
                "datetime_of_service": moment.isoformat(),
                "note_type": note_type.name if note_type is not None else "",
                "codes": _active_codes(note),
                "reason_for_visit": _reason_for_visit(note),
            }
        )
    return rows
