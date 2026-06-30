"""Resume-masking service — FastAPI.

Custom watermark IMAGE per client, stored in Salesforce as a File
titled 'ResumeWatermark' on the Account record. Falls back to text.

Flow:
  Salesforce popup page → POST /mask {job_applicant_id, account_id, mask_strings}
    1. Fetch resume PDF from Salesforce
    2. Fetch client's watermark image from Salesforce (by account_id)
    3. True-redact PII strings + overlay centered watermark image
    4. Upload masked PDF back to Salesforce
    5. Return {status, masked_content_version_id}

  Admin uploads watermark:
    POST /watermark/upload {account_id, image_file} → stores as 'ResumeWatermark'
"""
from __future__ import annotations

import re

from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from pydantic import BaseModel, Field

from app import mask, sf_client

app = FastAPI(title="Salesforce Resume Masking Service", version="1.2.0")


# --- PII detection fallback ---
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(r"\+?\d[\d\s().\-]{8,}\d")


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


# --- Routes ---

@app.get("/health")
def health() -> dict:
    return {"status": "ok", "salesforce_configured": sf_client.creds_configured()}


@app.post("/mask", response_model=MaskResponse)
def mask_endpoint(req: MaskRequest) -> MaskResponse:
    if not sf_client.creds_configured():
        return MaskResponse(status="error", detail="Salesforce credentials not configured.")

    try:
        sf = sf_client.connect()
    except sf_client.MissingCredentialsError as e:
        return MaskResponse(status="error", detail=str(e))

    # 1) Fetch resume PDF
    try:
        pdf_bytes = sf_client.fetch_resume_pdf(req.job_applicant_id, sf=sf)
    except sf_client.ResumeNotFoundError as e:
        return MaskResponse(status="error", detail=str(e))

    # 2) Determine PII to mask
    mask_strings = req.mask_strings if req.mask_strings else detect_pii(pdf_bytes)
    if not mask_strings:
        return MaskResponse(status="error", detail="No PII strings to mask.")

    # 3) Fetch client watermark image from Salesforce
    watermark_png = None
    watermark_used = "none"
    try:
        watermark_png = sf_client.fetch_watermark_png(account_id=req.account_id, sf=sf)
        if watermark_png:
            watermark_used = f"image:account_{req.account_id}" if req.account_id else "image:global"
    except Exception:
        watermark_png = None

    # 4) True-redact + overlay watermark
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


@app.post("/watermark/upload", response_model=WatermarkUploadResponse)
async def watermark_upload(
    account_id: str = Form(..., description="Salesforce Account Id."),
    file: UploadFile = File(..., description="Watermark image (PNG/JPEG)."),
) -> WatermarkUploadResponse:
    """Upload a custom watermark image for a client account.

    The image is stored as a Salesforce File titled 'ResumeWatermark'
    on the Account record. Subsequent /mask calls for that account
    will automatically use this image.

    Supported formats: PNG, JPEG. Recommended max dimensions: 800x800px.
    """
    if not sf_client.creds_configured():
        return WatermarkUploadResponse(status="error", detail="Salesforce not configured.")

    contents = await file.read()
    if not contents:
        return WatermarkUploadResponse(status="error", detail="Empty file.")

    # Validate it's an image
    if not contents.startswith(b"\x89PNG") and not contents.startswith(b"\xff\xd8"):
        return WatermarkUploadResponse(status="error", detail="Only PNG/JPEG images are supported.")

    filename = file.filename or "watermark.png"
    try:
        sf = sf_client.connect()
        new_id = sf_client.upload_watermark_image(account_id, contents, filename, sf=sf)
        return WatermarkUploadResponse(
            status="ok",
            content_version_id=new_id,
            detail=f"Watermark uploaded for account {account_id}.",
        )
    except Exception as e:
        return WatermarkUploadResponse(status="error", detail=str(e)[:200])


@app.get("/popup")
def popup_page() -> str:
    """HTML popup page for Salesforce embed (Lightning Component / iframe).

    Shows: watermark upload form, job applicants table, Mask Profile button.
    """
    return _POPUP_HTML


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
    const r = await fetch(SF + '/watermark/upload', {method:'POST', body:fd});
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
      method:'POST', headers:{'Content-Type':'application/json'},
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