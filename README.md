# Salesforce Resume Masking Service

Production, Railway-deployable FastAPI service that replaces the broken freelancer redirect on the
recruiting team's **"Generate Masking"** button.

Given a **Job Applicant Id**, it:
1. pulls the resume PDF from Salesforce (`simple-salesforce`, SOQL on `ContentVersion`),
2. **true-redacts** the candidate PII — name / phone / email, and nothing else
   (glyphs deleted, not covered; the region is filled **white**, so the masked copy
   reads as blank space rather than a page of censor bars),
3. stamps a **centered watermark** on every page,
4. writes the masked copy **back into the same record** as a new `ContentVersion`,
5. returns JSON — **no redirect, no popup**.

It fixes the two client complaints: it masks the **exact** strings supplied (so it never over-masks
experience or academic marks), and it writes back in-record instead of redirecting to a broken app.

---

## Changelog

**2026-07-01** — phone-masking leak, SOQL injection, endpoint auth, per-client watermark auto-resolve

- **Phone-masking leak fixed** (digit-aware fallback matcher in `app/mask.py`) — verified against a
  real sample resume (`ORIGINAL.pdf`); masked output saved as `MASKED_fixed_demo.pdf` alongside it.
- **SOQL injection fixed** — Salesforce-Id validation at the boundary (`app/sf_client.py`).
- **`/mask` + `/watermark/upload` now gated** by an optional `MASK_API_KEY` shared secret; `/popup`
  bug fixed (was serving JSON-escaped text instead of real HTML — would never have rendered in the
  Salesforce iframe) and now carries the key automatically.
