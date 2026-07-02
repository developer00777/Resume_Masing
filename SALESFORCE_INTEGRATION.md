# Salesforce Integration Guide — Resume Masking Service

This doc is for the Salesforce team wiring up the *CV Masking* button. It covers:
what the service does today, how to wire the new Job → Contact → Applicant
flow, and the full API reference.

---

## 1. How to wire the new Contact → Applicant flow

The requirement (*Job record → ATS tab → CV Masking button → pull resume from
the related Contact's Notes & Attachments → mask → save to the Applicant's
Notes & Attachments*) touches a Job/Contact/Applicant object model and a
Notes & Attachments API (Files vs. legacy Attachment) that only your org
knows for certain. Rather than us guessing SOQL/field names, **Apex should
own all of the Salesforce read/write for this flow**, and call our service
purely as a masking transform:

```
CV Masking button (on Job record)
  → Apex:
      1. Resolve the Contact from the Job (however your data model links them)
      2. Read the resume from the Contact's Notes & Attachments (Attachment.Body
         or ContentVersion.VersionData — Apex has native access to either, you
         don't need to tell us which one your org uses)
      3. Base64-encode it, POST to POST /mask/inline { resume_base64, mask_strings? }
      4. Take masked_pdf_base64 from the response
      5. Write it to the Applicant's Notes & Attachments (same API you read with)
```

