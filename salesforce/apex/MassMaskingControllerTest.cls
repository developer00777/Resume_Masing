/**
 * Tests for MassMaskingController.
 *
 * Production deployment needs >=75% coverage, so this covers the parts that
 * carry the risk: the JSON payload actually sent to the service, and every
 * branch of response handling (success, per-item failure, HTTP error,
 * unparseable body, batch-level error, callout exception).
 *
 * Deliberately does NO DML. SCSCHAMPS__Job_Applicant__c is a managed-package
 * object whose required fields we cannot know from outside the org, so a
 * fixture that inserts one would be a coin flip on whether this class deploys
 * at all. Synthetic Ids give the same coverage of collectPii()'s query path
 * without that risk.
 *
 * WORTH ADDING once you have confirmed the required fields on
 * SCSCHAMPS__Job_Applicant__c in this org: a test that inserts a Contact with
 * a name/phone/email plus a Job Applicant pointing at it, then asserts
 * collectPii() returns those three values in maskStrings. That is the one
 * branch synthetic Ids cannot reach, because it needs a row to come back from
 * the query.
 */
@IsTest
private class MassMaskingControllerTest {

    private static Integer idCounter = 0;

    /** A syntactically valid Id that matches no real record. */
    private static Id fakeId(Schema.SObjectType sot) {
        idCounter++;
        String suffix = String.valueOf(idCounter);
        return Id.valueOf(sot.getDescribe().getKeyPrefix()
                          + '000000000000'.substring(0, 12 - suffix.length())
                          + suffix);
    }

    private static MassMaskingController.CandidatePii candidate(String jaId, String acctId,
                                                                List<String> maskStrings) {
        MassMaskingController.CandidatePii p = new MassMaskingController.CandidatePii();
        p.jobApplicantId = jaId;
        p.accountId = acctId;
        p.maskStrings = maskStrings;
        return p;
    }

    private class Mock implements HttpCalloutMock {
        private Integer code;
        private String body;
        @TestVisible String lastRequestBody;

        Mock(Integer code, String body) {
            this.code = code;
            this.body = body;
        }

        public HttpResponse respond(HttpRequest req) {
            lastRequestBody = req.getBody();
            System.assertEquals('POST', req.getMethod(), 'must POST');
            System.assert(req.getEndpoint().endsWith('/mask/batch'),
                'must target the batch endpoint, got ' + req.getEndpoint());

            HttpResponse res = new HttpResponse();
            res.setStatusCode(code);
            res.setBody(body);
            return res;
        }
    }

    private class ThrowingMock implements HttpCalloutMock {
        public HttpResponse respond(HttpRequest req) {
            throw new CalloutException('Read timed out');
        }
    }

    private static HttpResponse response(Integer code, String body) {
        HttpResponse res = new HttpResponse();
        res.setStatusCode(code);
        res.setBody(body);
        return res;
    }


    // --- payload -----------------------------------------------------------

    @IsTest
    static void payloadCarriesNamePhoneAndEmail() {
        String jaId = fakeId(SCSCHAMPS__Job_Applicant__c.SObjectType);
        String acctId = fakeId(Account.SObjectType);

        String body = MassMaskingController.batchBody(new List<MassMaskingController.CandidatePii>{
            candidate(jaId, acctId,
                      new List<String>{ 'Rahul Sharma', '+919876543210', 'rahul@example.com' })
        });

        Map<String, Object> parsed = (Map<String, Object>) JSON.deserializeUntyped(body);
        System.assertEquals(UserInfo.getOrganizationId(), parsed.get('client_key'),
            'client_key must be this org Id -- that is how the service keys credentials');

        List<Object> items = (List<Object>) parsed.get('items');
        System.assertEquals(1, items.size());
        Map<String, Object> item = (Map<String, Object>) items[0];
        System.assertEquals(jaId, item.get('job_applicant_id'));
        System.assertEquals(acctId, item.get('account_id'),
            'account_id lets the service pick the client watermark without calling back');

        List<Object> maskStrings = (List<Object>) item.get('mask_strings');
        System.assertEquals(3, maskStrings.size(), 'name, phone and email must all be sent');
        System.assertEquals('Rahul Sharma', maskStrings[0]);
        System.assertEquals('+919876543210', maskStrings[1]);
        System.assertEquals('rahul@example.com', maskStrings[2]);
    }

    @IsTest
    static void payloadOmitsMaskStringsWhenThereIsNoContactPii() {
        // The service treats a missing mask_strings as "resolve the Contact
        // yourself", which is the behaviour we want when this org found
        // nothing. Sending an empty array would mean the same thing, but
        // omitting it makes the intent unambiguous.
        String body = MassMaskingController.batchBody(new List<MassMaskingController.CandidatePii>{
            candidate(fakeId(SCSCHAMPS__Job_Applicant__c.SObjectType), null, new List<String>())
        });

        Map<String, Object> item = (Map<String, Object>)
            ((List<Object>) ((Map<String, Object>) JSON.deserializeUntyped(body)).get('items'))[0];
        System.assert(!item.containsKey('mask_strings'), 'mask_strings should be absent');
        System.assert(!item.containsKey('account_id'), 'account_id should be absent when blank');
    }

