"""Resume-masking service — FastAPI. Replaces the broken freelancer redirect.

Flow (no redirect, no popup — pure JSON):
    Salesforce "Generate Masking" button  --POST /mask {job_applicant_id, account_id}-->  this service
        1. sf_client.fetch_resume_pdf(id)            — pull the resume PDF from Salesforce
        2. detect PII strings to mask                — request `mask_strings`, else stub detect_pii()
        3. sf_client.fetch_watermark_png(account_id) — per-client logo from SF; fallback org-wide → text
        4. mask.mask_pdf_bytes(...)                  — PyMuPDF true-redact + centered watermark
        5. sf_client.upload_masked_pdf(id, ...)      — write the masked copy back as a new ContentVersion
        6. return {status, masked_content_version_id}

SECURITY: the resume + masked PDF live in memory only. We never log PII strings or PDF bytes.
Salesforce creds live in the service ENV (Railway secrets), never in the request/URL.
"""
from __future__ import annotations

import re

from fastapi import FastAPI
from pydantic import BaseModel, Field

from app import mask, sf_client

app = FastAPI(title="Salesforce Resume Masking Service", version="1.0.0")


# --- PII detection -----------------------------------------------------------------------------
# Stub for offline/standalone use. PRODUCTION: the caller should pass the exact `mask_strings` the
# on-prem resume parser returns (name/phone/email — 30+ fields, accurate) so we mask EXACT values and
# never over-mask experience or academic marks (the client's original complaint). This regex pass is a
# best-effort fallback for email + phone only.
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
# Phone: 10-15 digits possibly grouped by spaces/dashes/dots/parens, optional leading +.
_PHONE_RE = re.compile(r"\+?\d[\d\s().\-]{8,}\d")


def detect_pii(pdf_bytes: bytes) -> list[str]:
    """Best-effort fallback PII detection from the PDF text layer: email + phone via regex.

    TODO(parser): replace/augment with the on-prem resume parser (and/or OpenRouter LLM) to also catch
    candidate NAMES the regex can't know. Until then, callers should pass `mask_strings` explicitly.
    """
    import fitz  # local import keeps PyMuPDF off the import path until needed

    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    text = "".join(page.get_text() for page in doc)
    doc.close()

    found: list[str] = []
    found += _EMAIL_RE.findall(text)
    found += [m.strip() for m in _PHONE_RE.findall(text)]
    # De-dupe, drop empties, keep deterministic order.
    seen: set[str] = set()
    out: list[str] = []
    for s in found:
        s = s.strip()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


# --- API models --------------------------------------------------------------------------------
class MaskRequest(BaseModel):
    job_applicant_id: str = Field(..., description="Salesforce Job Applicant record Id.")
    account_id: str | None = Field(
        default=None,
        description="Salesforce Account Id of the client company. Used to fetch their specific "
                    "watermark logo (File titled 'ResumeWatermark' on the Account). Falls back to "
                    "the org-wide 'ResumeWatermark' file if omitted or not found.")
    masking_profile: str | None = Field(
        default=None, description="Optional masking-profile id (which fields to mask). Reserved hook.")
    mask_strings: list[str] | None = Field(
        default=None,
        description="Exact strings to redact (from the on-prem parser). If omitted, detect_pii() runs.")


class MaskResponse(BaseModel):
    status: str
    masked_content_version_id: str | None = None
    redacted_regions: int | None = None
    detail: str | None = None


# --- Routes ------------------------------------------------------------------------------------
@app.get("/health")
def health() -> dict:
    """Liveness + creds pre-flight (no network call). Always 200 so Railway health checks stay green;
    `salesforce_configured` tells you whether creds are wired."""
    return {"status": "ok", "salesforce_configured": sf_client.creds_configured()}


@app.post("/mask", response_model=MaskResponse)
def mask_endpoint(req: MaskRequest) -> MaskResponse:
    # Fail fast + cleanly if creds aren't configured (they arrive in a later session).
    if not sf_client.creds_configured():
        return MaskResponse(
            status="error",
            detail="Salesforce credentials not configured on the service. Set SF_USERNAME / "
                   "SF_PASSWORD / SF_SECURITY_TOKEN (or the Connected App vars). See .env.example.",
        )

    try:
        sf = sf_client.connect()
    except sf_client.MissingCredentialsError as e:
        return MaskResponse(status="error", detail=str(e))

    # 1) Pull the resume PDF (in memory only).
    try:
        pdf_bytes = sf_client.fetch_resume_pdf(req.job_applicant_id, sf=sf)
    except sf_client.ResumeNotFoundError as e:
        return MaskResponse(status="error", detail=str(e))

    # 2) Determine the exact strings to mask. Prefer caller-supplied (parser output); else fallback.
    mask_strings = req.mask_strings if req.mask_strings else detect_pii(pdf_bytes)
    if not mask_strings:
        return MaskResponse(
            status="error",
            detail="No PII strings to mask. Pass `mask_strings` from the resume parser, or ensure the "
                   "PDF has a text layer (scanned image-only PDFs need OCR — route to manual).",
        )

    # 3) Fetch per-client watermark from Salesforce (falls back to org-wide logo, then text stub).
    watermark_bytes = sf_client.fetch_watermark_png(account_id=req.account_id, sf=sf)

    # 4) True-redact + centered watermark, fully in memory.
    masked_bytes, hits = mask.mask_pdf_bytes(pdf_bytes, mask_strings, watermark_png=watermark_bytes)

    # 5) Write the masked copy back to Salesforce as a new ContentVersion.
    filename = f"masked_{req.job_applicant_id}.pdf"
    new_id = sf_client.upload_masked_pdf(req.job_applicant_id, masked_bytes, filename, sf=sf)

    # 6) JSON result — no redirect, no popup. (We never echo PII or bytes.)
    return MaskResponse(
        status="ok",
        masked_content_version_id=new_id,
        redacted_regions=hits,
    )
