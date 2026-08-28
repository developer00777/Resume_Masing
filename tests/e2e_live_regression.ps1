# E2E regression suite against a LIVE deployment (Railway).
# Read-only + safely-reversible checks only -- self-register/admin-write tests
# clean up after themselves via the admin API when ADMIN_API_KEY is supplied.
#
# Usage:
#   pwsh tests/e2e_live_regression.ps1 -BaseUrl https://resume-masker-production.up.railway.app `
#       -ApiKey <MASK_API_KEY> -AdminApiKey <ADMIN_API_KEY>
#
# ApiKey / AdminApiKey are optional -- omit to test the "unauthenticated" paths only.

param(
    [Parameter(Mandatory=$true)][string]$BaseUrl,
    [string]$ApiKey = "",
    [string]$AdminApiKey = ""
)

$ErrorActionPreference = "Stop"
$results = New-Object System.Collections.Generic.List[object]

function Test-Case {
    param([string]$Name, [scriptblock]$Body)
    try {
        $ok, $detail = & $Body
        $results.Add([pscustomobject]@{ Name = $Name; Pass = [bool]$ok; Detail = $detail })
    } catch {
        $results.Add([pscustomobject]@{ Name = $Name; Pass = $false; Detail = "EXCEPTION: $($_.Exception.Message)" })
    }
}

function Invoke-Api {
    # Windows PowerShell 5.1 compatible: Invoke-WebRequest throws on 4xx/5xx (no
    # -SkipHttpErrorCheck, that's PS7+), so a non-2xx response is read back out
    # of the exception's own Response stream instead of treated as a hard failure.
    param([string]$Method, [string]$Path, $Body = $null, [hashtable]$Headers = @{}, [string]$ContentType = "application/json")
    $uri = "$BaseUrl$Path"
    $params = @{ Uri = $uri; Method = $Method; Headers = $Headers; TimeoutSec = 30; UseBasicParsing = $true }
    if ($Body -ne $null) { $params.Body = $Body; $params.ContentType = $ContentType }
    try {
        $resp = Invoke-WebRequest @params
        return @{ Status = [int]$resp.StatusCode; Json = try { $resp.Content | ConvertFrom-Json } catch { $null }; Raw = $resp.Content; Headers = $resp.Headers }
    } catch [System.Net.WebException] {
        $webResp = $_.Exception.Response
        if ($webResp -eq $null) { return @{ Status = -1; Json = $null; Raw = $_.Exception.Message; Headers = $null } }
        $status = [int]$webResp.StatusCode
        $stream = $webResp.GetResponseStream()
        $reader = New-Object System.IO.StreamReader($stream)
        $raw = $reader.ReadToEnd()
        $reader.Close()
        return @{ Status = $status; Json = try { $raw | ConvertFrom-Json } catch { $null }; Raw = $raw; Headers = $webResp.Headers }
    } catch {
        # PS 5.1's Invoke-WebRequest sometimes throws Microsoft.PowerShell.Commands.HttpResponseException
        # instead of System.Net.WebException depending on the failure path -- handle both.
        if ($_.Exception.Response) {
            $webResp = $_.Exception.Response
            $status = [int]$webResp.StatusCode
            try {
                $stream = $webResp.GetResponseStream()
                $reader = New-Object System.IO.StreamReader($stream)
                $raw = $reader.ReadToEnd()
                $reader.Close()
            } catch { $raw = "" }
            return @{ Status = $status; Json = try { $raw | ConvertFrom-Json } catch { $null }; Raw = $raw; Headers = $webResp.Headers }
        }
        return @{ Status = -1; Json = $null; Raw = $_.Exception.Message; Headers = $null }
    }
}

$authHeaders = @{}
if ($ApiKey) { $authHeaders["X-API-Key"] = $ApiKey }
$adminHeaders = @{}
if ($AdminApiKey) { $adminHeaders["X-Admin-API-Key"] = $AdminApiKey }

