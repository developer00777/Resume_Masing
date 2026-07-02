"""Resume-masking service — FastAPI.

Custom watermark IMAGE per client, configured on the Salesforce side. Salesforce
can hand it to us two ways:
  1. Inline: the caller (Apex/Flow) reads the client's watermark File and sends
     its base64 content directly in the /mask request (watermark_base64) — preferred,
     no extra round-trip.
  2. Stored: a File titled 'ResumeWatermark' on the Account record, which we fetch
     ourselves via SOQL (account_id) if watermark_base64 isn't supplied.
Falls back to a text watermark if neither is present.

Flow (RecruitChamp: Job list -> click Job Id -> joined view of that job's requirements
+ eligible professionals -> Mask button on each row of that joined view):
  join-view Mask button → POST /mask {job_applicant_id, account_id?, mask_strings, watermark_base64?}
    1. Fetch resume PDF from Salesforce
    2. Resolve the client Account: use account_id if the button passed one, else
       look it up from Job Applicant's own Account lookup (SCSCHAMPS__Account__c) —
       the join view already has both the job and the client in scope, so callers
       don't have to also pass account_id explicitly.
    3. Resolve the watermark image: use watermark_base64 if the caller sent it inline
       (no extra round-trip), else fetch that client's watermark image from Salesforce
       (by the resolved Account), else fall back to plain text.
    4. True-redact PII strings + overlay centered watermark image
    5. Upload masked PDF back to Salesforce
    6. Return {status, masked_content_version_id}

  Per-client watermark upload (once per client, reused for every job/applicant
  under that client; alternative to sending watermark_base64 inline each call):
    POST /watermark/upload {account_id, image_file} → stores as 'ResumeWatermark'
    on the client's Account record.

  Bulk masking (same client_key, many Job Applicants in one call):
    POST /mask/batch {client_key?, items: [{job_applicant_id, ...}]} → runs the
    same per-item flow as /mask for each item against one shared Salesforce
    session, so one bad item returns its own error without failing the batch.

  Inline masking (no Salesforce I/O — for callers that fetch/write themselves,
  e.g. Apex reading a Contact's Notes & Attachments and writing to a different
  object's Notes & Attachments; we don't need to know that object model at all):
    POST /mask/inline {resume_base64, mask_strings?, watermark_base64?} →
    {masked_pdf_base64, ...}. Pure bytes-in/bytes-out, no client_key, no
    Salesforce connection made.

Every Salesforce operation in /mask and /mask/batch runs through
sf_client.with_session(), which retries once with a forced-fresh token if the
session turns out to be expired mid-request (see sf_client.py's with_session
docstring for why the cached-token TTL is a conservative guess, not a guarantee).
/mask/inline never touches Salesforce, so this doesn't apply to it.

Auth: this is a public Railway URL (same pattern as RecruitChamp/LakeStream's other
Salesforce-facing services), so /mask and /watermark/upload are gated by a shared
secret (MASK_API_KEY, Railway env) checked via the X-API-Key header — otherwise
anyone with the URL could trigger a masking job or overwrite a client's watermark.
Off (unenforced) when MASK_API_KEY isn't set, so existing deploys aren't broken
mid-rollout; /health stays open for Railway's healthcheck.
"""
from __future__ import annotations

import base64
import binascii
import os
import re

from fastapi import Depends, FastAPI, UploadFile, File, Form, Header, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from app import mask, sf_client

app = FastAPI(title="Salesforce Resume Masking Service", version="1.3.0")


def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    expected = os.environ.get("MASK_API_KEY", "").strip()
    if not expected:
        return  # not configured yet -> unenforced (see module docstring)
    if x_api_key != expected:
        raise HTTPException(status_code=401, detail="Missing or invalid X-API-Key.")


# --- PII detection fallback ---
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
# Same-line separators only ([ ()\-.], not \s) -- \s also matches newlines, which let
# this span unrelated stacked lines (e.g. education years, date ranges) and get
# fuzzy-matched as a "phone number" by mask._phone_rects, blacking out real content.
_PHONE_RE = re.compile(r"\+?\d[\d ().\-]{8,}\d")


def detect_pii(pdf_bytes: bytes) -> list[str]:
    import fitz
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    text = "".join(page.get_text() for page in doc)
    doc.close()
    found: list[str] = []
    found += _EMAIL_RE.findall(text)
    found += [m.strip() for m in _PHONE_RE.findall(text)]
    seen: set[str] = set()
    out: list[str] = []
    for s in found:
        s = s.strip()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


