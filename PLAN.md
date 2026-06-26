# Salesforce ATS — Resume Masking + JD Parser (architecture plan)

> Recruiting/RPO team on Salesforce. Two features. Part 1 builds first. Refined from the client call.
> Input can be pasted audio transcript or typed text. SECRETS: any creds heard on the call go in
> Railway secret store / a Connected App — never in code, never in a URL.

---

## Part 1 — Resume Masking  (BUILD FIRST) — REPLACES the broken freelancer service

**Context (from the call):** Salesforce already has a **"Generate Masking" button** on (bulk) Job
Applicant records. Today it **redirects/pop-ups to an external app built by a freelancer** — the redirect
is broken AND the masking is inaccurate: it **misses** some phone/email characters and **over-masks**
things it shouldn't (e.g. academic %/marks). Replace it with our own service.

**Precise requirements:**
- Mask ONLY: candidate **name, phone, email, personal/confidential contact**.
- DO **NOT** mask: work **experience**, academic **marks/percentages** (current over-masking is the client complaint).
- **Centered logo watermark** on every page (agreed best — clean, consistent).
- **No redirect/popup** — masked PDF written straight back into the Salesforce record.
- One client first, **Railway** infra, scale later.

### Architecture (corrected)
```
Salesforce: "Generate Masking" button on Job Applicant record (bulk-capable)
   │  on-click → call the Python service with the JOB APPLICANT ID (+ masking-profile id)
   │  SF integration-user creds live in the SERVICE config/secrets, NOT in the URL (security fix)
   ▼
[Masking service — Python FastAPI on Railway, Dockerized]
   │  1. simple-salesforce: SOQL by Job Applicant ID → fetch the raw resume PDF (ContentVersion)
   │  2. get EXACT values to mask — REUSE the on-prem resume parser (30+ fields, accurate) →
   │     mask those exact strings → precise, no over-masking. Fallback: regex (email/phone) +
   │     OpenRouter LLM only for names the parser misses.
   │  3. TRUE-redact ONLY those strings with PyMuPDF (delete glyphs, not a black box)
   │  4. centered logo WATERMARK on every page (insert_image, overlay, low opacity)
   │  5. simple-salesforce: upload masked PDF back as a new ContentVersion on the same record
   ▼
Salesforce record shows the masked resume — NO redirect, NO popup.
```

**Why this fixes the complaints:** mask the EXACT strings the parser returns → no missed chars, no
over-masking of marks/experience (the freelancer's blind/regex-only approach was the bug); write-back via
simple-salesforce → result appears in-record, no broken redirect; one centered watermark → consistent.

### Detection — do we need an LLM?  Mostly NO.
If the on-prem parser already extracts name/phone/email accurately (it captures 30+ fields), **mask those
exact values** — deterministic + precise. Use OpenRouter only as a fallback for fields the parser misses.
**Docling** (doc/OCR toolkit) is the option if any resumes are scanned images — flag scanned PDFs for OCR.

### Config page (your ask)
A small admin page in the service to set ONCE: Salesforce instance URL, the **integration user**
(username + password + security token) OR a Connected App, and the **masking profile** (which fields to
mask). **Security fix vs the call:** do NOT pass SF username/password in the button URL (leaks via
browser history / server logs) — keep them in the service's Railway secrets; the button passes ONLY the
Job Applicant ID + masking-profile id.

### Build sequence
1. Python script: resume PDF + list-of-strings-to-mask → PyMuPDF true-redact + centered watermark.
   Runs offline on a sample resume (testable before any Salesforce wiring).
2. Wrap in FastAPI + simple-salesforce: fetch-by-ID, write-back; integration creds in env.
3. Salesforce: repoint the existing "Generate Masking" button to the new service (pass Applicant ID),
   delete the freelancer redirect. Feature-flag the target.
4. Config/admin page for SF URL + creds + masking profile.

### Test strategy (covers the exact client complaint)
- Golden resumes → assert masked PDF text layer has **NO** name/phone/email strings (true-redact proof)
  AND **still has** experience + marks/percentages (no over-masking) AND watermark on every page.
- simple-salesforce write-back mocked → assert a new ContentVersion is created.
- Scanned/image-only resume → OCR-needed gap flagged + routed to manual.

### Deploy / rollback
Railway (Docker), one client first. Feature-flag the button target so you can revert to the old service
instantly. Scale to more clients after the first proves out.

### What I need to start Part 1
- The **on-prem resume parser**'s interface (input PDF → which fields it returns) — to reuse for detection.
- A **Salesforce integration user** (or Connected App) + instance URL + the Job Applicant object's
  resume/ContentVersion relationship.
- A **sample resume PDF** + the **logo asset** (PNG) for the watermark.
- The **masking-profile field list** (exact fields to mask) from Ranvi/whoever owns it.

---

## Part 2 — JD Parser: Outlook → Salesforce Jobs  (PLAN, after Part 1)
**Goal:** client job-opening email in Outlook → auto-capture → parse the JD into structured fields →
create a **Job__c** record in the Salesforce ATS; surfaced on the Jobs page/table. 2-way.

### Architecture
```
Outlook team mailbox
   │  Microsoft Graph API change-notification (webhook on the mailbox/folder)
   ▼  new "job opening" mail →
[JD pipe service]
   │  extract email body (+ attachments) → structure to the Job__c schema
   │  REUSE the existing DeepSeek-V3 JD parser if its I/O is raw-JD-text → Job fields
   ▼  Salesforce Composite REST → create Job__c {role, jd_text, headcount, skills, client, ...}
Salesforce Jobs list view / Lightning page  ← the "designated page" (attributes table)
```
- Outlook capture = **Microsoft Graph webhook** on the team mailbox (headless, robust; no add-in needed).
  Optional add-in button for manual "send to ATS".
- 2-way = confirmation/category back to Outlook once the Job is created.

### Reuse-vs-rebuild on the existing DeepSeek-V3 JD parser
**Default: REUSE / WRAP it (ponytail) — do NOT rebuild a parser.** The NEW work is the
**Outlook→Salesforce pipe**, not the parsing brain.
- IF Yogi's parser takes raw JD text → emits Job__c fields → the pipe just forwards the email body. Zero new parser.
- IF it's bound to a Salesforce form (can't take arbitrary text) → build a THIN external parser
  (DeepSeek-V3 via OpenRouter) mirroring his exact field mapping. **Decision gate = 30 min with Yogi.**

### What I need for Part 2
Job__c schema (field API names + types), a sample client JD email, Salesforce + Microsoft Graph app
creds, and Yogi's parser input/output contract.

---

## Sequence
Part 1 masking (offline script → FastAPI + simple-salesforce → repoint button → config page) →
Part 2 Outlook Graph pipe → reuse/wrap Yogi's parser → Job__c writes. Both share one Salesforce
integration user + the same Railway deploy pattern.