function New-SamplePdfBase64 {
    # Minimal valid single-page PDF with a text stream (built by hand -- no PyMuPDF
    # dependency needed for this script to run standalone against a live URL).
    $pdf = @"
%PDF-1.4
1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj
2 0 obj<</Type/Pages/Count 1/Kids[3 0 R]>>endobj
3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 595 842]/Resources<</Font<</F1 4 0 R>>>>/Contents 5 0 R>>endobj
4 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj
5 0 obj<</Length 140>>
stream
BT /F1 12 Tf 72 750 Td (Regression Tester) Tj 0 -20 Td (Phone: +1 555 010 0111) Tj 0 -20 Td (Email: regress@example.com) Tj 0 -20 Td (Experience: 4 years) Tj ET
endstream
endobj
xref
0 6
trailer<</Size 6/Root 1 0 R>>
startxref
0
%%EOF
"@
    return [Convert]::ToBase64String([System.Text.Encoding]::ASCII.GetBytes($pdf))
}

$samplePdfB64 = New-SamplePdfBase64
$tinyPngB64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="

Write-Output "== Resume Masking Service -- Live E2E Regression =="
Write-Output "Base URL: $BaseUrl"
Write-Output "ApiKey supplied: $([bool]$ApiKey)   AdminApiKey supplied: $([bool]$AdminApiKey)"
Write-Output ""

# ── 1. Health / smoke ────────────────────────────────────────────────────
Test-Case "GET /health returns ok" {
    $r = Invoke-Api GET "/health"
    return ($r.Status -eq 200 -and $r.Json.status -eq "ok"), "status=$($r.Status) body=$($r.Raw)"
}

Test-Case "GET /popup returns real HTML (not JSON-escaped)" {
    $r = Invoke-Api GET "/popup"
    $ok = $r.Status -eq 200 -and $r.Raw -like "*<!DOCTYPE html>*"
    return $ok, "status=$($r.Status) content-type=$($r.Headers['Content-Type'])"
}

Test-Case "GET /nonexistent-route returns 404" {
    $r = Invoke-Api GET "/nonexistent-route-xyz"
    return ($r.Status -eq 404), "status=$($r.Status)"
}

Test-Case "GET /candidate/MaskProfileIndex renders the real Salesforce-embedded UI" {
    # This is the exact path Salesforce's button/Lightning Component hits.
    # Was a 404, then briefly a redirect stopgap to /popup, now the real
    # Jinja2-templated page (Mask Profile + User Settings tabs) served from
    # this same FastAPI service.
    $r = Invoke-Api GET "/candidate/MaskProfileIndex"
    $ok = $r.Status -eq 200 -and $r.Raw -like "*Mask Profile*" -and $r.Raw -like "*/static/js/app.js*"
    return $ok, "status=$($r.Status)"
}

Test-Case "GET /candidate/MaskProfileIndex prefills ids from the ?ids= query param" {
    $r = Invoke-Api GET "/candidate/MaskProfileIndex?ids=a0X000000000777"
    $ok = $r.Status -eq 200 -and $r.Raw -like "*a0X000000000777*"
    return $ok, "status=$($r.Status)"
}

Test-Case "GET /static/css/style.css and /static/js/app.js are served" {
    $css = Invoke-Api GET "/static/css/style.css"
    $js = Invoke-Api GET "/static/js/app.js"
    $ok = $css.Status -eq 200 -and $js.Status -eq 200
    return $ok, "css=$($css.Status) js=$($js.Status)"
}

Test-Case "POST /candidate/settings without X-API-Key is rejected IF MASK_API_KEY is enforced" {
    # DELIBERATELY the only /candidate/settings check run against a live
    # deployment -- unlike /clients/self-register (per-org rows, safe to
    # create-then-delete a test entry), this endpoint overwrites the SINGLE
    # default-connection credentials row. A "success" test with a valid
    # X-API-Key and fake data would silently break the live Salesforce
    # connection. Never add one here; verify save/connect behavior against
    # local Docker + a disposable Postgres instead (see tests/test_client_registry_db.py).
    $r = Invoke-Api POST "/candidate/settings" (@{password="x"; login_host="test"} | ConvertTo-Json)
    if ($r.Status -eq 401) { return $true, "enforced -- 401 (expected/secure)" }
    if ($r.Status -eq 200) { return $true, "NOT enforced -- endpoint is open" }
    return $false, "unexpected status=$($r.Status) body=$($r.Raw)"
}

