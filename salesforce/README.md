# Salesforce side — deploying `MassMaskingController`

Apex for the masking integration. `MassMaskingController` reads the
candidate's Name / Phone / Email off the related Contact and sends them to
`POST /mask/batch` alongside each Job Applicant Id, so the service does not
have to infer them.

## You cannot edit this in production

Salesforce does not allow Apex to be created or edited in a production org —
Setup → Apex Classes is read-only there and the Developer Console refuses to
save. Apex must be authored in a sandbox or Dev org and **deployed**, and a
production deploy runs tests and requires ≥75% coverage. That is why
`MassMaskingControllerTest.cls` exists.

## 1. Named Credential (do this first)

Setup → Named Credentials → New Legacy:

| Field | Value |
|---|---|
| Label / Name | `Resume_Masking_Service` |
| URL | the service base URL (no trailing slash, no `/mask`) |
| Identity Type | Named Principal |
| Authentication Protocol | No Authentication |
| Generate Authorization Header | **unchecked** |
| Custom header | Name `X-API-Key`, Value = `MASK_API_KEY` |

The custom header is the point: it keeps the key out of visible metadata, so
it never appears in this class, in Flow config, or in a change set. The class
targets `callout:Resume_Masking_Service/mask/batch`, so it needs no Remote Site
Setting and no hardcoded host.

A 401 reported against every record in a chunk means this header is missing or
wrong — that is the first thing to check.

## 2. Deploy

Sandbox first, then production. With the Salesforce CLI (`sf`):

```bash
# sandbox
sf project deploy start -d salesforce/apex -o <sandbox-alias>
sf apex run test -o <sandbox-alias> -n MassMaskingControllerTest -w 10 -y

# production — runs the tests as part of the deploy
sf project deploy start -d salesforce/apex -o <prod-alias> -l RunSpecifiedTests -t MassMaskingControllerTest
```

Add `--dry-run` to validate against production without committing. If you'd
rather not use the CLI, deploy the same two classes by change set from the
sandbox.

## 3. What the LWC calls

| Method | Use |
|---|---|
| `maskSelected(List<String> ids)` | Synchronous, returns a `MaskOutcome` per record. Capped at 10 records. |
| `enqueueMasking(List<String> ids)` | Background (Queueable, chained). No per-record return. |
| `getJobApplicants(String jobId)` | Unchanged. |
| `generatemassmasking(List<String> ids)` | Unchanged. |

The 10-record cap on `maskSelected` is Salesforce's **120 seconds of
cumulative callout time per transaction**, not the service's batch limit (which
is 200). Each resume costs the service a fetch, an optional LibreOffice
conversion, a redact and an upload, so a synchronous run has to stay small.
`enqueueMasking` sidesteps it because every Queueable execution gets its own
120s budget; it processes 10 per execution and chains until done.

`enqueueMasking` returns no results, which matters less than it looks: on
success the service uploads the masked PDF back onto the Job Applicant as a new
`ContentVersion`, so the outcome is visible on the record. Failures are logged
at `ERROR`.

## 4. Known gap in the test class

`MassMaskingControllerTest` does no DML, because `SCSCHAMPS__Job_Applicant__c`
is a managed-package object whose required fields cannot be known from outside
the org — a fixture inserting one would be a guess that could block the deploy.

Once you have confirmed those required fields, add a test that inserts a
Contact (name + phone + email) and a Job Applicant pointing at it, then asserts
`collectPii()` returns all three in `maskStrings`. That is the one branch the
synthetic-Id tests cannot reach, since it needs a row to come back from the
query.