    @IsTest
    static void payloadEscapesQuotesInPii() {
        // A name like O'Brien, or any value with a quote, must not be able to
        // break the JSON body -- this is why batchBody uses JSONGenerator
        // rather than string concatenation.
        String body = MassMaskingController.batchBody(new List<MassMaskingController.CandidatePii>{
            candidate(fakeId(SCSCHAMPS__Job_Applicant__c.SObjectType), null,
                      new List<String>{ 'Sean O"Brien \\ Jr' })
        });

        Map<String, Object> item = (Map<String, Object>)
            ((List<Object>) ((Map<String, Object>) JSON.deserializeUntyped(body)).get('items'))[0];
        List<Object> maskStrings = (List<Object>) item.get('mask_strings');
        System.assertEquals('Sean O"Brien \\ Jr', maskStrings[0], 'value must round-trip intact');
    }


    // --- response handling -------------------------------------------------

    @IsTest
    static void parsesMixedSuccessAndFailure() {
        String jaOk = fakeId(SCSCHAMPS__Job_Applicant__c.SObjectType);
        String jaBad = fakeId(SCSCHAMPS__Job_Applicant__c.SObjectType);

        String json =
            '{"status":"ok","succeeded":1,"failed":1,"results":['
            + '{"job_applicant_id":"' + jaOk + '","result":{"status":"ok",'
            + '"masked_content_version_id":"068000000000001AAA","redacted_regions":3,'
            + '"watermark_used":"image:global","detail":null,"job_applicant_name":"JA-26469"}},'
            + '{"job_applicant_id":"' + jaBad + '","result":{"status":"error",'
            + '"detail":"No resume found for this Job Applicant.","job_applicant_name":"JA-26470"}}'
            + ']}';

        List<MassMaskingController.MaskOutcome> outcomes =
            MassMaskingController.parseBatchResponse(
                response(200, json),
                new List<MassMaskingController.CandidatePii>{
                    candidate(jaOk, null, new List<String>{ 'A' }),
                    candidate(jaBad, null, new List<String>{ 'B' })
                });

        System.assertEquals(2, outcomes.size());

        System.assertEquals('ok', outcomes[0].status);
        System.assertEquals('068000000000001AAA', outcomes[0].maskedContentVersionId);
        System.assertEquals(3, outcomes[0].redactedRegions);
        System.assertEquals('JA-26469', outcomes[0].jobApplicantName);

        System.assertEquals('error', outcomes[1].status);
        System.assertEquals('No resume found for this Job Applicant.', outcomes[1].detail);
        System.assertEquals(null, outcomes[1].redactedRegions,
            'redacted_regions is absent on an error row and must stay null');
    }

    @IsTest
    static void httpErrorFailsTheChunkPerRecordRatherThanThrowing() {
        // A 401 means the Named Credential is not sending X-API-Key. Reported
        // per record so a bad chunk does not discard the chunks that worked.
        List<MassMaskingController.CandidatePii> chunk =
            new List<MassMaskingController.CandidatePii>{
                candidate(fakeId(SCSCHAMPS__Job_Applicant__c.SObjectType), null, new List<String>{ 'A' }),
                candidate(fakeId(SCSCHAMPS__Job_Applicant__c.SObjectType), null, new List<String>{ 'B' })
            };

        List<MassMaskingController.MaskOutcome> outcomes =
            MassMaskingController.parseBatchResponse(
                response(401, '{"detail":"Missing or invalid X-API-Key."}'), chunk);

        System.assertEquals(2, outcomes.size(), 'every record in the chunk must be reported');
        for (MassMaskingController.MaskOutcome m : outcomes) {
            System.assertEquals('error', m.status);
            System.assert(m.detail.contains('401'), 'detail should name the status code: ' + m.detail);
        }
    }

    @IsTest
    static void batchLevelErrorWithoutResultsFailsEveryRecord() {
        List<MassMaskingController.CandidatePii> chunk =
            new List<MassMaskingController.CandidatePii>{
                candidate(fakeId(SCSCHAMPS__Job_Applicant__c.SObjectType), null, new List<String>{ 'A' })
            };

        List<MassMaskingController.MaskOutcome> outcomes =
            MassMaskingController.parseBatchResponse(
                response(200, '{"status":"error","detail":"Unknown client_key."}'), chunk);

        System.assertEquals(1, outcomes.size());
        System.assertEquals('error', outcomes[0].status);
        System.assertEquals('Unknown client_key.', outcomes[0].detail);
    }

    @IsTest
    static void unparseableBodyFailsEveryRecord() {
        List<MassMaskingController.CandidatePii> chunk =
            new List<MassMaskingController.CandidatePii>{
                candidate(fakeId(SCSCHAMPS__Job_Applicant__c.SObjectType), null, new List<String>{ 'A' })
            };

        List<MassMaskingController.MaskOutcome> outcomes =
            MassMaskingController.parseBatchResponse(
                response(200, '<html>502 Bad Gateway</html>'), chunk);

        System.assertEquals('error', outcomes[0].status);
        System.assert(outcomes[0].detail.contains('Unreadable response'), outcomes[0].detail);
    }


