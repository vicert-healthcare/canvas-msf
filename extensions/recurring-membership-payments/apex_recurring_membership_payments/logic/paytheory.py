"""The Pay Theory GraphQL client, one function per mutation or query this plugin uses.

Every function takes the handler's own secrets mapping as its first argument
and resolves the endpoint and the authorization header from it, so no route
and no cron task builds either one for itself. Section 3 of the specification
is the source for the endpoint shape, the header shape and the operations
below, all labelled Documented against the Pay Theory documentation, and none
of it has been confirmed against a sandbox account yet.

This module never calls createTransaction and never calls
createRetryForFailedRecurringPayment, and it never imports the unmerged
ChargeStoredCard effect, because this plugin does not charge a payment method
directly and does not offer a retry. Step 37 of the specification is the
refusal this module exists to keep, and a function for either mutation
belongs nowhere in this file.
"""

import json
from typing import Any

from canvas_sdk.utils.http import Http

_DOMAINS = {
    "production": "paytheory.com",
    "sandbox": "paytheorystudy.com",
    "lab": "paytheorylab.com",
}
_DEFAULT_ENVIRONMENT = "production"
_DEFAULT_PARTNER = "canvas"

# The statuses a PAYMENT delivery or a recurringPayments item counts as a
# successful charge, read in step 23 of the specification against the
# previous plugin's own proof on the production merchant.
SUCCESS_STATUSES = frozenset({"SUCCESS", "PENDING", "SETTLED", "SUCCEEDED"})


class PayTheoryError(Exception):
    """Raised when Pay Theory cannot be reached, times out or answers with a GraphQL error.

    A caller catches this and turns it into the plain sentence the portal and
    the chart show when nothing was charged and nothing changed, never a
    stack trace and never a guess at what went wrong on the provider's side.
    """


def resolve_endpoint(secrets: dict[str, str]) -> str:
    """Return the Pay Theory GraphQL endpoint for the configured partner and environment."""
    partner = (secrets.get("PAYTHEORY_PARTNER") or "").strip() or _DEFAULT_PARTNER
    environment = (secrets.get("PAYTHEORY_ENVIRONMENT") or "").strip() or _DEFAULT_ENVIRONMENT
    domain = _DOMAINS.get(environment, _DOMAINS[_DEFAULT_ENVIRONMENT])
    return f"https://api.{partner}.{domain}/graphql"


def resolve_headers(secrets: dict[str, str]) -> dict[str, str]:
    """Return the authorization header Pay Theory expects on every call."""
    merchant_id = secrets.get("PAYTHEORY_MERCHANT_ID") or ""
    api_key = secrets.get("PAYTHEORY_API_KEY") or ""
    return {
        "Authorization": f"{merchant_id};{api_key}",
        "Content-Type": "application/json",
    }


def _encode_metadata(metadata: dict | None) -> str | None:
    """Encode metadata as the AWSJSON scalar Pay Theory expects, a JSON string rather than an object."""
    if not metadata:
        return None
    return json.dumps(metadata)


def _request(secrets: dict[str, str], query: str, variables: dict[str, Any]) -> dict[str, Any]:
    """Post one GraphQL document to Pay Theory and return its data object, or raise PayTheoryError."""
    try:
        response = Http().post(
            resolve_endpoint(secrets),
            json={"query": query, "variables": variables},
            headers=resolve_headers(secrets),
        )
    except Exception as error:
        raise PayTheoryError("Pay Theory could not be reached.") from error

    try:
        body = response.json()
    except Exception as error:
        raise PayTheoryError("Pay Theory returned a response that could not be read.") from error

    if response.status_code != 200 or body.get("errors"):
        raise PayTheoryError("Pay Theory returned an error.")

    return body.get("data") or {}


_CREATE_RECURRING_PAYMENT = """
mutation CreateRecurringPayment($input: RecurringPaymentInput!) {
  createRecurringPayment(input: $input) {
    recurring_id
    status
    is_active
    is_processing
    next_payment_date
    prev_payment_date
    amount_per_payment
    payment_interval
    remaining_payments
    metadata
    recurring_name
  }
}
"""


def create_recurring_payment(
    secrets: dict[str, str],
    *,
    amount: int,
    merchant_uid: str,
    payment_interval: str,
    payment_method_id: str,
    recurring_name: str,
    payor_id: str | None = None,
    metadata: dict | None = None,
    first_payment_date: str | None = None,
    payment_count: int | None = None,
    mute_all_emails: bool | None = None,
    recurring_description: str | None = None,
    reference: str | None = None,
) -> dict[str, Any]:
    """Call createRecurringPayment and return the RecurringPayment it hands back.

    Omitting first_payment_date, the caller's default and the only way step 7
    is ever used, is what makes Pay Theory take the first charge immediately.
    """
    payment_input: dict[str, Any] = {
        "amount": int(amount),
        "merchant_uid": merchant_uid,
        "payment_interval": payment_interval,
        "payment_method_id": payment_method_id,
        "recurring_name": recurring_name,
    }
    if payor_id:
        payment_input["payor_id"] = payor_id
    encoded_metadata = _encode_metadata(metadata)
    if encoded_metadata is not None:
        payment_input["metadata"] = encoded_metadata
    if first_payment_date:
        payment_input["first_payment_date"] = first_payment_date
    if payment_count is not None:
        payment_input["payment_count"] = payment_count
    if mute_all_emails is not None:
        payment_input["mute_all_emails"] = mute_all_emails
    if recurring_description:
        payment_input["recurring_description"] = recurring_description
    if reference:
        payment_input["reference"] = reference

    data = _request(secrets, _CREATE_RECURRING_PAYMENT, {"input": payment_input})
    return data.get("createRecurringPayment") or {}


