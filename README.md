# Salesforce Resume Masking Service

Production, Railway-deployable FastAPI service that replaces the broken freelancer redirect on the
recruiting team's **"Generate Masking"** button.

Given a **Job Applicant Id**, it:
1. pulls the resume PDF from Salesforce (`simple-salesforce`, SOQL on `ContentVersion`),
2. **true-redacts** the candidate PII (name / phone / email — glyphs deleted, not a black box),
3. stamps a **centered watermark** on every page,
4. writes the masked copy **back into the same record** as a new `ContentVersion`,
5. returns JSON — **no redirect, no popup**.

It fixes the two client complaints: it masks the **exact** strings supplied (so it never over-masks
experience or academic marks), and it writes back in-record instead of redirecting to a broken app.

---

## Layout

| File | Purpose |
|------|---------|
| `app/mask.py` | Masking core — PyMuPDF true-redact + centered watermark. `mask_pdf` (path) + `mask_pdf_bytes` (in-memory, used by the service). |
| `app/sf_client.py` | Salesforce wrapper: `connect()`, `fetch_resume_pdf(id)`, `upload_masked_pdf(id, bytes, filename)`. All creds from ENV. |
| `app/server.py` | FastAPI app: `POST /mask`, `GET /health`, `detect_pii()` fallback. |
| `app/assets/watermark.png` | (Optional) company logo. If present, stamped centered; else a faint text watermark. |
| `tests/test_server.py` | `/mask` + `/health` with Salesforce mocked, PyMuPDF real. |
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
POST  https://<your-railway-app>.up.railway.app/mask
Content-Type: application/json

{ "job_applicant_id": "{!JobApplicant.Id}" }
```

Optionally include `"mask_strings": [...]` (the exact name/phone/email values from the on-prem resume
parser — preferred, most accurate) and/or `"masking_profile": "<id>"`.

**Security:** Salesforce creds live in the **service env** (Railway secrets), **never** in the button
URL — the button passes only the Job Applicant Id. The service returns:

```json
{ "status": "ok", "masked_content_version_id": "068...", "redacted_regions": 3 }
```

The masked PDF appears as a new file on the record. No redirect, no popup.

---

## PII detection

Preferred: the caller passes `mask_strings` (the on-prem parser's exact output → no missed chars, no
over-masking). Fallback: `detect_pii()` regex-matches email + phone from the PDF text layer. A
`TODO(parser)` hook in `app/server.py` marks where to wire the on-prem parser / OpenRouter LLM for names.
Scanned image-only PDFs have no text layer → `/mask` returns a clear "needs OCR / route to manual" error.