    // --- callout path ------------------------------------------------------

    @IsTest
    static void sendChunkPostsToBatchEndpoint() {
        String jaId = fakeId(SCSCHAMPS__Job_Applicant__c.SObjectType);
        Mock mock = new Mock(200,
            '{"status":"ok","succeeded":1,"failed":0,"results":['
            + '{"job_applicant_id":"' + jaId + '","result":{"status":"ok",'
            + '"masked_content_version_id":"068000000000001AAA","redacted_regions":3,'
            + '"job_applicant_name":"JA-1"}}]}');
        Test.setMock(HttpCalloutMock.class, mock);

        Test.startTest();
        List<MassMaskingController.MaskOutcome> outcomes =
            MassMaskingController.sendChunk(new List<MassMaskingController.CandidatePii>{
                candidate(jaId, null, new List<String>{ 'Rahul Sharma', 'rahul@example.com' })
            });
        Test.stopTest();

        System.assertEquals(1, outcomes.size());
        System.assertEquals('ok', outcomes[0].status);
        System.assert(mock.lastRequestBody.contains('Rahul Sharma'),
            'the candidate name must reach the service');
        System.assert(mock.lastRequestBody.contains('rahul@example.com'),
            'the candidate email must reach the service');
    }

    @IsTest
    static void calloutExceptionIsReportedNotThrown() {
        Test.setMock(HttpCalloutMock.class, new ThrowingMock());

        Test.startTest();
        List<MassMaskingController.MaskOutcome> outcomes =
            MassMaskingController.sendChunk(new List<MassMaskingController.CandidatePii>{
                candidate(fakeId(SCSCHAMPS__Job_Applicant__c.SObjectType), null, new List<String>{ 'A' })
            });
        Test.stopTest();

        System.assertEquals('error', outcomes[0].status);
        System.assert(outcomes[0].detail.contains('Callout failed'), outcomes[0].detail);
    }


    // --- guards and schema resolution --------------------------------------

    @IsTest
    static void maskSelectedRejectsEmptySelection() {
        try {
            MassMaskingController.maskSelected(new List<String>());
            System.assert(false, 'should have thrown');
        } catch (AuraHandledException e) {
            // expected
        }
    }

    @IsTest
    static void maskSelectedRejectsMoreThanTheSynchronousLimit() {
        // Guards Salesforce's 120s cumulative callout limit per transaction --
        // the real ceiling on a synchronous run, well below the service's own
        // 200-item batch cap.
        List<String> tooMany = new List<String>();
        for (Integer i = 0; i < 50; i++) {
            tooMany.add(fakeId(SCSCHAMPS__Job_Applicant__c.SObjectType));
        }
        try {
            MassMaskingController.maskSelected(tooMany);
            System.assert(false, 'should have thrown');
        } catch (AuraHandledException e) {
            // expected
        }
    }

    @IsTest
    static void unknownAndInaccessibleFieldsAreSkipped() {
        // PhoneNumber__c is org-specific, so every PII field is reached
        // dynamically; a field this org does not have must drop out silently
        // instead of breaking the class.
        List<String> resolved = MassMaskingController.existingFields(
            Contact.SObjectType,
            new List<String>{ 'Email', 'No_Such_Field_Xyz__c' });

        System.assertEquals(new List<String>{ 'Email' }, resolved,
            'a field absent from the org must be skipped, not queried');
    }

    @IsTest
    static void collectPiiOnUnknownIdsReturnsNothing() {
        List<MassMaskingController.CandidatePii> pii = MassMaskingController.collectPii(
            new List<String>{ fakeId(SCSCHAMPS__Job_Applicant__c.SObjectType) });
        System.assertEquals(0, pii.size(), 'no matching Job Applicant means nothing to mask');
    }

    @IsTest
    static void enqueueMaskingSchedulesTheJob() {
        Test.setMock(HttpCalloutMock.class, new Mock(200, '{"status":"ok","results":[]}'));

        Test.startTest();
        Id jobId = MassMaskingController.enqueueMasking(
            new List<String>{ fakeId(SCSCHAMPS__Job_Applicant__c.SObjectType) });
        Test.stopTest();

        System.assertNotEquals(null, jobId, 'a Queueable job Id should come back');
    }

    @IsTest
    static void enqueueMaskingRejectsEmptySelection() {
        try {
            MassMaskingController.enqueueMasking(null);
            System.assert(false, 'should have thrown');
        } catch (AuraHandledException e) {
            // expected
        }
    }


    // --- existing entry points still work ----------------------------------

    @IsTest
    static void existingEntryPointsStillRun() {
        Test.startTest();
        List<SCSCHAMPS__Job_Applicant__c> rows = MassMaskingController.getJobApplicants(null);
        Map<String, String> info = MassMaskingController.generatemassmasking(
            new List<String>{ fakeId(SCSCHAMPS__Job_Applicant__c.SObjectType) });
        Test.stopTest();

        System.assertNotEquals(null, rows);
        System.assertEquals(UserInfo.getUserName(), info.get('uname'));
    }
}