# --- API Models ---
class MaskRequest(BaseModel):
    job_applicant_id: str = Field(..., description="Salesforce Job Applicant record Id.")
    account_id: str | None = Field(default=None, description="Salesforce Account Id (for per-client watermark).")
    mask_strings: list[str] | None = Field(default=None, description="Exact PII strings to redact.")
    watermark_text: str = Field(default="", description="Fallback text if no watermark image found.")
    watermark_base64: str | None = Field(
        default=None,
        description="Base64-encoded PNG/JPEG watermark image, set up client-side in Salesforce and "
                    "sent inline. Takes priority over the Salesforce-stored 'ResumeWatermark' File "
                    "lookup (account_id) when both are present.")
    client_key: str | None = Field(
        default=None,
        description="Which registered Salesforce client/org (SF_CLIENTS_JSON) to use for this call. "
                    "Required when the service is configured for multiple clients via OAuth2 Client "
                    "Credentials; omit for a single-tenant username/password deployment.")


class MaskResponse(BaseModel):
    status: str
    masked_content_version_id: str | None = None
    redacted_regions: int | None = None
    watermark_used: str = "none"
    detail: str | None = None


class WatermarkUploadResponse(BaseModel):
    status: str
    content_version_id: str | None = None
    detail: str | None = None


class BatchMaskItem(BaseModel):
    job_applicant_id: str = Field(..., description="Salesforce Job Applicant record Id.")
    account_id: str | None = Field(default=None, description="Salesforce Account Id (for per-client watermark).")
    mask_strings: list[str] | None = Field(default=None, description="Exact PII strings to redact.")
    watermark_base64: str | None = Field(
        default=None,
        description="Per-item watermark override. Falls back to the batch-level watermark_base64, "
                    "then to the Salesforce-stored lookup, then to text — same priority as /mask.")


class BatchMaskRequest(BaseModel):
    items: list[BatchMaskItem] = Field(..., min_length=1, max_length=200,
                                       description="Job Applicants to mask in this batch.")
    client_key: str | None = Field(
        default=None,
        description="Which registered Salesforce client/org (SF_CLIENTS_JSON) to use for the whole "
                    "batch — one shared session, reused across all items.")
    watermark_text: str = Field(default="", description="Fallback text watermark, shared by all items.")
    watermark_base64: str | None = Field(
        default=None,
        description="Batch-level watermark image, used for any item that doesn't set its own.")


class BatchMaskResult(BaseModel):
    job_applicant_id: str
    result: MaskResponse


class BatchMaskResponse(BaseModel):
    status: str
    results: list[BatchMaskResult] = Field(default_factory=list)
    succeeded: int = 0
    failed: int = 0
    detail: str | None = None


class InlineMaskRequest(BaseModel):
    resume_base64: str = Field(..., description="Base64-encoded source resume PDF.")
    mask_strings: list[str] | None = Field(default=None, description="Exact PII strings to redact.")
    watermark_text: str = Field(default="", description="Fallback text if no watermark image found.")
    watermark_base64: str | None = Field(default=None, description="Base64-encoded PNG/JPEG watermark image.")


class InlineMaskResponse(BaseModel):
    status: str
    masked_pdf_base64: str | None = None
    redacted_regions: int | None = None
    watermark_used: str = "none"
    detail: str | None = None


# --- Routes ---

@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "salesforce_configured": sf_client.creds_configured(),
        "client_keys": sf_client.list_client_keys(),
    }