- **Per-client watermark now auto-resolves.** Confirmed against the live org that
  `SCSCHAMPS__Job_Applicant__c` (the join row the "click Job Id → requirements × eligible
  professionals" view sits on) already carries `SCSCHAMPS__Account__c`. The mask button only needs
  `job_applicant_id` — the service looks up the client itself, fetches that Account's
  `ResumeWatermark` file, falls back to global/text if none uploaded yet.
- 11 pytest + 5 offline checks, all green.

**Setup you still need to do, per client:** one `POST /watermark/upload` with their logo + Account
Id (see "Custom watermark per client" below) — after that it's automatic for every job/applicant
under that client.

---

## Layout

| File | Purpose |
|------|---------|
| `app/pii.py` | PII detection & classification. Strict, precision-first phone detection (a digit run must carry positive evidence of being a phone), so employment date ranges, credential ids, ISO/IEEE/RFC numbers, versions, percentages and PIN codes are never reported as PII. |
| `app/mask.py` | Masking core — PyMuPDF true-redact (white fill) + centered watermark. Per-kind matching: email exact, phone by digit-equivalence, name whole-word only. `mask_pdf` (path) + `mask_pdf_bytes` (in-memory, used by the service). PDF only. |
| `app/docx_convert.py` | `.docx`/`.doc` → PDF via headless LibreOffice (`soffice`, installed in the Dockerfile) — real candidate resumes on this org are legacy Word attachments, not PDFs, so this runs before `app/mask.py` whenever the fetched resume isn't already a PDF. |
| `app/sf_client.py` | Salesforce wrapper: `connect()`, `with_session()` (401-retry wrapper), `fetch_resume_pdf(id)` (checks modern Files + legacy Attachments, on the Job Applicant and its related Contact — returns `(bytes, extension)`), `upload_masked_pdf(id, bytes, filename)`. Creds from ENV or the Postgres-backed override (`register_default_credentials`). |
| `app/server.py` | FastAPI app: `POST /mask`, `POST /mask/batch`, `POST /mask/inline`, `GET /health`, `detect_pii()` fallback. |
| `app/assets/watermark.png` | (Optional) company logo. If present, stamped centered; else a faint text watermark. |
| `app/templates/`, `app/static/` | Jinja2 templates + CSS/JS for `GET /candidate/MaskProfileIndex` — the real Salesforce-embedded masking UI (driven by `MassMaskingController` Apex). |
| `API.md` | Full endpoint + environment-variable reference, including the **current live Railway config** (today: only `DATABASE_URL` is set) and exactly what each endpoint does/doesn't do as a result. |
| `tests/test_server.py` | `/mask`, `/mask/batch`, `/mask/inline` + `/health` with Salesforce mocked, PyMuPDF real. |
| `tests/test_sf_client_multitenant.py` | Multi-client token registry, cache eviction, `with_session()` retry — network mocked. |
| `Dockerfile`, `railway.json`, `Procfile` | Railway deploy. |
| `.env.example` | Every env var, with comments. |

---

## 1. Set the Salesforce creds (ENV — never in code or the URL)

Copy `.env.example` → `.env` (local) or set as Railway service variables (prod).

**Checklist for the integration user (mode A — simplest):**

| Var | What |
|-----|------|
| `SF_USERNAME` | Salesforce integration user login |
| `SF_PASSWORD` | that user's password |
| `SF_SECURITY_TOKEN` | Setup → Reset My Security Token |
| `SF_DOMAIN` | `login` (prod, default) / `test` (sandbox) / a My Domain host |

**Connected App (mode B — optional):** also set `SF_CONSUMER_KEY` + `SF_CONSUMER_SECRET` (the service
switches to the OAuth flow automatically when `SF_CONSUMER_KEY` is present; still needs `SF_USERNAME` +
`SF_PASSWORD`).

If creds are missing, `/mask` returns `{"status":"error","detail":"...not configured..."}` (a clear
message, not a crash) and `GET /health` reports `"salesforce_configured": false`.

---

## 2. Run locally

```bash
cd ~/salesforce-ats
# deps (PyMuPDF already in .venv):
VIRTUAL_ENV=$PWD/.venv uv pip install fastapi uvicorn simple-salesforce python-multipart pytest httpx
# (or: pip install -r requirements.txt)

# load creds + run
set -a; source .env; set +a
.venv/bin/uvicorn app.server:app --reload
```

Then:

```bash
curl localhost:8000/health
curl -X POST localhost:8000/mask \
  -H 'Content-Type: application/json' \
  -d '{"job_applicant_id":"a0X000000000001"}'
# or supply exact strings from the on-prem parser:
#   -d '{"job_applicant_id":"a0X...","mask_strings":["Jane Roe","jane@x.com","+1 555 0100"]}'
```

Run the tests (Salesforce mocked, masking real):

```bash
.venv/bin/python -m pytest tests/test_server.py -q
.venv/bin/python -m tests.test_mask        # offline masking self-check
```

---

## 3. Deploy to Railway

```bash
railway login
railway init                 # in ~/salesforce-ats
# set every secret from the checklist above:
railway variables --set SF_USERNAME=... --set SF_PASSWORD=... \
                  --set SF_SECURITY_TOKEN=... --set SF_DOMAIN=login
railway up                   # builds the Dockerfile, deploys
```

Railway injects `$PORT`; the Dockerfile / `railway.json` start command binds to it. Health check is
`/health`. Drop the logo at `app/assets/watermark.png` before deploy to use the real watermark.

---

## 4. How the Salesforce button calls it

Repoint the existing **"Generate Masking"** button (Apex / Flow / external action) at:

```
POST  https://resume-masker-production.up.railway.app/mask
Content-Type: application/json
X-API-Key: <MASK_API_KEY, if you've set one — see below>

{ "job_applicant_id": "{!SCSCHAMPS__Job_Applicant__c.Id}" }
```

(`SCSCHAMPS__Job_Applicant__c` is the RecruitChamp managed-package object — confirmed against the
live org. Adjust if a different client org uses a different namespace.)

Optionally include `"mask_strings": [...]` (the exact name/phone/email values from the on-prem resume
parser — preferred, most accurate) and/or `"masking_profile": "<id>"`.

**Watermark, set up client-side in Salesforce:** the client configures their logo as a File in Salesforce
(e.g. on the Account, titled `ResumeWatermark`, or wherever your Apex/Flow reads it from). Pass it inline
as base64 in the same call — no extra round-trip to us:

```json
{ "job_applicant_id": "{!JobApplicant.Id}", "watermark_base64": "{!Base64EncodedWatermarkFormula}" }
```

If `watermark_base64` is omitted, the service falls back to fetching a `ResumeWatermark` File from
Salesforce itself via `account_id` (SOQL on `ContentVersion`), then to a plain text watermark.

**Multi-client (multiple Salesforce orgs) via OAuth2 Client Credentials:** if the service is configured
with `SF_CLIENTS_JSON` (see `.env.example`, Auth mode C), also pass `"client_key": "acme"` to select which
registered org/Connected App to use for that call.

**Security:** Salesforce creds live in the **service env** (Railway secrets), **never** in the button
URL — the button passes only the Job Applicant Id (+ optional watermark/client_key). Set `MASK_API_KEY`
(Railway env, see `.env.example`) once this leaves the pilot client — without it, `/mask` and
`/watermark/upload` are open to anyone with the Railway URL. Named Credential in Apex lets you attach the
header without the secret touching Flow/button config. The `/popup` page (browser-embedded) picks up the
key automatically once it's set — nothing else to wire there. The service returns:

```json
{ "status": "ok", "masked_content_version_id": "068...", "redacted_regions": 3 }
```

The masked PDF appears as a new file on the record. No redirect, no popup.

**Bulk masking:** to mask many Job Applicants in one call (e.g. a recruiter selecting a page of
candidates for the same client), use `POST /mask/batch` instead of looping `/mask` per row:

```json
{
  "client_key": "acme",
  "items": [
    { "job_applicant_id": "a0X000000000001" },
    { "job_applicant_id": "a0X000000000002" }
  ]
}
```

`client_key`, `watermark_text`, and `watermark_base64` are shared batch-level defaults; each item can
override `account_id` / `mask_strings` / `watermark_base64` individually. One item's failure (bad Id,
no resume, no PII found) does not abort the rest of the batch — the response reports each item's own
result:

