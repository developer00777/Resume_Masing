"""READ-ONLY diagnostic against a live Salesforce org. No DML, no writes.

Answers two questions that cannot be answered from outside the org, and that
between them account for every remaining way masking can go wrong:

  1. Do all of sf_client._CONTACT_PII_FIELDS actually exist on this org's
     Contact? fetch_contact_pii_strings() asks for every one of them in a
     SINGLE SOQL statement wrapped in `except Exception: return []`. SOQL is
     all-or-nothing about unknown fields, so ONE missing field makes that
     query throw and the function silently return [] for EVERY candidate --
     no error, no log, just no structured PII ever. That failure mode is
     invisible in production and would leave the candidate's name unmasked
     whenever the resume text scan cannot see it either.

  2. Does any populated Contact PII field hold a value that is not actually a
     phone number or email? Values from the Contact are treated as
     authoritative and masked verbatim, so a field holding a date, a code or
     an id is the one remaining route by which something that is not PII gets
     redacted out of a resume.

Deliberately prints SHAPES, never values: candidate names, emails and phone
numbers are real PII and must not end up in a terminal transcript or a log.

Run:  python tests/diagnose_contact_pii.py
Needs SF_USERNAME / SF_PASSWORD / SF_SECURITY_TOKEN, from the environment or
from a local .env (which .gitignore already excludes).
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def load_dotenv() -> None:
    """Minimal .env reader -- avoids adding a dependency for one diagnostic."""
    path = Path(__file__).resolve().parents[1] / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def shape(value: str) -> str:
    """A value's shape with every character class collapsed, so nothing
    identifying survives: "Rahul Sharma" -> "Aaaaa Aaaaaa"."""
    out = re.sub(r"[A-Z]", "A", str(value))
    out = re.sub(r"[a-z]", "a", out)
    return re.sub(r"\d", "9", out)


def main() -> int:
    load_dotenv()
    missing_env = [v for v in ("SF_USERNAME", "SF_PASSWORD", "SF_SECURITY_TOKEN")
                   if not os.environ.get(v)]
    if missing_env:
        print(f"Set {', '.join(missing_env)} in the environment or in .env first.")
        return 2

    from simple_salesforce import Salesforce

    from app import pii
    from app.sf_client import _CONTACT_PII_FIELDS

    # "login" is production; SF_DOMAIN=Live is not a value simple-salesforce
    # understands and would build https://Live.salesforce.com.
    domain = os.environ.get("SF_DOMAIN", "login").strip() or "login"
    if domain.lower() in ("live", "prod", "production"):
        print(f"note: SF_DOMAIN={domain!r} is not valid for simple-salesforce; using 'login'")
        domain = "login"

    sf = Salesforce(username=os.environ["SF_USERNAME"],
                    password=os.environ["SF_PASSWORD"],
                    security_token=os.environ["SF_SECURITY_TOKEN"],
                    domain=domain)
    print(f"connected: {sf.sf_instance}\n")

    all_fields = ("Name",) + _CONTACT_PII_FIELDS

    # --- 1) field existence -------------------------------------------------
    present = {f["name"] for f in sf.Contact.describe()["fields"]}
    absent = [f for f in all_fields if f not in present]
    print("--- 1) _CONTACT_PII_FIELDS vs this org's Contact ---")
    for f in all_fields:
        print(f"   {'ok     ' if f in present else 'MISSING'}  {f}")

    print("\n--- does fetch_contact_pii_strings()'s own SOQL run at all? ---")
    try:
        sf.query("SELECT " + ", ".join(all_fields) + " FROM Contact LIMIT 1")
        print("   OK -- structured Contact PII is available to the service")
        soql_ok = True
    except Exception as e:
        soql_ok = False
        print("   FAILS -> fetch_contact_pii_strings() returns [] for EVERY candidate,")
        print("           silently, because of `except Exception: return []`.")
        print(f"   reason: {type(e).__name__}: {str(e)[:160]}")
        print(f"   absent fields: {absent}")

    if not soql_ok:
        print("\n   Fix: drop the absent fields from _CONTACT_PII_FIELDS, or build the")
        print("   field list from a describe() call the way the Apex side does.")
        return 1

    # --- 2) do populated values actually look like PII? ---------------------
    usable = [f for f in all_fields if f in present]
    where = " OR ".join(f"{f} != NULL" for f in usable if f != "Name")
    rows = sf.query_all(
        f"SELECT {', '.join(usable)} FROM Contact WHERE {where} LIMIT 500"
    ).get("records", [])

    print(f"\n--- 2) what {len(rows)} populated Contact records actually hold ---")
    suspicious: list[tuple[str, str, str]] = []
    counts: dict[str, int] = {}
    for rec in rows:
        for f in usable:
            value = rec.get(f)
            if not value or not str(value).strip():
                continue
            value = str(value).strip()
            kind = pii.classify(value)
            counts[f"{f}:{kind}"] = counts.get(f"{f}:{kind}", 0) + 1
            # A phone/email field whose value classifies as NAME is neither a
            # phone nor an email -- it is free text that will still be masked
            # verbatim, because Contact values are trusted.
            if f != "Name" and kind == pii.NAME:
                suspicious.append((rec.get("Id", "?"), f, value))

    for key in sorted(counts):
        print(f"   {counts[key]:5d}  {key}")

    print(f"\n--- values in a phone/email field that are neither: {len(suspicious)} ---")
    if not suspicious:
        print("   none -- no Contact field can be over-masking a resume")
    else:
        print("   (shapes only; A=uppercase a=lowercase 9=digit)")
        for cid, field, value in suspicious[:25]:
            print(f"   {cid}  {field:38s}  {shape(value)!r}")
        print("\n   These are masked verbatim, so any of them appearing in a resume")
        print("   would be redacted even though it is not PII.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
