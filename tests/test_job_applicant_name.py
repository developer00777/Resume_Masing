"""Tests for sf_client.fetch_job_applicant_name() -- the display-only Name
lookup (e.g. "JA-26469") used by the frontend results table instead of the
raw Salesforce Id. Never raises; never used for any masking operation."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import sf_client

JA_ID = "a0X000000000001"


class _ScriptedSF:
    def __init__(self, rules):
        self.rules = rules  # list of (substring, result_dict)

    def query(self, soql):
        for substr, result in self.rules:
            if substr in soql:
                return result
        return {"records": []}


class _RaisingSF:
    def query(self, soql):
        raise RuntimeError("simulated Salesforce outage")


def test_fetch_job_applicant_name_found():
    sf = _ScriptedSF([
        (f"FROM {sf_client._JOB_APPLICANT_OBJECT} WHERE Id = '{JA_ID}'", {"records": [{"Name": "JA-26469"}]}),
    ])
    assert sf_client.fetch_job_applicant_name(JA_ID, sf=sf) == "JA-26469"


def test_fetch_job_applicant_name_no_record():
    sf = _ScriptedSF([])
    assert sf_client.fetch_job_applicant_name(JA_ID, sf=sf) is None


def test_fetch_job_applicant_name_blank_name():
    sf = _ScriptedSF([
        (f"FROM {sf_client._JOB_APPLICANT_OBJECT} WHERE Id = '{JA_ID}'", {"records": [{"Name": None}]}),
    ])
    assert sf_client.fetch_job_applicant_name(JA_ID, sf=sf) is None


def test_fetch_job_applicant_name_never_raises_on_query_error():
    assert sf_client.fetch_job_applicant_name(JA_ID, sf=_RaisingSF()) is None


def test_fetch_job_applicant_name_rejects_invalid_id():
    # _safe_id() validation runs before any query -- malformed/injection-shaped
    # input is rejected the same way resolve_account_id/resolve_contact_id do it.
    assert sf_client.fetch_job_applicant_name("' OR 1=1--", sf=_ScriptedSF([])) is None