Test-Case "DELETE /candidate/settings without X-API-Key is rejected IF MASK_API_KEY is enforced" {
    # Same reasoning as the POST check above -- only verifies auth
    # enforcement, never actually clears a real stored override.
    $r = Invoke-Api DELETE "/candidate/settings"
    if ($r.Status -eq 401) { return $true, "enforced -- 401 (expected/secure)" }
    if ($r.Status -eq 200) { return $true, "NOT enforced -- endpoint is open" }
    return $false, "unexpected status=$($r.Status) body=$($r.Raw)"
}

# ── 2. Auth enforcement ──────────────────────────────────────────────────
Test-Case "POST /mask without X-API-Key is rejected IF MASK_API_KEY is enforced (else runs unauthenticated)" {
    $r = Invoke-Api POST "/mask" (@{job_applicant_id="a0X000000000001"} | ConvertTo-Json)
    if ($r.Status -eq 401) { return $true, "MASK_API_KEY enforced -- 401 without header (expected/secure)" }
    if ($r.Status -eq 200) { return ($true), "MASK_API_KEY NOT enforced -- endpoint is open (status=$($r.Json.status))" }
    return $false, "unexpected status=$($r.Status) body=$($r.Raw)"
}

Test-Case "POST /mask/inline without X-API-Key is rejected IF MASK_API_KEY is enforced" {
    $r = Invoke-Api POST "/mask/inline" (@{resume_base64=$samplePdfB64} | ConvertTo-Json)
    if ($r.Status -eq 401) { return $true, "enforced -- 401 (expected/secure)" }
    if ($r.Status -eq 200) { return $true, "NOT enforced -- endpoint is open" }
    return $false, "unexpected status=$($r.Status) body=$($r.Raw)"
}

Test-Case "GET /admin/clients without X-Admin-API-Key never returns 200 (503 if unconfigured, 401 if configured)" {
    # Whether the "correct" unauthenticated response is 503 or 401 depends on
    # whether ADMIN_API_KEY is actually set on this deployment -- this script
    # can't know that a priori, so both are valid so long as it's never 200
    # (200 would mean the route is open with no key at all -- fail-closed broken).
    $r = Invoke-Api GET "/admin/clients"
    $ok = $r.Status -eq 503 -or $r.Status -eq 401
    return $ok, "status=$($r.Status) body=$($r.Raw) -- 503=ADMIN_API_KEY unset, 401=set-but-header-missing, both are correctly fail-closed; 200 would NOT be"
}

Test-Case "GET /admin/clients with wrong X-Admin-API-Key is rejected (401)" {
    $r = Invoke-Api GET "/admin/clients" -Headers @{"X-Admin-API-Key"="definitely-wrong-key"}
    return ($r.Status -eq 401), "status=$($r.Status)"
}

if ($ApiKey) {
    Test-Case "POST /mask with wrong X-API-Key is rejected (401)" {
        $r = Invoke-Api POST "/mask" (@{job_applicant_id="a0X000000000001"} | ConvertTo-Json) -Headers @{"X-API-Key"="wrong-key-xyz"}
        return ($r.Status -eq 401), "status=$($r.Status)"
    }
    Test-Case "POST /mask with correct X-API-Key is accepted (not 401)" {
        $r = Invoke-Api POST "/mask" (@{job_applicant_id="a0X000000000001"} | ConvertTo-Json) -Headers $authHeaders
        return ($r.Status -ne 401), "status=$($r.Status) body=$($r.Raw)"
    }
}

