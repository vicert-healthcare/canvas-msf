Apex Recurring Membership Payments
===================================

## Description

Monthly membership subscriptions charged through Pay Theory. A patient joins
or cancels from a page in the patient portal. Staff see membership status,
charge history and a cancel action from the patient's chart, and see every
member across the practice, searchable and filterable by status, from a page
in the staff left menu.

## Events and triggers

- A patient opens the Membership entry in the portal menu, which serves the
  join, active, payment failed or cancelling view of their own membership.
- A patient submits the join form, the payment method form or the cancel
  action from the portal page.
- A staff member opens the Membership icon in a patient's chart header,
  which serves that patient's membership status, charge history and a
  staff cancel action.
- A staff member opens the Members entry in the left menu, which serves the
  searchable, filterable list of every membership, and can search, filter
  by status, open a member's chart in a new tab, view a member's charge
  history in a modal, or cancel a membership from a row.
- Pay Theory delivers a webhook on every charge outcome, success or decline,
  to this plugin's own webhook route.
- A daily scheduled task keeps the webhook registration active, reconciles
  subscription status against Pay Theory, and ends any cancelled membership
  whose paid period is over.

## Effects and actions

- Opens the portal page, the chart panel and the members page as plugin
  pages or panes.
- Adds a member banner to the chart on join, replaces it with a declined
  charge banner when a payment fails, and restores the member banner when
  the balance is paid or removes it entirely when the membership ends.
- Creates a front desk task, with an instruction comment, when a charge
  fails.
- Sends the member a portal message when a charge fails.

## Configuration

Set through the plugin's secrets. The two payment provider keys and the
webhook secret are sensitive values.

- `MEMBERSHIP_PRICE_CENTS`, the monthly membership price in cents
- `MEMBERSHIP_INTERVAL`, the billing interval Pay Theory bills on
- `FAILURE_TASK_ASSIGNEE_ID`, who a failed charge task is assigned to
- `FAILURE_TASK_TEAM_ID`, which team a failed charge task is assigned to
- `CANVAS_PUBLIC_URL`, this instance's own public address
- `PAYTHEORY_API_KEY`, sensitive
- `PAYTHEORY_MERCHANT_ID`
- `PAYTHEORY_PARTNER`
- `PAYTHEORY_ENVIRONMENT`
- `PAYTHEORY_PUBLIC_KEY`
- `PAYTHEORY_SDK_URL`
- `PAYTHEORY_BROWSER_ORIGINS`
- `PAYTHEORY_WEBHOOK_SECRET`, sensitive

## External dependencies

Pay Theory is the payment provider behind every charge this plugin makes.
The portal page mounts Pay Theory's own hosted card fields through their
browser SDK, so no card number, expiry or verification code ever reaches
this plugin. Recurring payments are created, updated and cancelled through
Pay Theory's GraphQL API, and every charge outcome arrives back through a
Pay Theory webhook delivery.
