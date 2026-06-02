# PREVENT CVD Calculator

A Canvas plugin that embeds the AHA PREVENT™ cardiovascular-risk calculator
directly inside the patient chart.

## What it does

Adds a **PREVENT CVD Score** action button to the chart conditions /
problem-list section, plus a **PREVENT CVD Calculator** entry in the chart
**Plugins** tab. Either one opens a modal pre-filled with the latest matching
values from the patient's chart (sex, age, cholesterol, blood pressure, BMI,
eGFR, diabetes, smoking status, BP/statin treatment). The clinician edits as
needed, clicks **Calculate**, and sees the six PREVENT risk percentages —
10-year and 30-year for ASCVD, Heart Failure, and Total CVD. Each computed
score is written back to the chart as a `laboratory` Observation, so it shows
up in the lab section, the patient timeline, and the PDF chart export.

Optionally, the clinician can save the numeric inputs they typed (TC, HDL,
SBP, BMI, eGFR, HbA1c, UACR) back to the chart as Observations so the next
visit's calculation pre-fills automatically.

By default the calculator runs the AHA PREVENT **base** model. Supplying any
of three optional inputs — **HbA1c** (%), **UACR** (urine albumin/creatinine,
mg/g), or **SDI decile** (1–10) — flips it into the AHA PREVENT **full**
model with the published extended coefficients; missing predictors fall back
to the model's built-in `*_missing` indicators exactly as the upstream R
reference does.

## Problem it solves

The AHA PREVENT equations are the current guideline-recommended way to
estimate cardiovascular risk, but the official calculator lives on an
external website. Without this plugin a clinician has to leave the EHR,
re-key a dozen chart values into a web form, read off the result, and then
manually transcribe the scores back into the note — a slow, error-prone,
copy-by-hand workflow. This plugin brings the calculation in-chart: values
auto-fill from the record, the math runs locally, and the results are saved
as structured Observations in one click — no separate website, no
re-keying, no transcription errors.

## Who it's for

- **Primary care** clinicians performing routine cardiovascular-risk
  assessment and primary-prevention counseling.
- **Cardiology** and **preventive-cardiology** practices that want PREVENT
  scores captured as discrete, trendable chart data.
- **Care-management / population-health** teams who need risk scores stored
  as Observations for cohorting and reporting.
- Any care team that wants AHA PREVENT risk in-chart without a separate
  web calculator.

## Screenshots

The PREVENT calculator modal, pre-filled from the chart:

![PREVENT calculator modal pre-filled from the chart](../screenshots/calculator-modal.png)

After clicking Calculate — the six 10-year and 30-year risk scores:

![Computed PREVENT risk scores](../screenshots/calculator-results.png)

## How to install

```bash
canvas install prevent_calculator --host <your-canvas-host>
```