if ($AdminApiKey) {
    Test-Case "GET /admin/clients with correct X-Admin-API-Key succeeds" {
        $r = Invoke-Api GET "/admin/clients" -Headers $adminHeaders
        return ($r.Status -eq 200 -and $r.Json.status -eq "ok"), "status=$($r.Status) clients=$($r.Json.clients.Count)"
    }
}

# ── 3. /mask/inline core functionality (no Salesforce needed) ──────────────
Test-Case "/mask/inline: valid PDF + mask_strings redacts and returns masked PDF" {
    $r = Invoke-Api POST "/mask/inline" (@{resume_base64=$samplePdfB64; mask_strings=@("Regression Tester","+1 555 010 0111","regress@example.com")} | ConvertTo-Json) -Headers $authHeaders
    $ok = $r.Status -eq 200 -and $r.Json.status -eq "ok" -and $r.Json.redacted_regions -gt 0 -and $r.Json.masked_pdf_base64
    return $ok, "status=$($r.Status) body_status=$($r.Json.status) redacted=$($r.Json.redacted_regions) detail=$($r.Json.detail)"
}

Test-Case "/mask/inline: no mask_strings falls back to regex PII detection" {
    $r = Invoke-Api POST "/mask/inline" (@{resume_base64=$samplePdfB64} | ConvertTo-Json) -Headers $authHeaders
    $ok = $r.Status -eq 200 -and $r.Json.status -eq "ok" -and $r.Json.redacted_regions -gt 0
    return $ok, "status=$($r.Status) body_status=$($r.Json.status) redacted=$($r.Json.redacted_regions) detail=$($r.Json.detail)"
}

Test-Case "/mask/inline: invalid base64 resume returns clean error, not 500" {
    $r = Invoke-Api POST "/mask/inline" (@{resume_base64="not-valid-base64!!!"} | ConvertTo-Json) -Headers $authHeaders
    $ok = $r.Status -eq 200 -and $r.Json.status -eq "error" -and $r.Json.detail -like "*base64*"
    return $ok, "status=$($r.Status) detail=$($r.Json.detail)"
}

Test-Case "/mask/inline: valid base64 but not a PDF returns clean error, not 500" {
    $garbageB64 = [Convert]::ToBase64String([System.Text.Encoding]::ASCII.GetBytes("this is not a pdf"))
    $r = Invoke-Api POST "/mask/inline" (@{resume_base64=$garbageB64; mask_strings=@("x")} | ConvertTo-Json) -Headers $authHeaders
    $ok = $r.Status -eq 200 -and $r.Json.status -eq "error"
    return $ok, "status=$($r.Status) detail=$($r.Json.detail)"
}

Test-Case "/mask/inline: inline watermark_base64 is honored (watermark_used=image:inline_base64)" {
    $r = Invoke-Api POST "/mask/inline" (@{resume_base64=$samplePdfB64; mask_strings=@("Regression Tester"); watermark_base64=$tinyPngB64} | ConvertTo-Json) -Headers $authHeaders
    $ok = $r.Status -eq 200 -and $r.Json.watermark_used -eq "image:inline_base64"
    return $ok, "status=$($r.Status) watermark_used=$($r.Json.watermark_used) detail=$($r.Json.detail)"
}

Test-Case "/mask/inline: invalid watermark_base64 returns clean error" {
    $r = Invoke-Api POST "/mask/inline" (@{resume_base64=$samplePdfB64; mask_strings=@("x"); watermark_base64="!!!not-base64"} | ConvertTo-Json) -Headers $authHeaders
    $ok = $r.Status -eq 200 -and $r.Json.status -eq "error" -and $r.Json.detail -like "*base64*"
    return $ok, "status=$($r.Status) detail=$($r.Json.detail)"
}

