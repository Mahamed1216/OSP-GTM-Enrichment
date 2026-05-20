# Phase 8a — Smart CSV column mapping

## Why

`scripts/ingest_leads.py` requires exact snake_case headers
(`first_name`, `last_name`, `email`). Real SDR exports — Apollo, ZoomInfo,
LinkedIn Sales Navigator, Lusha — use their own conventions ("First Name",
"Person Linkedin Url", "Company Name for Emails"). The current behavior on
an Apollo CSV is a hard "Missing required columns" failure.

Phase 8a adds a normalization + alias layer at the ingest boundary so the
common SDR-tool exports auto-detect into the existing canonical schema. The
DB schema does not change. The CLI keeps working unchanged.

## Alias coverage

Aliases run **after** normalization. Normalization rules:

- Lowercase
- Strip surrounding whitespace
- Replace spaces and hyphens with underscores
- Collapse repeated underscores

So `First Name`, `first name`, `First_Name`, `FIRST-NAME` all normalize to
`first_name` and match the canonical field directly. The aliases below are
the additional vendor-specific variants we recognize.

| Canonical field | Required? | Normalized aliases recognized |
|---|---|---|
| `first_name` | yes | `first_name`, `firstname`, `given_name`, `fname` |
| `last_name` | yes | `last_name`, `lastname`, `surname`, `lname`, `family_name` |
| `email` | yes | `email`, `email_address`, `work_email`, `business_email` |
| `title` | no | `title`, `job_title`, `position`, `role` |
| `company` | no | `company`, `company_name`, `organization`, `employer`, `company_name_for_emails` |
| `company_domain` | no | `company_domain`, `website`, `domain`, `company_website` |
| `industry` | no | `industry`, `company_industry`, `sector` |
| `linkedin_url` | no | `linkedin_url`, `linkedin`, `linkedin_profile`, `person_linkedin_url`, `linkedin_profile_url` |
| `company_linkedin_url` | no | `company_linkedin_url`, `company_linkedin`, `organization_linkedin_url` |

### Apollo column → canonical (sample)

| Apollo column | Normalized | Canonical |
|---|---|---|
| First Name | first_name | first_name |
| Last Name | last_name | last_name |
| Title | title | title |
| Company Name | company_name | company |
| Email | email | email |
| Email Status | email_status | *unmapped* (next iteration) |
| Primary Email Source | primary_email_source | *unmapped* |
| Person Linkedin Url | person_linkedin_url | linkedin_url |
| Company Linkedin Url | company_linkedin_url | company_linkedin_url |
| Industry | industry | industry |
| Website | website | company_domain |
| Company Name for Emails | company_name_for_emails | *collision — see below* |

### Tie-breaker — duplicate matches

Apollo emits both `Company Name` and `Company Name for Emails`. Both
normalize to aliases of the same canonical field (`company`). The detector
picks the **first CSV column whose normalized form matches any alias** for
that field. Later collisions are left unmapped; the UI surfaces them so the
user can manually re-route or skip.

### What is NOT recognized

Anything outside the alias table is left unmapped and silently ignored at
ingest. Notable examples in Apollo exports: `Email Status`, `Primary Email
Source`, `Seniority`, `Stage`, `Lists`, `Last Contacted`, `Account Owner`,
`# Employees`. Some of these (e.g. `Email Status` ↔ existing
`email_verification_status` field) are reasonable next-iteration adds; they
are deliberately out of scope here to keep the cache/upsert logic untouched.

## UI mapping flow

`app/pages/4_run_pipeline.py` — section "1) Ingest CSV":

```
┌─ Upload CSV ─────────────────────────────────────────────────┐
│ [ file picker ]                                              │
└──────────────────────────────────────────────────────────────┘

After upload:

┌─ Preview (first 5 rows) ─────────────────────────────────────┐
│ <existing dataframe preview>                                 │
└──────────────────────────────────────────────────────────────┘

┌─ Column mapping ─────────────────────────────────────────────┐
│ Source column            →  Mapped to                        │
│ -------------------------    ----------------------------    │
│ First Name                   [ first_name        ▼ ]         │
│ Last Name                    [ last_name         ▼ ]         │
│ Email                        [ email             ▼ ]         │
│ Title                        [ title             ▼ ]         │
│ Company Name                 [ company           ▼ ]         │
│ Person Linkedin Url          [ linkedin_url      ▼ ]         │
│ Website                      [ company_domain    ▼ ]         │
│ Email Status                 [ — Skip —          ▼ ]         │
│ Primary Email Source         [ — Skip —          ▼ ]         │
│ Company Name for Emails      [ — Skip —          ▼ ]         │
└──────────────────────────────────────────────────────────────┘

┌─ Required fields ────────────────────────────────────────────┐
│ ✓ first_name      mapped from "First Name"                   │
│ ✓ last_name       mapped from "Last Name"                    │
│ ✓ email           mapped from "Email"                        │
└──────────────────────────────────────────────────────────────┘

[ Ingest ]   ← disabled until all three required fields show ✓
```

If a required field is missing the validation panel shows it red and lists
which canonical field has no source column.

### Session state

Manual overrides persist in
`st.session_state["ingest_mapping_overrides"][<uploaded_filename>]` so a
button click (which triggers a Streamlit script rerun) does not lose the
user's choices. The state is keyed by filename so re-uploading a different
file starts fresh.

### Mapping JSON contract

When Ingest is clicked, the UI writes a sibling `mapping.json` to the same
tempdir as the uploaded CSV, then invokes
`scripts/ingest_leads.py <csv> --mapping <mapping.json>`.

Contract:

```json
{
  "first_name": "First Name",
  "last_name": "Last Name",
  "email": "Email",
  "title": "Title",
  "company": "Company Name",
  "linkedin_url": "Person Linkedin Url",
  "company_domain": "Website"
}
```

Keys are canonical field names. Values are the exact source CSV column
header strings. Canonical fields the user chose to skip are simply absent
from the mapping. The script reverses this map at row-read time.

## CLI behavior

- `python scripts/ingest_leads.py file.csv` — auto-detects mapping (new
  behavior). Snake-case CSVs continue to work unchanged.
- `python scripts/ingest_leads.py file.csv --mapping map.json` — uses the
  explicit mapping (used by the UI to honor manual overrides).
- Validation failure (missing required field after detection) exits with
  code 2 and a clear error listing which canonical fields are unmapped.

## Quality bar

- Apollo-shaped CSV ingests end-to-end with one click after auto-detect.
- Existing `data/sample_leads.csv` (snake_case) shows all-green mapping
  immediately and ingests with zero clicks beyond the existing Ingest
  button.
- Unrecognized columns are silently ignored (not stored).
- All 104 existing tests continue to pass.

## Out of scope (next iterations)

- Mapping Apollo's `Email Status` → existing `email_verification_status`
  field. Would let us skip re-verification for already-verified leads.
- Saved per-vendor presets ("Apollo preset", "ZoomInfo preset") for reuse
  across sessions.
- Fuzzy / embedding-based matching beyond the explicit alias table.