def _mask_one(req: MaskRequest, sf) -> MaskResponse:
    """Run the full mask pipeline for one Job Applicant against an already-connected
    Salesforce session. Shared by /mask and /mask/batch so both retry and batch
    behavior stay in one place."""
    # 1) Fetch resume PDF
    try:
        pdf_bytes = sf_client.fetch_resume_pdf(req.job_applicant_id, sf=sf)
    except (sf_client.ResumeNotFoundError, sf_client.InvalidIdError) as e:
        return MaskResponse(status="error", detail=str(e))

    # 2) Determine PII to mask
    mask_strings = req.mask_strings if req.mask_strings else detect_pii(pdf_bytes)
    if not mask_strings:
        return MaskResponse(status="error", detail="No PII strings to mask.")

    # 3) Resolve the client watermark image, in priority order:
    #    a) inline base64 (caller sent it directly, no extra round-trip)
    #    b) Salesforce File lookup, against account_id if passed, else auto-resolved
    #       from the Job Applicant's own Account lookup (SCSCHAMPS__Account__c) —
    #       best-effort, falls through to the global/text watermark if it can't resolve.
    watermark_png = None
    watermark_used = "none"
    if req.watermark_base64:
        try:
            watermark_png = base64.b64decode(req.watermark_base64, validate=True)
            watermark_used = "image:inline_base64"
        except (binascii.Error, ValueError):
            return MaskResponse(status="error", detail="watermark_base64 is not valid base64.")
    else:
        account_id = req.account_id or sf_client.resolve_account_id(req.job_applicant_id, sf=sf)
        try:
            watermark_png = sf_client.fetch_watermark_png(account_id=account_id, sf=sf)
            if watermark_png:
                watermark_used = f"image:account_{account_id}" if account_id else "image:global"
        except Exception:
            watermark_png = None

    # 4) True-redact PII text + candidate photos, then overlay watermark
    masked_bytes, hits = mask.mask_pdf_bytes(
        pdf_bytes, mask_strings,
        watermark_png=watermark_png,
        watermark_text=req.watermark_text or "CONFIDENTIAL",
    )

    # 5) Upload masked PDF back to Salesforce
    filename = f"masked_{req.job_applicant_id}.pdf"
    new_id = sf_client.upload_masked_pdf(req.job_applicant_id, masked_bytes, filename, sf=sf)

    return MaskResponse(
        status="ok",
        masked_content_version_id=new_id,
        redacted_regions=hits,
        watermark_used=watermark_used,
    )


@app.post("/mask", response_model=MaskResponse, dependencies=[Depends(require_api_key)])
def mask_endpoint(req: MaskRequest) -> MaskResponse:
    if not sf_client.creds_configured(client_key=req.client_key):
        return MaskResponse(status="error", detail="Salesforce credentials not configured.")

    try:
        return sf_client.with_session(lambda sf: _mask_one(req, sf), client_key=req.client_key)
    except (sf_client.MissingCredentialsError, sf_client.UnknownClientError) as e:
        return MaskResponse(status="error", detail=str(e))


def _batch_item_to_mask_request(item: BatchMaskItem, batch: BatchMaskRequest) -> MaskRequest:
    return MaskRequest(
        job_applicant_id=item.job_applicant_id,
        account_id=item.account_id,
        mask_strings=item.mask_strings,
        watermark_text=batch.watermark_text,
        watermark_base64=item.watermark_base64 or batch.watermark_base64,
        client_key=batch.client_key,
    )


@app.post("/mask/batch", response_model=BatchMaskResponse, dependencies=[Depends(require_api_key)])
def mask_batch_endpoint(req: BatchMaskRequest) -> BatchMaskResponse:
    """Mask many Job Applicants in one call, one shared Salesforce session/org.

    Each item is independent: one item's Salesforce/PDF error is captured in its
    own result and does NOT abort the rest of the batch. _mask_one() is safe to
    re-run (it only ever creates a new masked ContentVersion, never mutates the
    original), so if the shared session expires mid-batch, with_session()'s
    retry re-runs the whole batch once against a fresh session -- items already
    completed before the expiry just produce a second masked copy, not a
    duplicate-charge or inconsistent-state problem.
    """
    if not sf_client.creds_configured(client_key=req.client_key):
        return BatchMaskResponse(status="error", detail="Salesforce credentials not configured.")

    def run_batch(sf) -> BatchMaskResponse:
        results: list[BatchMaskResult] = []
        for item in req.items:
            item_req = _batch_item_to_mask_request(item, req)
            try:
                result = _mask_one(item_req, sf)
            except sf_client.SESSION_EXPIRED_ERRORS:
                # Let with_session()'s retry handle this at the batch level --
                # re-raising here (instead of turning it into a per-item error)
                # is what makes the whole batch retry against a fresh session.
                raise
            except Exception as e:
                # Any other per-item failure (e.g. a Salesforce validation rule
                # rejecting the upload) must not take down the rest of the batch.
                result = MaskResponse(status="error", detail=str(e)[:300])
            results.append(BatchMaskResult(job_applicant_id=item.job_applicant_id, result=result))
        succeeded = sum(1 for r in results if r.result.status == "ok")
        return BatchMaskResponse(
            status="ok",
            results=results,
            succeeded=succeeded,
            failed=len(results) - succeeded,
        )

    try:
        return sf_client.with_session(run_batch, client_key=req.client_key)
    except (sf_client.MissingCredentialsError, sf_client.UnknownClientError) as e:
        return BatchMaskResponse(status="error", detail=str(e))


