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
<title>Resume Masking</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, system-ui, sans-serif; background: #fff; color: #1a1a2e; padding: 24px; }
  h1 { font-size: 20px; margin-bottom: 8px; color: #2563eb; }
  h2 { font-size: 15px; margin: 20px 0 12px; color: #374151; }
  .section { background: #f9fafb; border: 1px solid #e5e7eb; border-radius: 8px; padding: 16px; margin-bottom: 16px; }
  .section-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; }
  .section-header h3 { font-size: 14px; color: #6b7280; text-transform: uppercase; letter-spacing: 0.5px; }
  label { display: block; font-size: 13px; color: #374151; margin-bottom: 4px; }
  input, select { width: 100%; padding: 8px 12px; border: 1px solid #d1d5db; border-radius: 6px; font-size: 14px; margin-bottom: 12px; }
  input:focus { outline: none; border-color: #2563eb; box-shadow: 0 0 0 2px rgba(37,99,235,0.15); }
  .btn { padding: 8px 20px; border-radius: 6px; border: none; cursor: pointer; font-size: 14px; font-weight: 500; }
  .btn-primary { background: #2563eb; color: white; }
  .btn-primary:hover { background: #1d4ed8; }
  .btn-secondary { background: #e5e7eb; color: #374151; }
  .btn-secondary:hover { background: #d1d5db; }
  .btn-row { display: flex; gap: 8px; margin-top: 8px; }
  .file-upload { border: 2px dashed #d1d5db; border-radius: 8px; padding: 20px; text-align: center; cursor: pointer; }
  .file-upload:hover { border-color: #2563eb; background: #f0f4ff; }
  table { width: 100%; border-collapse: collapse; }
  th { padding: 8px 12px; text-align: left; font-size: 12px; color: #6b7280; text-transform: uppercase; letter-spacing: 0.5px; border-bottom: 2px solid #e5e7eb; }
  td { padding: 10px 12px; border-bottom: 1px solid #f3f4f6; font-size: 13px; }
  .badge { display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 11px; font-weight: 500; }
  .badge-green { background: #d1fae5; color: #065f46; }
  .badge-yellow { background: #fef3c7; color: #92400e; }
  .badge-gray { background: #f3f4f6; color: #6b7280; }
  .badge-red { background: #fee2e2; color: #991b1b; }
  .empty { padding: 24px; text-align: center; color: #9ca3af; font-size: 13px; }
  .mono { font-family: monospace; font-size: 12px; }
  #preview-img { max-width: 200px; max-height: 100px; margin-top: 8px; display: none; border: 1px solid #e5e7eb; border-radius: 4px; }
  .success { color: #065f46; background: #d1fae5; padding: 8px 12px; border-radius: 6px; margin-top: 8px; font-size: 13px; display: none; }
  .error { color: #991b1b; background: #fee2e2; padding: 8px 12px; border-radius: 6px; margin-top: 8px; font-size: 13px; display: none; }
</style></head>
<body>
<h1>🔒 Resume Masking</h1>
<p style="color:#6b7280;font-size:13px;margin-bottom:16px;">Redact PII + overlay client watermark</p>

<!-- Watermark Upload Section -->
<div class="section">
  <div class="section-header"><h3>Client Watermark</h3></div>
  <label for="account-id">Account ID</label>
  <input type="text" id="account-id" placeholder="Salesforce Account Id (e.g. 001...)" />
  <div class="file-upload" id="upload-zone">
    <p style="color:#6b7280;font-size:14px;margin-bottom:4px;">Drop watermark image here or click to upload</p>
    <p style="color:#9ca3af;font-size:12px;">PNG or JPEG, recommended 800x800px max</p>
    <input type="file" id="file-input" accept="image/png,image/jpeg" style="display:none" />
  </div>
  <img id="preview-img" />
  <div class="btn-row">
    <button class="btn btn-primary" id="upload-btn" disabled>Upload Watermark</button>
  </div>
  <div id="upload-success" class="success"></div>
  <div id="upload-error" class="error"></div>
</div>

<!-- Job Applicants Table -->
<div class="section">
  <div class="section-header"><h3>Job Applicants</h3></div>
  <table><thead><tr>
    <th>Name</th><th>Job</th><th>Status</th><th>Action</th>
  </tr></thead><tbody id="applicants-table">
    <tr><td colspan="4" class="empty">No applicants loaded. Fetch from Salesforce.</td></tr>
  </tbody></table>
  <div style="margin-top:12px;text-align:right;">
    <button class="btn btn-secondary" id="fetch-applicants">Refresh</button>
  </div>
</div>

<!-- Mask Selected -->
<div class="section" style="display:none;" id="mask-section">
  <h3>Mask Profile</h3>
  <p style="font-size:13px;color:#6b7280;margin-bottom:12px;">Click below to mask the selected candidate's resume.</p>
  <div class="btn-row">
    <button class="btn btn-primary" id="mask-btn">Mask Profile</button>
  </div>
  <div id="mask-result" class="success"></div>
  <div id="mask-error" class="error"></div>
</div>

<script>
const SF_MASK_URL = window.location.origin;

// File upload handling
const uploadZone = document.getElementById('upload-zone');
const fileInput = document.getElementById('file-input');
const previewImg = document.getElementById('preview-img');
const accountIdInput = document.getElementById('account-id');
const uploadBtn = document.getElementById('upload-btn');

uploadZone.addEventListener('click', () => fileInput.click());
uploadZone.addEventListener('dragover', (e) => { e.preventDefault(); uploadZone.style.borderColor = '#2563eb'; });
uploadZone.addEventListener('dragleave', () => { uploadZone.style.borderColor = '#d1d5db'; });
uploadZone.addEventListener('drop', (e) => {
  e.preventDefault();
  uploadZone.style.borderColor = '#d1d5db';
  if (e.dataTransfer.files.length) fileInput.files = e.dataTransfer.files;
  handleFileSelect();
});

fileInput.addEventListener('change', handleFileSelect);

function handleFileSelect() {
  const file = fileInput.files[0];
  if (!file) return;
  const reader = new FileReader();
  reader.onload = (e) => {
    previewImg.src = e.target.result;
    previewImg.style.display = 'block';
    uploadZone.querySelector('p:first-child').textContent = file.name;
    uploadBtn.disabled = !accountIdInput.value.trim();
  };
  reader.readAsDataURL(file);
}

accountIdInput.addEventListener('input', () => {
  uploadBtn.disabled = !(accountIdInput.value.trim() && fileInput.files.length);
});

// Upload watermark
uploadBtn.addEventListener('click', async () => {
  const formData = new FormData();
  formData.append('account_id', accountIdInput.value.trim());
  formData.append('file', fileInput.files[0]);
  document.getElementById('upload-error').style.display = 'none';
  document.getElementById('upload-success').style.display = 'none';
  uploadBtn.disabled = true;
  uploadBtn.textContent = 'Uploading...';

  try {
    const resp = await fetch(`${SF_MASK_URL}/watermark/upload`, { method: 'POST', body: formData });
    const data = await resp.json();
    if (data.status === 'ok') {
      document.getElementById('upload-success').textContent = '✅ Watermark uploaded successfully!';
      document.getElementById('upload-success').style.display = 'block';
    } else {
      document.getElementById('upload-error').textContent = '❌ ' + (data.detail || 'Upload failed.');
      document.getElementById('upload-error').style.display = 'block';
    }
  } catch (err) {
    document.getElementById('upload-error').textContent = '❌ Network error: ' + err.message;
    document.getElementById('upload-error').style.display = 'block';
  } finally {
    uploadBtn.disabled = false;
    uploadBtn.textContent = 'Upload Watermark';
  }
});

// Fetch applicants (placeholder — real impl queries SF)
document.getElementById('fetch-applicants').addEventListener('click', async () => {
  const tbody = document.getElementById('applicants-table');
  tbody.innerHTML = '<tr><td colspan="4" class="empty">Loading...</td></tr>';
  // In production: query Salesforce via REST API or parent window
  // This is a stub — real integration passes data from Salesforce LWC
  setTimeout(() => {
    tbody.innerHTML = '<tr><td colspan="4" class="empty">Integrate with Salesforce data source to populate.</td></tr>';
  }, 500);
});

// Mask profile button
document.getElementById('mask-btn').addEventListener('click', async () => {
  const btn = document.getElementById('mask-btn');
  const result = document.getElementById('mask-result');
  const error = document.getElementById('mask-error');
  result.style.display = 'none';
  error.style.display = 'none';
  btn.disabled = true;
  btn.textContent = 'Masking...';

  try {
    const resp = await fetch(`${SF_MASK_URL}/mask`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        job_applicant_id: '__SELECTED_ID__',
        account_id: accountIdInput.value.trim() || null,
        watermark_text: '',
      }),
    });
    const data = await resp.json();
    if (data.status === 'ok') {
      result.textContent = `✅ Masked. Regions redacted: ${data.redacted_regions}. Watermark: ${data.watermark_used}`;
      result.style.display = 'block';
    } else {
      error.textContent = '❌ ' + (data.detail || 'Masking failed.');
      error.style.display = 'block';
    }
  } catch (err) {
    error.textContent = '❌ Network error: ' + err.message;
    error.style.display = 'block';
  } finally {
    btn.disabled = false;
    btn.textContent = 'Mask Profile';
  }
});
</script>
</body>
</html>"""