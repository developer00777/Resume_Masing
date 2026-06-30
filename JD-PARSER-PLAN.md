# JD Parser — Outlook → structured fields → Salesforce ATS (ponytail plan)

> New client job openings land as **emails in the team's Outlook mailbox**. The moment one arrives,
> capture it, parse the JD into structured fields, and create the role in the **Salesforce ATS**
> (same org as the resume-masking service). This is the *pipe*; the *parser* may already exist.

## The one decision that gates everything (talk to Yogi first)

Yogi already built a **DeepSeek-V3 JD parser inside Salesforce** that's "accurate." Don't rebuild it.

- **If his parser takes raw JD text → Job fields** (callable as an Apex REST endpoint or invocable):
  our service just forwards the email body to it. We build ONLY the Outlook→SF pipe. ← preferred, least code.
- **If his parser is form-bound** (only works from a Salesforce UI form, can't take arbitrary text):
  we add a thin external parser (OpenRouter/DeepSeek, one structured-output call) in this service and
  write the fields straight to the Job object ourselves.

**Need from Yogi:** his parser's input/output contract (does it accept raw text? what's the callable
name/endpoint? what fields does it emit, with API names?).

## Architecture (the lazy correct shape)

```
Client emails a JD  →  Team Outlook mailbox
        │
        │  [1] Microsoft Graph change-notification subscription on the mailbox
        ▼        (one subscription, auto-renewed; NOT polling)
  POST /jd/inbound   ← new endpoint on the SAME Railway FastAPI service (sibling to /mask)
        │
        │  [2] Graph webhook fires with the message id → service GETs the message body (Mail.Read)
        ▼
   parse JD → fields
        │   ├─ Yogi's SF parser (Apex REST)  ──┐  pick ONE per the decision above
        │   └─ thin OpenRouter/DeepSeek call ──┘  (structured JSON schema = the Job field list)
        ▼
   [3] simple-salesforce → create Job__c (Composite REST), reusing app/sf_client.py's auth
        ▼
   Job shows up on the Jobs Lightning page. 2-way later (status back to client) is Phase 2.
```

Reuses what already exists: the Railway service, `sf_client.connect()` (same Connected App creds),
the deploy pipeline. The only NEW pieces are the Graph subscription + the `/jd/inbound` handler + the
field-mapping.

## Microsoft Graph wiring (the genuinely new part)

1. **App registration** (Azure AD): client id + secret + tenant id. Application permission
   **`Mail.Read`** (+ `Mail.ReadBasic.All` if a shared mailbox), **admin-consented**. App-only auth
   (client-credentials) — no user sign-in, it's a daemon.
2. **Subscription**: `POST /subscriptions` with `resource: "users/{mailbox}/mailMessages"`,
   `changeType: "created"`, `notificationUrl: https://<railway>/jd/inbound`. Graph sends a validation
   token on creation — echo it back (handshake). Subscriptions expire (~3 days) → a `pg_cron`/Railway
   cron renews it. ponytail: one cron, one row of state.
3. **On notification**: Graph posts `{ resourceData.id }`. Service does
   `GET /users/{mailbox}/messages/{id}` for subject + body, filters to JD emails (sender allowlist or
   subject rule), then parses.

Secrets live in **Railway env** (never in code): `MS_TENANT_ID`, `MS_CLIENT_ID`, `MS_CLIENT_SECRET`,
`JD_MAILBOX`, `JD_SENDER_ALLOWLIST`. Same secret-store discipline as the SF creds.

## Salesforce side

- **Job object API name + field list** (e.g. `Job__c`: Title, Client, Location, Skills, Experience,
  Salary band, JD text…) with API names + types. This list *is* the LLM's output schema — exact match =
  no over/under-extraction (same principle as the masking service's exact `mask_strings`).
- Create via Composite REST (`sf.Job__c.create({...})`), de-dupe on (client + title + received-date) so a
  re-sent email doesn't double-create.

## Build order

1. **Gate:** Yogi's parser contract + the `Job__c` field list. (No code until these land.)
2. `/jd/inbound` endpoint: Graph validation handshake + message fetch + sender/subject filter. Unit-test
   the handshake and the filter offline (fixture payloads — no Graph account needed), mirroring how the
   voice-bot/masking suites self-check.
3. Parser adapter: Yogi-passthrough OR thin DeepSeek structured call (whichever the gate decides).
4. `Job__c` create + de-dupe.
5. Graph subscription create + cron renew.
6. Deploy to Railway; point a test subscription at a staging mailbox; email a sample JD; watch the Job
   appear.

## Credentials checklist (what to collect)

| For | Need |
|-----|------|
| Microsoft Graph | Azure app: tenant id, client id, client secret; `Mail.Read` app permission + admin consent; the shared mailbox address; a JD-sender allowlist |
| Salesforce | `Job__c` (or actual object) API name + field API names/types; reuse the existing Connected App creds in `.env` |
| Parser | Yogi's parser I/O contract (raw-text input? callable endpoint? emitted fields) — the gate |
| Samples | 2-3 real client JD emails (for the filter + field-mapping) |
```
