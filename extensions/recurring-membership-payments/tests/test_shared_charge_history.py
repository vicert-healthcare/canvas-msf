"""Tests proving the portal page, the chart panel and the members history fragment share one table.

02-spec/SPEC.md criterion 28, the 2026-09-07 engineer direction's item 8.
The chart panel already rendered a charge history table before the members
page existed, and the redesign extracts that table into one shared include,
templates/_charge_history.html, used by the chart panel, the portal page and
the new members history route, so the three surfaces cannot drift apart the
way three separate copies of the same table eventually would. Asserting a
shared substring from the include across the three rendered bodies is
enough to prove that, rather than reading three copies of the same table
markup and comparing them by hand.
"""

import pytest

from recurring_membership_payments.handlers import chart_api, members_api, portal_api

from tests.support import DummyEvent, DummyRequest, make_charge, make_membership, make_patient


@pytest.mark.django_db
def test_portal_chart_and_members_history_render_the_same_shared_charge_history_table():
    """Covers criterion: AC28
    Covers scenario: AC28, the members history route serves the shared charge history fragment keyed on patient_key
    """
    patient = make_patient()
    membership = make_membership(patient=patient)
    make_charge(membership, transaction_id="T1")

    portal = portal_api.PortalAPI(
        event=DummyEvent(context={"method": "GET", "path": "/portal/"}), secrets={}
    )
    portal.request = DummyRequest(headers={"canvas-logged-in-user-id": patient.id})
    portal_body = portal.index()[0].content

    chart = chart_api.ChartAPI(
        event=DummyEvent(context={"method": "GET", "path": "/chart/"}), secrets={}
    )
    chart.request = DummyRequest(query_params={"patient_id": patient.id})
    chart_body = chart.index()[0].content

    members = members_api.MembersAPI(
        event=DummyEvent(
            context={"method": "GET", "path": f"/members/history/{membership.patient_key}"}
        ),
        secrets={},
    )
    members.request = DummyRequest(path_params={"patient_key": membership.patient_key})
    members_body = members.get_history()[0].content

    shared_snippet = b'id="charge-history-body"'
    assert shared_snippet in portal_body
    assert shared_snippet in chart_body
    assert shared_snippet in members_body
