# Salesforce Integration Guide — Resume Masking Service

This doc is for the Salesforce team wiring up the *CV Masking* button. It covers:
what the service does today, what we need confirmed before wiring the new
Job → Contact → Applicant flow, and exactly how to call the API once that's settled.

---

## 1. Open items — please confirm before we finalize the Apex/Flow

The requirement described (*Job record → ATS tab → CV Masking button → pull
resume from the related Contact's Notes & Attachments → mask → save to the
Applicant's Notes & Attachments*) is a **different object/field model** than
what this service currently reads/writes (see [§4](#4-current-behavior-live-today)).
We don't want to hardcode field/object API names we're guessing at, so we need:

| # | What we need | Why |
|---|---------------------------------------------------------------------|-----|
| 1 | API name of the **Job** object (e.g. `SCSCHAMPS__Job__c`?) | To know what record the button lives on and what Id the button passes us. |
| 2 | API name of the **Applicant** object, and the relationship field linking it to Job and to Contact | To know where to write the masked file and how to resolve the candidate. |
| 3 | Confirm: does "Notes & Attachments" mean modern **Files** (`ContentDocumentLink`/`ContentVersion`), or legacy **Attachment** objects (`Attachment.Body`/`ParentId`)? | These are different Salesforce APIs. Most orgs created after ~2017 use Files even when the page layout still says "Notes & Attachments," but some legacy orgs still use `Attachment`. This determines which SOQL/API we implement. |
| 4 | How is the Contact related to the Job/Applicant? Direct lookup on Applicant, or via a junction? | To resolve "the candidate's resume" — i.e. which Contact record to read from. |
| 5 | Where should the masked file be named/titled? Same title as source, or a fixed convention (e.g. `Masked_<name>`)? | Affects `Title`/`PathOnClient` on the new File/Attachment we create. |
| 6 | Multiple resumes on one Contact — pick most recent, or is there a flag/field marking "the" resume? | Contact could have several attachments (old CVs, cover letters, ID docs). |
| 7 | Which org(s)/client(s) does this apply to — same `SF_CLIENTS_JSON` multi-tenant setup as today, or a single org? | Determines whether the button needs to pass `client_key`. |

Once we have these, we'll add a `fetch_resume_from_contact()` /
`upload_masked_to_applicant()` pair to the service (mirroring the existing
`fetch_resume_pdf()` / `upload_masked_pdf()` in `app/sf_client.py`) and this
doc gets updated with the final request shape. **Until then, the API below
reflects what's actually live** — if you want to start integrating now, wire
the button to the current `/mask` contract and we'll swap the field names
under the hood without changing your Apex once the above is confirmed.

---

## 2. Base URL & Auth

```
Base URL: https://<your-railway-app>.up.railway.app
```

Every write endpoint (`/mask`, `/mask/batch`, `/watermark/upload`) requires a shared-secret header once `MASK_API_KEY` is set on our side:

```
X-API-Key: <the key we give you>
```

Get this from us once — it's a Railway env var, never put it in Flow/button config directly. If you're calling from Apex, use a **Named Credential** so the key never touches visible metadata:

```apex
HttpRequest req = new HttpRequest();
req.setEndpoint('callout:Resume_Masking_Service/mask');
req.setMethod('POST');
req.setHeader('Content-Type', 'application/json');
// Named Credential injects X-API-Key automatically if configured with a custom header
```

`GET /health` is unauthenticated (used for Railway's own healthcheck and for you to sanity-check the deploy):

```bash
curl https://<your-railway-app>.up.railway.app/health
# {"status":"ok","salesforce_configured":true,"client_keys":["acme","beta_corp"]}
```

---

## 3. What we need from you (setup checklist)

To connect this service to your org, we need **one** of the following. Option C is recommended if you have multiple client orgs (e.g. one org per RecruitChamp customer).

**Option A — Integration User (simplest, single org)**
- A dedicated Salesforce integration user (not a personal login)
- Username + password + security token (Setup → Reset My Security Token)

**Option B — Connected App, username-password flow (single org)**
- Setup → App Manager → New Connected App
- Consumer Key + Consumer Secret
- Still needs the integration user's username/password

**Option C — Connected App, Client Credentials flow (recommended for multi-org)**
- One Connected App **per org/client**, configured for the **Client Credentials** OAuth grant
- Each Connected App needs a dedicated **"Run As" execution user** (Setup → App Manager → your Connected App → Edit Policies → Run As) — fully headless, no username/password stored on our side at all
- That execution user's profile needs **API Only** + these object permissions:
  - Read: `ContentDocumentLink`, `ContentVersion`
  - Create: `ContentVersion`
  - Read: whatever object holds the source resume (today: `SCSCHAMPS__Job_Applicant__c`; pending your answer to open item #1–4 above)
- Send us per org: `client_id`, `client_secret`, `token_url` (`https://<org>.my.salesforce.com/services/oauth2/token`), `instance_url`

For Option C, your Apex/Flow passes a `client_key` (an arbitrary short name we agree on per org, e.g. `"acme"`) in the request body so the service knows which org's credentials to use.

---

## 4. Current behavior (live today)

This is what's implemented and tested right now, before the Contact/Applicant changes described in §1.

### Flow

RecruitChamp page hierarchy: **Job list → click a Job Id → joined view of that
job's requirements + eligible professionals.** The Mask button lives on each
row of that joined view.

```
join-view "Mask" button
  → POST /mask { job_applicant_id, account_id?, mask_strings?, watermark_base64?, client_key? }
      1. Fetch resume PDF — currently from ContentVersion linked via
         ContentDocumentLink to SCSCHAMPS__Job_Applicant__c (the join row itself,
         not a separate Contact record)
      2. Resolve the client Account (for per-client watermark) — from account_id
         if passed, else auto-resolved from SCSCHAMPS__Job_Applicant__c.SCSCHAMPS__Account__c
      3. Resolve watermark image — inline base64 > Salesforce File lookup > plain text
      4. True-redact PII text (name/phone/email) + overlay centered watermark
      5. Upload masked PDF — currently back onto the SAME SCSCHAMPS__Job_Applicant__c
         record (not a separate Applicant object)
      6. Return { status, masked_content_version_id, redacted_regions, watermark_used }
```

### `POST /mask` — mask one candidate's resume

**Request:**
```json
{
  "job_applicant_id": "a0X000000000001",
  "account_id": "001XXXXXXXXXXXXAAA",
  "mask_strings": ["John Doe", "+91 98765 43210", "john.doe@example.com"],
  "watermark_text": "CONFIDENTIAL",
  "watermark_base64": "<base64 PNG/JPEG, optional>",
  "client_key": "acme"
}
```

| Field | Required | Notes |
|---|---|---|
| `job_applicant_id` | yes | Salesforce Id of the source record (today: Job Applicant). 15 or 18 chars. |
| `account_id` | no | Client Account Id, for watermark resolution. Auto-resolved from the Job Applicant if omitted. |
| `mask_strings` | no | Exact PII strings to redact. If omitted, the service runs a regex fallback (email/phone) over the extracted PDF text — less accurate than passing exact values from your data. |
| `watermark_text` | no | Fallback text watermark if no image is found. Default `"CONFIDENTIAL"`. |
| `watermark_base64` | no | Inline watermark image — skips an extra Salesforce round-trip. See §5. |
| `client_key` | conditional | Required only if we're using multi-org auth (Option C above). Omit for Option A/B. |

**Response:**
```json
{
  "status": "ok",
  "masked_content_version_id": "068000000000001AAA",
  "redacted_regions": 3,
  "watermark_used": "image:account_001XXXXXXXXXXXXAAA",
  "detail": null
}
```

`status: "error"` responses always return HTTP 200 with a human-readable `detail` — check `status`, not the HTTP status code, for success/failure (this keeps Flow's error handling simple — no need to branch on HTTP codes).

### `POST /mask/batch` — mask many candidates in one call

Same shape, but for bulk actions (e.g. a recruiter multi-selecting rows).
One shared Salesforce session per batch — send items for the **same** `client_key`/org
per call; split into multiple batch calls for different orgs.

```json
{
  "client_key": "acme",
  "watermark_text": "CONFIDENTIAL",
  "items": [
    { "job_applicant_id": "a0X000000000001" },
    { "job_applicant_id": "a0X000000000002", "account_id": "001OVERRIDE0001AAA" }
  ]
}
```

Max 200 items per call. One item's failure doesn't abort the batch:

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

### `POST /watermark/upload` — set a client's logo (one-time per client)

```
Content-Type: multipart/form-data
account_id=<client's Salesforce Account Id>
file=<PNG/JPEG>
client_key=<optional, multi-org only>
```

Stores the image as a File titled `ResumeWatermark` on that Account. Every
subsequent `/mask` call for that client picks it up automatically — no code
change, no redeploy. Re-upload to swap the logo.

### `GET /popup` — embeddable UI (optional)

A ready-made HTML page (watermark upload form + Mask button) you can drop
into a Lightning Component / iframe if you don't want to build your own UI.
Authenticates itself automatically once `MASK_API_KEY` is set — nothing extra
to wire.

---

## 5. Watermark — how the per-client logo gets to us

Two options, pick one per client:

1. **Inline (preferred, no extra round-trip):** your Apex/Flow reads the
   client's logo File and base64-encodes it directly into the `/mask` call's
   `watermark_base64` field.
2. **Pre-uploaded:** call `POST /watermark/upload` once per client (see above);
   every later `/mask` call for that Account auto-resolves it — you only need
   to send `job_applicant_id`.

If neither is set, we fall back to a plain text watermark (`watermark_text`,
default `"CONFIDENTIAL"`).

---

## 6. Error handling reference

All error responses share this shape (HTTP 200, `status: "error"`, `detail` is
a plain-English message safe to surface in a Flow error toast):

| `detail` pattern | Likely cause | Fix |
|---|---|---|
| `Salesforce credentials not configured.` | Service-side env vars missing | Contact us — this is our config, not yours |
| `Invalid Salesforce Id for job_applicant_id: ...` | Malformed/wrong-length Id passed | Check the button's merge field |
| `No resume found for Job Applicant '...'.` | No PDF attached to that record | Confirm the resume was actually uploaded there |
| `No PII strings to mask.` | Neither `mask_strings` nor the regex fallback found anything | Pass explicit `mask_strings` from your parsed candidate data |
| `Unknown client_key '...'. Configured: [...]` | Wrong/missing `client_key` for multi-org setup | Check the key we gave you against what your Flow is sending |
| `watermark_base64 is not valid base64.` | Malformed inline watermark | Check the formula field encoding |

---

## 7. Security notes

- Salesforce credentials live only in our service's environment (Railway secrets) — never in Flow/button config, never in code.
- `/mask`, `/mask/batch`, `/watermark/upload` require `X-API-Key` once we set `MASK_API_KEY` — ask us for the key and use a Named Credential in Apex so it never appears in visible metadata.
- The service validates every Salesforce Id it receives against Salesforce's Id format before using it in SOQL (defense against SOQL injection from a malformed/malicious button parameter).
- We do not log or persist resume content — the PDF is fetched, masked in memory, uploaded, and discarded per request.