# ── 4. /mask (Salesforce-backed) ────────────────────────────────────────
Test-Case "/mask: SOQL-injection-shaped job_applicant_id is rejected cleanly (no 500)" {
    $r = Invoke-Api POST "/mask" (@{job_applicant_id="x' OR Id != '"; mask_strings=@("x")} | ConvertTo-Json) -Headers $authHeaders
    $ok = $r.Status -eq 200 -and $r.Json.status -eq "error" -and $r.Json.detail -like "*nvalid*"
    return $ok, "status=$($r.Status) detail=$($r.Json.detail)"
}

Test-Case "/mask: well-formed but nonexistent job_applicant_id -> 'No resume found' (proves live SF auth works)" {
    $r = Invoke-Api POST "/mask" (@{job_applicant_id="a0X000000000999"} | ConvertTo-Json) -Headers $authHeaders
    $ok = $r.Status -eq 200 -and $r.Json.status -eq "error"
    $isAuthProof = $r.Json.detail -notlike "*not configured*"
    return ($ok -and $isAuthProof), "status=$($r.Status) detail=$($r.Json.detail)"
}

Test-Case "/mask: unknown client_key returns specific UnknownClientError (GAP: currently returns generic 'not configured')" {
    # Known gap as of this writing: server.py's mask_endpoint() calls
    # sf_client.creds_configured(client_key=...) BEFORE the try/except that
    # catches UnknownClientError. creds_configured() internally swallows
    # UnknownClientError into a plain bool False, so the generic "Salesforce
    # credentials not configured." fires first and the specific "Unknown
    # client_key '...'. Configured: [...]" message (documented in API.md /
    # SALESFORCE_INTEGRATION.md's error table) is never reached for a real
    # unregistered client_key. This assertion is expected to FAIL until
    # server.py is fixed -- kept as a regression marker, not silently softened.
    $r = Invoke-Api POST "/mask" (@{job_applicant_id="a0X000000000999"; client_key="totally-unregistered-key"} | ConvertTo-Json) -Headers $authHeaders
    $ok = $r.Status -eq 200 -and $r.Json.status -eq "error" -and $r.Json.detail -like "*nknown*client_key*"
    return $ok, "status=$($r.Status) detail=$($r.Json.detail)"
}

# ── 5. /mask/batch ───────────────────────────────────────────────────────
Test-Case "/mask/batch: empty items array is rejected with 422" {
    $r = Invoke-Api POST "/mask/batch" (@{items=@()} | ConvertTo-Json) -Headers $authHeaders
    return ($r.Status -eq 422), "status=$($r.Status)"
}

Test-Case "/mask/batch: partial failure does not abort the batch" {
    $body = @{items=@(
        @{job_applicant_id="a0X000000000998"},
        @{job_applicant_id="a0X000000000999"}
    )} | ConvertTo-Json
    $r = Invoke-Api POST "/mask/batch" $body -Headers $authHeaders
    $ok = $r.Status -eq 200 -and $r.Json.status -eq "ok" -and $r.Json.results.Count -eq 2
    return $ok, "status=$($r.Status) succeeded=$($r.Json.succeeded) failed=$($r.Json.failed)"
}

Test-Case "/mask/batch: over 200 items rejected with 422" {
    $items = 1..201 | ForEach-Object { @{job_applicant_id="a0X0000000000$_"} }
    $body = @{items=$items} | ConvertTo-Json -Depth 5
    $r = Invoke-Api POST "/mask/batch" $body -Headers $authHeaders
    return ($r.Status -eq 422), "status=$($r.Status)"
}