No post-install configuration is required (see
[Configuration options](#configuration-options)). After installation the
**PREVENT CVD Score** action button appears in the chart conditions section
and the **PREVENT CVD Calculator** entry appears in the chart Plugins tab.

## Configuration options

**None.** This plugin declares no secrets, no environment variables, and no
custom-data namespace (`"secrets": []` in `CANVAS_MANIFEST.json`). It reads
chart data through the Canvas SDK and writes results back as Observation
effects — there is nothing to configure after install.

## How it works

### Prefill — where each field comes from

| Field | Source | Resolver detail |
|---|---|---|
| Sex | `Patient.sex_at_birth` | `M`→Male, `F`→Female. "From patient record". |
| Age | `Patient.birth_date` | "From patient record". |
| Total cholesterol | latest `Observation` LOINC `2093-3` | newest by `effective_datetime`. |
| HDL cholesterol | latest `Observation` LOINC `2085-9` | same pattern. |
| Systolic BP | latest `Observation` `name="blood_pressure"` | split `120/80`, take systolic. |
| BMI | latest height + weight, or `Observation` LOINC `39156-5` | normalizes in/oz/kg/lb/cm/m; direct BMI as fallback. |
| eGFR | latest `Observation` LOINC `98979-8` or `48642-3` | CKD-EPI 2021 first, 2009 fallback. |
| Diabetes | active `Condition` ICD-10 `E10*` / `E11*` / `E13*` | defaults to "No" when none found. |
| Current smoker | `Observation` LOINC `39240-7` / `72166-2` (SNOMED value), else latest **Tobacco** questionnaire response | defaults to "No". |
| On BP treatment | active antihypertensive `Medication` (name/coding hint match) | defaults to "No". |
| On statin therapy | active statin `Medication` (name/coding hint match) | defaults to "No". |

Each pre-filled value carries a provenance tag the UI renders as "From
patient record", "Last value MM/DD/YYYY" (with ⚠ if older than 365 days),
"Condition onset …", "Started …", "Recorded today", or "No record found in
chart — defaulted to No".

### What gets saved

Per Calculate, up to six `laboratory` Observations (`units="%"`) are emitted:

| Observation name | LOINC |
|---|---|
| PREVENT 10-year Total CVD risk | `97506-9` |
| PREVENT 10-year ASCVD risk | `79423-0` |
| PREVENT 10-year Heart Failure risk | *(no LOINC published yet — display name only)* |
| PREVENT 30-year Total CVD risk | *(no LOINC published yet)* |
| PREVENT 30-year ASCVD risk | *(no LOINC published yet)* |
| PREVENT 30-year Heart Failure risk | *(no LOINC published yet)* |

30-year scores are omitted for age > 59 per the AHA spec. Adding a code in
`lib/loinc.py` later retrofits codings without other changes.

## Architecture

```
PreventCalculatorButton (ActionButton, chart conditions section)
PreventCalculatorApp    (patient-scoped Application, Plugins tab)
        │  both launch the same modal in the right chart pane
        ▼
PreventCalculatorAPI ──► StaffSessionAuthMixin (SimpleAPI)
        │
        ▼
canvas_sdk.v1.data ORM (read) ──► PREVENT equations (pure math) ──► Observation effects (write)
```

- `protocols/button.py` — `PreventCalculatorButton`: action button that
  launches the modal.
- `applications/prevent_app.py` — `PreventCalculatorApp`: Plugins-tab entry
  that opens the same modal.
- `protocols/calculator_api.py` — `PreventCalculatorAPI`: SimpleAPI route,
  staff-session authenticated.
- `lib/equations.py` / `lib/equations_full.py` — PREVENT base- and
  full-model risk equations (pure math).
- `lib/chart_data.py` — batched chart-data resolution (single round-trip per
  resource, `prefetch_related` on codings, `entered_in_error` filtered).
- `lib/loinc.py` — LOINC code constants for inputs and outputs.
- `templates/calculator.html` — self-contained modal (inline CSS + JS).

## Routes

All routes are under `/plugin-io/api/prevent_calculator/`.

| Method | Path | Auth | Purpose |
| --- | --- | --- | --- |
| GET  | `/calculator?patient_id=<id>` | Staff session | Render the pre-filled calculator modal |
| POST | `/calculate?patient_id=<id>`  | Staff session | Validate inputs, compute scores, emit Observation effects |

## Source & attribution

The risk equations are a line-for-line port of the
[AHAprevent v1.0.0](https://github.com/AHA-DS-Analytics/PREVENT) R package
(GPL-3, DOI 10.1161/CIRCULATIONAHA.123.067626). Coefficients are unchanged
from the published reference implementation. PREVENT™ is a trademark of the
American Heart Association.

## Local development

```bash
uv sync
uv run pytest --cov=prevent_calculator --cov-report=term-missing
uv run mypy prevent_calculator
```

## License

Released under the [MIT License](../LICENSE).

## Info

*This plugin was developed and contributed by [Vicert](https://vicert.com).*
Contact: engineering@vicert.com