`/mask/inline` (see [§4](#4b-post-maskinline--mask-bytes-directly-no-salesforce-io))
takes a base64 PDF in and returns a base64 masked PDF out — it never calls
Salesforce itself, so there's no object/field API name for us to get wrong,
and no Connected App / auth-to-your-org needed for this flow at all (still
gated by `X-API-Key` if you've set one, same as every other endpoint).

**What we still need from you**, only if you want us doing the PII detection
too (recommended — see `mask_strings` below):
- Nothing else. If you already have the candidate's name/phone/email as
  structured fields on the Contact, pass them as `mask_strings` in the
  request for the most accurate redaction — otherwise we fall back to a
  regex scan of the PDF text (email + phone patterns), which is good but not
  as precise as exact values from your data.

If instead you'd prefer **we** do the Salesforce I/O (i.e. Apex just passes an
Id and we fetch/write, like `/mask` does today for Job Applicant), that's also
possible — but then we need to know your Job/Contact/Applicant object and
field API names, and whether "Notes & Attachments" means modern Files
(`ContentDocumentLink`/`ContentVersion`) or the legacy `Attachment` object.
Tell us and we'll follow up with the specific questions; the inline approach
above avoids that whole conversation, so we'd lead with it unless there's a
reason Apex can't own the Notes & Attachments read/write itself.

---

## 2. Base URL & Auth

```
Base URL: https://<your-railway-app>.up.railway.app
```

Every write endpoint (`/mask`, `/mask/batch`, `/mask/inline`, `/watermark/upload`, `/clients/self-register`) requires a shared-secret header once `MASK_API_KEY` is set on our side (`/admin/clients` uses a separate key, see §3b):

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
# {"status":"ok","salesforce_configured":true,"client_keys":["00D5f000000ABCDEAU"],"registry_backend":"db+env"}
```

`registry_backend` tells you whether dynamic org registration (§3b below) is available on this deployment: `"db+env"` means yes, `"env-only"` means orgs can only be added via a static config change on our side.

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
  - Read: whatever object holds the source resume (today: `SCSCHAMPS__Job_Applicant__c`; not needed at all if you use `/mask/inline`, §1/§4b)

For Option C, your Apex/Flow passes a `client_key` in the request body so the
service knows which org's credentials to use. **By convention, `client_key` is
your org's own Salesforce Organization Id** (Setup → Company Information, or
in Apex: `UserInfo.getOrganizationId()`) — not a name you have to invent or
that we have to hand you. This means every org's Apex/Flow config looks
identical; nothing org-specific is hardcoded anywhere on your side.

### 3a. Getting your Connected App credentials to us

Once you have the Consumer Key/Secret from step above, there are two ways to
register them — pick whichever fits your process. Both require `registry_backend: "db+env"`
on `GET /health` (see §2); if it says `"env-only"`, dynamic registration isn't
available yet on this deployment and you should send us the credentials
directly instead.

**Self-service (recommended — no round-trip with us):**
```
POST /clients/self-register
X-API-Key: <the same key used for /mask>
Content-Type: application/json

{
  "client_key": "00D5f000000ABCDEAU",
  "client_id": "<Consumer Key>",
  "client_secret": "<Consumer Secret>",
  "token_url": "https://yourorg.my.salesforce.com/services/oauth2/token",
  "instance_url": "https://yourorg.my.salesforce.com"
}
```
Or use the form on `GET /popup` ("Connect Your Org") if you're already
embedding that page — same effect, no API call needed on your end.

This is **create-only**: if `client_key` is already registered, you'll get
back `{"status": "error", "detail": "This org is already registered. Contact us to rotate credentials."}`
instead of silently overwriting a working connection. If you need to rotate
credentials for an org that's already connected, ask us to do it via the
admin route (§3b) — self-service intentionally can't overwrite.

**Manual (we do it for you):** send us the four fields above plus your
Organization Id, and we'll register it via the admin API.

### 3b. Admin API (for us, or your own internal tooling)

```
GET    /admin/clients                     — list all registered orgs (no secrets returned)
PUT    /admin/clients/{client_key}        — register or rotate credentials for an org
DELETE /admin/clients/{client_key}        — deregister an org
```
All three require `X-Admin-API-Key` — a separate, higher-privilege secret from
the `X-API-Key` used everywhere else, since these routes can add/rotate/remove
any org's access. We hold this key; you generally won't need it unless you're
managing your own multi-tenant deployment of this service.

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
  "client_key": "00D5f000000ABCDEAU"
}
```

| Field | Required | Notes |
|---|---|---|
| `job_applicant_id` | yes | Salesforce Id of the source record (today: Job Applicant). 15 or 18 chars. |
| `account_id` | no | Client Account Id, for watermark resolution. Auto-resolved from the Job Applicant if omitted. |
| `mask_strings` | no | Exact PII strings to redact. If omitted, the service runs a regex fallback (email/phone) over the extracted PDF text — less accurate than passing exact values from your data. |
| `watermark_text` | no | Fallback text watermark if no image is found. Default `"CONFIDENTIAL"`. |
| `watermark_base64` | no | Inline watermark image — skips an extra Salesforce round-trip. See §5. |
| `client_key` | conditional | Required only if we're using multi-org auth (Option C above) — your org's Organization Id (`UserInfo.getOrganizationId()`). Omit for Option A/B. Must be registered first, see §3a. |

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
  "client_key": "00D5f000000ABCDEAU",
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

### `POST /mask/inline` — mask bytes directly, no Salesforce I/O {#4b-post-maskinline--mask-bytes-directly-no-salesforce-io}

**This is the recommended endpoint for the CV Masking button** (§1 above) —
Apex fetches the resume and writes the result, we only transform bytes.

**Request:**
```json
{
  "resume_base64": "<base64 PDF, read from the Contact's Notes & Attachments>",
  "mask_strings": ["John Doe", "+91 98765 43210", "john.doe@example.com"],
  "watermark_text": "CONFIDENTIAL",
  "watermark_base64": "<base64 PNG/JPEG, optional>"
}
```

| Field | Required | Notes |
|---|---|---|
| `resume_base64` | yes | The source PDF, base64-encoded. |
| `mask_strings` | no | Exact PII strings to redact — pass the candidate's name/phone/email from your Contact fields for the most accurate result. Falls back to a regex email/phone scan if omitted. |
| `watermark_text` | no | Fallback text watermark. Default `"CONFIDENTIAL"`. |
| `watermark_base64` | no | Client logo image, base64. |

**Response:**
```json
{
  "status": "ok",
  "masked_pdf_base64": "<base64 masked PDF — write this to the Applicant's Notes & Attachments>",
  "redacted_regions": 3,
  "watermark_used": "image:inline_base64",
  "detail": null
}
```

No `job_applicant_id`, `account_id`, or `client_key` — this endpoint never
calls Salesforce, so none of that is relevant. Same `X-API-Key` auth as
every other endpoint applies if `MASK_API_KEY` is set.

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

### `POST /clients/self-register` — connect a new org (see §3a)

Auth: `X-API-Key` (same key as `/mask` — **not** `X-Admin-API-Key`).

**Request:**
```json
{
  "client_key": "00D5f000000ABCDEAU",
  "client_id": "<Connected App Consumer Key>",
  "client_secret": "<Connected App Consumer Secret>",
  "token_url": "https://yourorg.my.salesforce.com/services/oauth2/token",
  "instance_url": "https://yourorg.my.salesforce.com"
}
```

**Response:**
```json
{ "status": "ok", "detail": "Registered '00D5f000000ABCDEAU'." }
```

Create-only — a second call for an already-registered `client_key` returns
`{"status": "error", "detail": "This org is already registered. Contact us to rotate credentials."}`
rather than overwriting. Returns `{"status": "error", "detail": "..."}` (still HTTP 200)
if this deployment doesn't have Postgres provisioned (`registry_backend: "env-only"`
on `GET /health`) — in that case, send us your credentials directly instead (§3a).

### `GET/PUT/DELETE /admin/clients` — admin registry management

Auth: `X-Admin-API-Key` (separate, higher-privilege secret — see §3b). We hold
this key; you generally won't need it unless managing your own deployment.

```
GET /admin/clients
→ {"status":"ok","clients":[{"client_key":"00D5f000000ABCDEAU","client_id":"...","token_url":"...","instance_url":"...","source":"db"}]}
```
`source` is `"db"` (dynamically registered) or `"env"` (from the static
`SF_CLIENTS_JSON` config) — `client_secret` is never included in any response.

```
PUT /admin/clients/{client_key}
{ "client_id": "...", "client_secret": "...", "token_url": "...", "instance_url": "..." }
```
Registers or rotates credentials for an org — this route (unlike self-register)
can overwrite an existing entry, so it's the one to use for credential rotation.

```
DELETE /admin/clients/{client_key}
```
Deregisters an org. Only affects dynamically-registered (`source: "db"`)
entries — an env-only entry needs `SF_CLIENTS_JSON` edited on our side instead.

### `GET /popup` — embeddable UI (optional)

A ready-made HTML page — "Connect Your Org" self-service form (§3a), watermark
upload form, and Mask button — you can drop into a Lightning Component / iframe
if you don't want to build your own UI. Authenticates itself automatically
once `MASK_API_KEY` is set — nothing extra to wire. The "Connect Your Org"
form currently requires manually typing your Organization Id; if you embed
this page via Visualforce/Lightning, that field can be pre-filled from
`{!$Organization.Id}` — ask us if you want that wired up.

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
| `Unknown client_key '...'. Configured: [...]` | Wrong/missing `client_key`, or your org hasn't been registered yet | Register via `/clients/self-register` (§3a), or check the Org Id against what your Flow is sending |
| `watermark_base64 is not valid base64.` | Malformed inline watermark | Check the formula field encoding |
| `This org is already registered. Contact us to rotate credentials.` | `/clients/self-register` called twice for the same `client_key` | Ask us to rotate via `/admin/clients` instead — self-service can't overwrite |
| `Postgres isn't provisioned on this deployment (DATABASE_URL unset)...` | `/clients/self-register` or `/admin/clients` called on a deployment with `registry_backend: "env-only"` | Send us your credentials directly instead of self-registering |
| `Missing or invalid X-Admin-API-Key.` / `Admin API not configured...` | Wrong or missing admin key on `/admin/clients` | This route isn't for Apex/Flow — it's for us; you shouldn't normally be calling it |

---

## 7. Security notes

- Salesforce credentials live only in our service's environment (Railway secrets) or, for dynamically-registered orgs, encrypted at rest in our database — never in Flow/button config, never in code.
- `/mask`, `/mask/batch`, `/mask/inline`, `/watermark/upload`, `/clients/self-register` require `X-API-Key` once we set `MASK_API_KEY` — ask us for the key and use a Named Credential in Apex so it never appears in visible metadata.
- `/admin/clients` requires a separate, higher-privilege `X-Admin-API-Key` that we hold — it can add/rotate/remove any org's credentials, so it's deliberately not the same key handed to Apex/Flow.
- The service validates every Salesforce Id it receives against Salesforce's Id format before using it in SOQL (defense against SOQL injection from a malformed/malicious button parameter).
- We do not log or persist resume content — the PDF is fetched, masked in memory, uploaded, and discarded per request.