# ── 6. /watermark/upload ────────────────────────────────────────────────
Test-Case "/watermark/upload: non-image content is rejected" {
    $boundary = [System.Guid]::NewGuid().ToString()
    $bodyLines = @(
        "--$boundary",
        'Content-Disposition: form-data; name="account_id"',
        "",
        "001000000000000AAA",
        "--$boundary",
        'Content-Disposition: form-data; name="file"; filename="not-an-image.txt"',
        "Content-Type: text/plain",
        "",
        "this is not an image",
        "--$boundary--",
        ""
    )
    $bodyStr = $bodyLines -join "`r`n"
    $headers = $authHeaders.Clone()
    $r = $null
    try {
        $resp = Invoke-WebRequest -Uri "$BaseUrl/watermark/upload" -Method POST -Headers $headers `
            -ContentType "multipart/form-data; boundary=$boundary" -Body $bodyStr -TimeoutSec 30 -UseBasicParsing
        $r = @{ Status = [int]$resp.StatusCode; Json = try { $resp.Content | ConvertFrom-Json } catch { $null } }
    } catch {
        if ($_.Exception.Response) {
            $webResp = $_.Exception.Response
            $status = [int]$webResp.StatusCode
            $stream = $webResp.GetResponseStream()
            $reader = New-Object System.IO.StreamReader($stream)
            $raw = $reader.ReadToEnd()
            $reader.Close()
            $r = @{ Status = $status; Json = try { $raw | ConvertFrom-Json } catch { $null } }
        } else {
            return $false, "EXCEPTION: $($_.Exception.Message)"
        }
    }
    $ok = $r.Status -eq 200 -and $r.Json.status -eq "error" -and $r.Json.detail -like "*PNG*JPEG*"
    return $ok, "status=$($r.Status) detail=$($r.Json.detail)"
}

# ── 7. /clients/self-register (mutating -- cleaned up via admin API if AdminApiKey given) ──
$testClientKey = "00D000000E2ETEST01"
Test-Case "/clients/self-register: create succeeds (or reports Postgres not configured)" {
    $body = @{
        client_key=$testClientKey; client_id="e2e-test-id"; client_secret="e2e-test-secret";
        token_url="https://e2e-test.my.salesforce.com/services/oauth2/token";
        instance_url="https://e2e-test.my.salesforce.com"
    } | ConvertTo-Json
    $r = Invoke-Api POST "/clients/self-register" $body -Headers $authHeaders
    if ($r.Json.status -eq "ok") { return $true, "registered $testClientKey" }
    if ($r.Json.detail -like "*already registered*") { return $true, "already present from a prior run (idempotent-ish) -- $($r.Json.detail)" }
    return $false, "status=$($r.Status) detail=$($r.Json.detail)"
}

Test-Case "/clients/self-register: duplicate registration is rejected (create-only)" {
    $body = @{
        client_key=$testClientKey; client_id="e2e-test-id-2"; client_secret="e2e-test-secret-2";
        token_url="https://e2e-test.my.salesforce.com/services/oauth2/token";
        instance_url="https://e2e-test.my.salesforce.com"
    } | ConvertTo-Json
    $r = Invoke-Api POST "/clients/self-register" $body -Headers $authHeaders
    $ok = $r.Status -eq 200 -and $r.Json.status -eq "error" -and $r.Json.detail -like "*already registered*"
    return $ok, "status=$($r.Status) detail=$($r.Json.detail)"
}

if ($AdminApiKey) {
    Test-Case "cleanup: DELETE the e2e test client registered above" {
        $r = Invoke-Api DELETE "/admin/clients/$testClientKey" -Headers $adminHeaders
        $ok = $r.Status -eq 200 -and $r.Json.status -eq "ok"
        return $ok, "status=$($r.Status) detail=$($r.Json.detail)"
    }
} else {
    Write-Output "[skip cleanup] No -AdminApiKey supplied -- '$testClientKey' left registered in the live DB. Re-run with -AdminApiKey to clean it up, or remove it manually via PUT/DELETE /admin/clients/$testClientKey."
}

# ── Report ───────────────────────────────────────────────────────────────
Write-Output ""
Write-Output "== Results =="
$results | ForEach-Object {
    $mark = if ($_.Pass) { "PASS" } else { "FAIL" }
    Write-Output ("[{0}] {1}" -f $mark, $_.Name)
    Write-Output ("       {0}" -f $_.Detail)
}
$passed = ($results | Where-Object Pass).Count
$total = $results.Count
Write-Output ""
Write-Output "== $passed / $total passed =="
if ($passed -ne $total) { exit 1 }