_UPDATE_RECURRING_PAYMENT = """
mutation UpdateRecurringPayment($input: UpdateRecurringPaymentInput!) {
  updateRecurringPayment(input: $input) {
    recurring_id
    status
    is_active
    is_processing
    next_payment_date
    prev_payment_date
    amount_per_payment
    payment_interval
    remaining_payments
    metadata
    recurring_name
  }
}
"""


def update_recurring_payment(
    secrets: dict[str, str],
    *,
    recurring_id: str,
    payment_method_id: str,
    pay_all_missed_payments: bool | None = None,
    mute_all_emails: bool | None = None,
) -> dict[str, Any]:
    """Call updateRecurringPayment and return the RecurringPayment it hands back.

    This is the only way this plugin ever replaces a payment method, used by
    step 41 when a member saves new payment details after a decline.
    """
    payment_input: dict[str, Any] = {
        "recurring_id": recurring_id,
        "payment_method_id": payment_method_id,
    }
    if pay_all_missed_payments is not None:
        payment_input["pay_all_missed_payments"] = pay_all_missed_payments
    if mute_all_emails is not None:
        payment_input["mute_all_emails"] = mute_all_emails

    data = _request(secrets, _UPDATE_RECURRING_PAYMENT, {"input": payment_input})
    return data.get("updateRecurringPayment") or {}


_CANCEL_RECURRING_PAYMENT = """
mutation CancelRecurringPayment($recurring_id: String) {
  cancelRecurringPayment(recurring_id: $recurring_id)
}
"""


def cancel_recurring_payment(secrets: dict[str, str], *, recurring_id: str) -> bool:
    """Call cancelRecurringPayment and return whether it succeeded, immediate and permanent."""
    data = _request(secrets, _CANCEL_RECURRING_PAYMENT, {"recurring_id": recurring_id})
    return bool(data.get("cancelRecurringPayment"))


_RECURRING_PAYMENTS = """
query RecurringPayments($limit: Int, $offset: String, $offset_id: String, $query: SqlQuery) {
  recurringPayments(limit: $limit, offset: $offset, offset_id: $offset_id, query: $query) {
    items {
      recurring_id
      status
      is_active
      is_processing
      next_payment_date
      prev_payment_date
      amount_per_payment
      payment_interval
      remaining_payments
      metadata
      recurring_name
    }
    total_row_count
  }
}
"""


def recurring_payments(
    secrets: dict[str, str],
    *,
    query: dict | None = None,
    limit: int = 100,
    offset: str | None = None,
    offset_id: str | None = None,
) -> dict[str, Any]:
    """Call recurringPayments and return its items and total_row_count, used only by the health check."""
    variables: dict[str, Any] = {"limit": limit}
    if query is not None:
        variables["query"] = query
    if offset is not None:
        variables["offset"] = offset
    if offset_id is not None:
        variables["offset_id"] = offset_id

    data = _request(secrets, _RECURRING_PAYMENTS, variables)
    result = data.get("recurringPayments") or {}
    return {
        "items": result.get("items") or [],
        "total_row_count": result.get("total_row_count", 0),
    }


_WEBHOOKS = """
query Webhooks($endpoint: String) {
  webhooks(endpoint: $endpoint) {
    endpoint
    is_active
    name
  }
}
"""


def webhooks(secrets: dict[str, str], *, endpoint: str) -> list[dict[str, Any]]:
    """Call webhooks and return the entries registered for this endpoint, empty when there are none."""
    data = _request(secrets, _WEBHOOKS, {"endpoint": endpoint})
    return data.get("webhooks") or []


_CREATE_WEBHOOK = """
mutation CreateWebhook($endpoint: String, $name: String) {
  createWebhook(endpoint: $endpoint, name: $name) {
    success
  }
}
"""


def create_webhook(secrets: dict[str, str], *, endpoint: str, name: str) -> bool:
    """Call createWebhook and return whether it succeeded."""
    data = _request(secrets, _CREATE_WEBHOOK, {"endpoint": endpoint, "name": name})
    return bool((data.get("createWebhook") or {}).get("success"))


_UPDATE_WEBHOOK = """
mutation UpdateWebhook($endpoint: String, $name: String, $is_active: Boolean) {
  updateWebhook(endpoint: $endpoint, name: $name, is_active: $is_active) {
    success
  }
}
"""


def update_webhook(secrets: dict[str, str], *, endpoint: str, name: str, is_active: bool) -> bool:
    """Call updateWebhook and return whether it succeeded, used by the health check to reactivate one."""
    data = _request(
        secrets,
        _UPDATE_WEBHOOK,
        {"endpoint": endpoint, "name": name, "is_active": is_active},
    )
    return bool((data.get("updateWebhook") or {}).get("success"))