```json
{
  "status": "ok",
  "succeeded": 1,
  "failed": 1,
  "results": [
    { "job_applicant_id": "a0X000000000001", "result": { "status": "ok", "masked_content_version_id": "068..." } },
    { "job_applicant_id": "a0X000000000002", "result": { "status": "error", "detail": "No resume found for Job Applicant 'a0X000000000002'." } }
  ]
}
```

Capped at 200 items per call. All items in a batch share one Salesforce session/org (`client_key`) —
for candidates across different client orgs, send separate batch calls.

---

## 5. Custom watermark per client (RecruitChamp flow)

RecruitChamp's page hierarchy: **Job list (per client/company) → click a Job Id → joined view of
that job's requirements + the eligible professionals matched to it.** The Mask button lives on each
row of that joined view — one candidate, one job, one client, in scope together.

**One-time setup per client** — upload their logo once:

```
POST /watermark/upload
Content-Type: multipart/form-data
X-API-Key: <MASK_API_KEY>

account_id=<client's Salesforce Account Id>
file=<their logo, PNG/JPEG>
```

This stores the image as a Salesforce File titled `ResumeWatermark` on that Account. Re-upload to
swap the logo later — no code change, no redeploy.

**Every mask call after that just works** — the button only needs to send `job_applicant_id`:

```json
{ "job_applicant_id": "{!SCSCHAMPS__Job_Applicant__c.Id}" }
```

The service resolves the client itself: `SCSCHAMPS__Job_Applicant__c.SCSCHAMPS__Account__c` (that
field already sits on the join row — confirmed against the live org's schema) → fetch that Account's
`ResumeWatermark` File → stamp it centered on the masked PDF. If the join view already has the
Account Id handy, pass it explicitly as `"account_id": "..."` and the lookup is skipped. No watermark
uploaded for that client yet → falls back to the global `ResumeWatermark` File (org-wide default) →
falls back to plain text (`watermark_text`, default `"CONFIDENTIAL"`).

```
Job list (per client) ──click Job Id──▶ joined view: job requirements × eligible professionals
                                              │  Mask button on a row
                                              ▼
                                   POST /mask {job_applicant_id}
                                              │
                             resolve client Account (auto, from the join row)
                                              │
                          fetch that Account's ResumeWatermark File (or fall back)
                                              │
                              redact PII + stamp watermark + write back
```

---

## PII detection

Preferred: the caller passes `mask_strings` (the on-prem parser's exact output → no missed chars, no
over-masking). For `/mask` and `/mask/batch` (which have a live Salesforce session), the fallback when
`mask_strings` is omitted is now two-layered: `sf_client.fetch_contact_pii_strings()` pulls the
candidate's structured Name/Phone/Email straight from the related Contact record, merged with
`detect_pii()`'s regex email/phone scan of the PDF text layer. The Contact-field lookup exists because
regex-on-text alone can silently miss real PII — confirmed on real candidate data: resumes built from
Microsoft's built-in "Contoso" template render the phone/email via a Word content control that extracts
as blank or garbled text after DOCX→PDF conversion, even though the correct value sits right there,
structured and correct, on the Contact. `/mask/inline` has no Salesforce session, so it's regex-only —
pass `mask_strings` explicitly there for full accuracy. Scanned image-only PDFs have no text layer →
`/mask` returns a clear "needs OCR / route to manual" error.