@app.post("/mask/inline", response_model=InlineMaskResponse, dependencies=[Depends(require_api_key)])
def mask_inline_endpoint(req: InlineMaskRequest) -> InlineMaskResponse:
    """Mask a resume PDF handed to us directly — no Salesforce fetch or upload.

    For callers (e.g. Apex) that already have the source PDF bytes and want to
    write the masked result back themselves, rather than pointing us at a
    Salesforce record to fetch from/write to. This is what lets a caller own
    an arbitrary source/destination record shape (e.g. read from one object's
    Notes & Attachments, write to a different object's) without us needing to
    know its object/field API names.
    """
    try:
        pdf_bytes = base64.b64decode(req.resume_base64, validate=True)
    except (binascii.Error, ValueError):
        return InlineMaskResponse(status="error", detail="resume_base64 is not valid base64.")

    mask_strings = req.mask_strings if req.mask_strings else detect_pii(pdf_bytes)
    if not mask_strings:
        return InlineMaskResponse(status="error", detail="No PII strings to mask.")

    watermark_png = None
    watermark_used = "none"
    if req.watermark_base64:
        try:
            watermark_png = base64.b64decode(req.watermark_base64, validate=True)
            watermark_used = "image:inline_base64"
        except (binascii.Error, ValueError):
            return InlineMaskResponse(status="error", detail="watermark_base64 is not valid base64.")

    try:
        masked_bytes, hits = mask.mask_pdf_bytes(
            pdf_bytes, mask_strings,
            watermark_png=watermark_png,
            watermark_text=req.watermark_text or "CONFIDENTIAL",
        )
    except Exception as e:
        return InlineMaskResponse(status="error", detail=f"Masking failed: {e}"[:300])

    return InlineMaskResponse(
        status="ok",
        masked_pdf_base64=base64.b64encode(masked_bytes).decode("ascii"),
        redacted_regions=hits,
        watermark_used=watermark_used,
    )


@app.post("/watermark/upload", response_model=WatermarkUploadResponse,
         dependencies=[Depends(require_api_key)])
async def watermark_upload(
    account_id: str = Form(..., description="Salesforce Account Id."),
    file: UploadFile = File(..., description="Watermark image (PNG/JPEG)."),
    client_key: str | None = Form(
        default=None,
        description="Which registered Salesforce client/org (SF_CLIENTS_JSON) owns this account."),
) -> WatermarkUploadResponse:
    """Upload a custom watermark image for a client account.

    The image is stored as a Salesforce File titled 'ResumeWatermark'
    on the Account record. Subsequent /mask calls for that account
    will automatically use this image.

    Supported formats: PNG, JPEG. Recommended max dimensions: 800x800px.
    """
    if not sf_client.creds_configured(client_key=client_key):
        return WatermarkUploadResponse(status="error", detail="Salesforce not configured.")

    contents = await file.read()
    if not contents:
        return WatermarkUploadResponse(status="error", detail="Empty file.")

    # Validate it's an image
    if not contents.startswith(b"\x89PNG") and not contents.startswith(b"\xff\xd8"):
        return WatermarkUploadResponse(status="error", detail="Only PNG/JPEG images are supported.")

    filename = file.filename or "watermark.png"
    try:
        sf = sf_client.connect(client_key=client_key)
        new_id = sf_client.upload_watermark_image(account_id, contents, filename, sf=sf)
        return WatermarkUploadResponse(
            status="ok",
            content_version_id=new_id,
            detail=f"Watermark uploaded for account {account_id}.",
        )
    except Exception as e:
        return WatermarkUploadResponse(status="error", detail=str(e)[:200])


@app.get("/popup", response_class=HTMLResponse)
def popup_page() -> HTMLResponse:
    """HTML popup page for Salesforce embed (Lightning Component / iframe).

    Shows: watermark upload form, job applicants table, Mask Profile button.
    Was returning a bare str, which FastAPI JSON-encodes by default (content-type
    application/json) -- the iframe would render escaped JSON text, not the page.

    The API key (if MASK_API_KEY is set) is templated in here server-side so the
    page's own fetch() calls to /mask and /watermark/upload authenticate. Trust
    boundary: only Salesforce-authenticated users viewing this Lightning page can
    reach this HTML in the first place; the key still blocks blind internet abuse
    of the bare Railway URL.
    """
    api_key = os.environ.get("MASK_API_KEY", "").strip()
    html = _POPUP_HTML.replace("__MASK_API_KEY__", api_key)
    return HTMLResponse(content=html)


_POPUP_HTML = """<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Mask Profile</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, system-ui, sans-serif; background: #f5f7fa; color: #1a1a2e; padding: 24px; }
  h1 { font-size: 22px; margin-bottom: 16px; color: #1a1a2e; font-weight: 600; }

  .card { background: #fff; border: 1px solid #e2e6ef; border-radius: 10px; padding: 20px; margin-bottom: 16px; box-shadow: 0 1px 3px rgba(0,0,0,0.04); }
  .card-title { font-size: 14px; color: #6b7280; text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 12px; font-weight: 600; }

  .btn { display: inline-block; padding: 10px 24px; border-radius: 8px; border: none; cursor: pointer; font-size: 14px; font-weight: 600; text-align: center; width: 100%; }
  .btn-primary { background: #2563eb; color: white; }
  .btn-primary:hover { background: #1d4ed8; }
  .btn-primary:disabled { background: #93b4f5; cursor: not-allowed; }

  table { width: 100%; border-collapse: collapse; }
  th { padding: 10px 12px; text-align: left; font-size: 12px; color: #6b7280; text-transform: uppercase; letter-spacing: 0.5px; border-bottom: 2px solid #e2e6ef; font-weight: 600; }
  td { padding: 10px 12px; border-bottom: 1px solid #f0f2f5; font-size: 13px; color: #374151; }
  .empty { padding: 24px; text-align: center; color: #9ca3af; font-size: 13px; }

  .status { padding: 3px 10px; border-radius: 12px; font-size: 12px; font-weight: 500; display: inline-block; }
  .status-pending { background: #fef3c7; color: #92400e; }
  .status-done { background: #d1fae5; color: #065f46; }
  .status-failed { background: #fee2e2; color: #991b1b; }

  .file-upload { border: 2px dashed #d1d5db; border-radius: 8px; padding: 20px; text-align: center; cursor: pointer; margin-bottom: 12px; }
  .file-upload:hover { border-color: #2563eb; background: #f0f4ff; }

  #preview-img { max-width: 200px; max-height: 80px; margin: 8px auto; display: none; border: 1px solid #e2e6ef; border-radius: 4px; }
  .success { color: #065f46; background: #d1fae5; padding: 10px 14px; border-radius: 6px; margin-top: 10px; font-size: 13px; display: none; }
  .error { color: #991b1b; background: #fee2e2; padding: 10px 14px; border-radius: 6px; margin-top: 10px; font-size: 13px; display: none; }

  input, select { width: 100%; padding: 8px 12px; border: 1px solid #d1d5db; border-radius: 6px; font-size: 14px; margin-bottom: 10px; }
  input:focus { outline: none; border-color: #2563eb; box-shadow: 0 0 0 2px rgba(37,99,235,0.15); }
  label { display: block; font-size: 12px; color: #6b7280; margin-bottom: 4px; font-weight: 500; }
</style>
</head>
<body>

<h1>Mask Profile</h1>

<!-- Watermark Upload -->
<div class="card">
  <div class="card-title">Upload Watermark</div>
  <label for="account-id">Account ID</label>
  <input type="text" id="account-id" placeholder="Salesforce Account Id" />
  <div class="file-upload" id="upload-zone">
    <p style="color:#6b7280;font-size:14px;margin-bottom:4px;">Click to upload custom watermark</p>
    <p style="color:#9ca3af;font-size:12px;">PNG or JPEG — recommended 800x800px</p>
    <input type="file" id="file-input" accept="image/png,image/jpeg" style="display:none" />
  </div>
  <img id="preview-img" />
  <button class="btn btn-primary" id="upload-btn" disabled>Upload Watermark</button>
  <div id="upload-success" class="success"></div>
  <div id="upload-error" class="error"></div>
</div>

<!-- Job Applicants -->
<div class="card">
  <div class="card-title">Job Applicant</div>
  <table><thead><tr>
    <th>Job Applicant</th><th>Status</th>
  </tr></thead><tbody id="applicants-table">
    <tr><td colspan="2" class="empty">No status found!</td></tr>
  </tbody></table>
</div>

<!-- Mask Profile Button -->
<div class="card" style="text-align:center;">
  <p style="font-size:13px;color:#6b7280;margin-bottom:12px;">Click Below To Mask Candidate Profile:</p>
  <button class="btn btn-primary" id="mask-btn">Mask Profile</button>
  <div id="mask-success" class="success"></div>
  <div id="mask-error" class="error"></div>
</div>

<script>
const SF = window.location.origin;
const API_KEY = "__MASK_API_KEY__";  // server-templated; empty string when MASK_API_KEY unset
const authHeaders = API_KEY ? {'X-API-Key': API_KEY} : {};

// File upload
const uploadZone = document.getElementById('upload-zone');
const fileInput = document.getElementById('file-input');
const previewImg = document.getElementById('preview-img');
const accountId = document.getElementById('account-id');
const uploadBtn = document.getElementById('upload-btn');

uploadZone.addEventListener('click', () => fileInput.click());
fileInput.addEventListener('change', () => {
  const f = fileInput.files[0];
  if (!f) return;
  const r = new FileReader();
  r.onload = e => { previewImg.src = e.target.result; previewImg.style.display = 'block'; };
  r.readAsDataURL(f);
  uploadBtn.disabled = !accountId.value.trim();
});
accountId.addEventListener('input', () => {
  uploadBtn.disabled = !(accountId.value.trim() && fileInput.files.length);
});

uploadBtn.addEventListener('click', async () => {
  const fd = new FormData();
  fd.append('account_id', accountId.value.trim());
  fd.append('file', fileInput.files[0]);
  ['upload-success','upload-error'].forEach(id => document.getElementById(id).style.display = 'none');
  uploadBtn.disabled = true; uploadBtn.textContent = 'Uploading...';
  try {
    const r = await fetch(SF + '/watermark/upload', {method:'POST', headers: authHeaders, body:fd});
    const d = await r.json();
    if (d.status === 'ok') {
      document.getElementById('upload-success').textContent = '✅ Watermark uploaded!';
      document.getElementById('upload-success').style.display = 'block';
    } else {
      document.getElementById('upload-error').textContent = '❌ ' + (d.detail || 'Failed');
      document.getElementById('upload-error').style.display = 'block';
    }
  } catch(e) {
    document.getElementById('upload-error').textContent = '❌ ' + e.message;
    document.getElementById('upload-error').style.display = 'block';
  } finally {
    uploadBtn.disabled = false; uploadBtn.textContent = 'Upload Watermark';
  }
});

// Mask Profile
document.getElementById('mask-btn').addEventListener('click', async () => {
  ['mask-success','mask-error'].forEach(id => document.getElementById(id).style.display = 'none');
  const btn = document.getElementById('mask-btn');
  btn.disabled = true; btn.textContent = 'Masking...';
  try {
    const r = await fetch(SF + '/mask', {
      method:'POST', headers: Object.assign({'Content-Type':'application/json'}, authHeaders),
      body: JSON.stringify({
        job_applicant_id: document.querySelector('#applicants-table td')?.textContent || 'demo_id',
        account_id: accountId.value.trim() || null,
      }),
    });
    const d = await r.json();
    if (d.status === 'ok') {
      document.getElementById('mask-success').textContent = '✅ Masked — ' + d.redacted_regions + ' regions, watermark: ' + d.watermark_used;
      document.getElementById('mask-success').style.display = 'block';
    } else {
      document.getElementById('mask-error').textContent = '❌ ' + (d.detail || 'Failed');
      document.getElementById('mask-error').style.display = 'block';
    }
  } catch(e) {
    document.getElementById('mask-error').textContent = '❌ ' + e.message;
    document.getElementById('mask-error').style.display = 'block';
  } finally {
    btn.disabled = false; btn.textContent = 'Mask Profile';
  }
});
</script>
</body>
</html>"""