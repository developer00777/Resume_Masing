public class MassMaskingController {
    private static final String NAMED_CREDENTIAL = 'callout:Resume_Masking_Service';

    private static final Integer CHUNK_SIZE = 5;
    private static final Integer SYNC_MAX_ITEMS = 10;
    private static final Integer CALLOUT_TIMEOUT_MS = 120000;
    private static final String WATERMARK_FALLBACK_TEXT = 'CONFIDENTIAL';

    private static final List<String> CONTACT_PII_FIELDS = new List<String>{
        'Name',
        'Phone', 'MobilePhone', 'HomePhone', 'OtherPhone',
        'PhoneNumber__c', 'SCSCHAMPS__PhoneNumber__c', 'SCSCHAMPS__AlternatePhoneNumber__c',
        'Email', 'SCSCHAMPS__AlternateEmail__c'
    };

    private static final String JA_CONTACT_FIELD = 'SCSCHAMPS__Contact_Talent__c';
    private static final String JA_ACCOUNT_FIELD = 'SCSCHAMPS__Account__c';

    @AuraEnabled(cacheable=true)
    public static List<SCSCHAMPS__Job_Applicant__c> getJobApplicants(String jobId) {
        try {
            String query = 'SELECT Id, Name, SCSCHAMPS__Job_Title__c, ' +
                           'SCSCHAMPS__Contact_Talent__c, SCSCHAMPS__Contact_Talent__r.Name ' +
                           'FROM SCSCHAMPS__Job_Applicant__c ' +
                           'WHERE SCSCHAMPS__Contact_Talent__c != NULL';

            if (String.isNotBlank(jobId)) {
                query += ' AND SCSCHAMPS__Job__c = :jobId';
            }

            query += ' ORDER BY CreatedDate DESC LIMIT 200';

            return Database.query(query);
        } catch (Exception e) {
            throw new AuraHandledException('Error: ' + e.getMessage());
        }
    }

    @AuraEnabled
    public static Map<String, String> generatemassmasking(List<String> jobAppIdList) {
        List<SCSCHAMPS__Job_Applicant__c> applicants =
            [SELECT Id, Name FROM SCSCHAMPS__Job_Applicant__c WHERE Id IN :jobAppIdList];

        List<String> ids = new List<String>();
        List<String> displayNames = new List<String>();

        for (SCSCHAMPS__Job_Applicant__c ja : applicants) {
            String displayName = ja.Name;

            if (String.isBlank(displayName) || !displayName.startsWith('JA-')) {
                String idStr = String.valueOf(ja.Id);
                displayName = 'JA-' + idStr.substring(idStr.length() - 4);
            }

            ids.add(ja.Id);
            displayNames.add(displayName);
        }

        return new Map<String, String>{
            'ids'          => String.join(ids, ';'),
            'displayNames' => String.join(displayNames, ','),
            'orgUrl'       => URL.getOrgDomainUrl().toExternalForm()
                              + '/services/Soap/c/59.0/' + UserInfo.getOrganizationId(),
            'uname'        => UserInfo.getUserName()
        };
    }

    public class CandidatePii {
        public String jobApplicantId;
        public String accountId;
        public List<String> maskStrings = new List<String>();
    }

    public class MaskOutcome {
        @AuraEnabled public String  jobApplicantId;
        @AuraEnabled public String  jobApplicantName;
        @AuraEnabled public String  status;
        @AuraEnabled public String  maskedContentVersionId;
        @AuraEnabled public Integer redactedRegions;
        @AuraEnabled public String  detail;
    }

    @AuraEnabled
    public static List<MaskOutcome> maskSelected(List<String> jobAppIdList) {
        if (jobAppIdList == null || jobAppIdList.isEmpty()) {
            throw new AuraHandledException('Select at least one Job Applicant to mask.');
        }
        if (jobAppIdList.size() > SYNC_MAX_ITEMS) {
            throw new AuraHandledException(
                'Selected ' + jobAppIdList.size() + ' Job Applicants. Salesforce allows only 120 '
                + 'seconds of callout time per transaction, so more than ' + SYNC_MAX_ITEMS
                + ' at a time has to run in the background -- use enqueueMasking() instead.');
        }
        return run(jobAppIdList);
    }

    @AuraEnabled
    public static Id enqueueMasking(List<String> jobAppIdList) {
        if (jobAppIdList == null || jobAppIdList.isEmpty()) {
            throw new AuraHandledException('Select at least one Job Applicant to mask.');
        }
        return System.enqueueJob(new MaskQueueable(new List<String>(jobAppIdList)));
    }

    public class MaskQueueable implements Queueable, Database.AllowsCallouts {
        private List<String> remaining;

        public MaskQueueable(List<String> remaining) {
            this.remaining = remaining;
        }

        public void execute(QueueableContext ctx) {
            List<String> slice = new List<String>();
            while (!remaining.isEmpty() && slice.size() < SYNC_MAX_ITEMS) {
                slice.add(remaining.remove(0));
            }

            for (MaskOutcome m : run(slice)) {
                if (m.status != 'ok') {
                    System.debug(LoggingLevel.ERROR,
                        'Masking failed for ' + m.jobApplicantId + ': ' + m.detail);
                }
            }

            if (!remaining.isEmpty() && !Test.isRunningTest()) {
                System.enqueueJob(new MaskQueueable(remaining));
            }
        }
    }

    private static List<MaskOutcome> run(List<String> jobAppIdList) {
        List<CandidatePii> pii = collectPii(jobAppIdList);
        List<MaskOutcome> outcomes = new List<MaskOutcome>();

        for (Integer i = 0; i < pii.size(); i += CHUNK_SIZE) {
            List<CandidatePii> chunk = new List<CandidatePii>();
            for (Integer j = i; j < Math.min(i + CHUNK_SIZE, pii.size()); j++) {
                chunk.add(pii[j]);
            }
            outcomes.addAll(sendChunk(chunk));
        }
        return outcomes;
    }

    @TestVisible
    private static List<String> existingFields(Schema.SObjectType sot, List<String> wanted) {
        Map<String, Schema.SObjectField> fieldMap = sot.getDescribe().fields.getMap();
        List<String> out = new List<String>();
        for (String f : wanted) {
            Schema.SObjectField token = fieldMap.get(f.toLowerCase());
            if (token != null && token.getDescribe().isAccessible()) {
                out.add(f);
            }
        }
        return out;
    }

    @TestVisible
    private static List<CandidatePii> collectPii(List<String> jobAppIdList) {
        List<String> jaFields = existingFields(
            SCSCHAMPS__Job_Applicant__c.SObjectType,
            new List<String>{ JA_CONTACT_FIELD, JA_ACCOUNT_FIELD });

        Boolean hasContactField = jaFields.contains(JA_CONTACT_FIELD);
        Boolean hasAccountField = jaFields.contains(JA_ACCOUNT_FIELD);

        String jaSoql = 'SELECT Id'
            + (jaFields.isEmpty() ? '' : ', ' + String.join(jaFields, ', '))
            + ' FROM SCSCHAMPS__Job_Applicant__c WHERE Id IN :jobAppIdList';
        List<SObject> applicants = Database.query(jaSoql);

        Set<Id> contactIds = new Set<Id>();
        if (hasContactField) {
            for (SObject ja : applicants) {
                Object cid = ja.get(JA_CONTACT_FIELD);
                if (cid != null) {
                    contactIds.add((Id) cid);
                }
            }
        }

        List<String> piiFields = existingFields(Contact.SObjectType, CONTACT_PII_FIELDS);
        Map<Id, SObject> contactById = new Map<Id, SObject>();
        if (!contactIds.isEmpty() && !piiFields.isEmpty()
                && Contact.SObjectType.getDescribe().isAccessible()) {
            String soql = 'SELECT Id, ' + String.join(piiFields, ', ')
                        + ' FROM Contact WHERE Id IN :contactIds';
            for (SObject c : Database.query(soql)) {
                contactById.put((Id) c.get('Id'), c);
            }
        }

        List<CandidatePii> out = new List<CandidatePii>();
        for (SObject ja : applicants) {
            CandidatePii p = new CandidatePii();
            p.jobApplicantId = String.valueOf(ja.get('Id'));

            if (hasAccountField && ja.get(JA_ACCOUNT_FIELD) != null) {
                p.accountId = String.valueOf(ja.get(JA_ACCOUNT_FIELD));
            }

            SObject c = (hasContactField && ja.get(JA_CONTACT_FIELD) != null)
                ? contactById.get((Id) ja.get(JA_CONTACT_FIELD))
                : null;

            if (c != null) {
                Set<String> seen = new Set<String>();
                for (String f : piiFields) {
                    Object v = c.get(f);
                    String s = (v == null) ? null : String.valueOf(v).trim();
                    if (String.isBlank(s) || seen.contains(s)) {
                        continue;
                    }
                    seen.add(s);
                    p.maskStrings.add(s);
                }
            }
            out.add(p);
        }
        return out;
    }

    @TestVisible
    private static String batchBody(List<CandidatePii> chunk) {
        JSONGenerator g = JSON.createGenerator(false);
        g.writeStartObject();
        g.writeStringField('client_key', UserInfo.getOrganizationId());
        g.writeStringField('watermark_text', WATERMARK_FALLBACK_TEXT);
        g.writeFieldName('items');
        g.writeStartArray();
        for (CandidatePii p : chunk) {
            g.writeStartObject();
            g.writeStringField('job_applicant_id', p.jobApplicantId);
            if (String.isNotBlank(p.accountId)) {
                g.writeStringField('account_id', p.accountId);
            }
            if (!p.maskStrings.isEmpty()) {
                g.writeFieldName('mask_strings');
                g.writeStartArray();
                for (String s : p.maskStrings) {
                    g.writeString(s);
                }
                g.writeEndArray();
            }
            g.writeEndObject();
        }
        g.writeEndArray();
        g.writeEndObject();
        return g.getAsString();
    }

    @TestVisible
    private static List<MaskOutcome> sendChunk(List<CandidatePii> chunk) {
        HttpRequest req = new HttpRequest();
        req.setEndpoint(NAMED_CREDENTIAL + '/mask/batch');
        req.setMethod('POST');
        req.setHeader('Content-Type', 'application/json');
        req.setTimeout(CALLOUT_TIMEOUT_MS);
        req.setBody(batchBody(chunk));

        HttpResponse res;
        try {
            res = new Http().send(req);
        } catch (Exception e) {
            return allFailed(chunk, 'Callout failed: ' + e.getMessage());
        }
        return parseBatchResponse(res, chunk);
    }

    @TestVisible
    private static List<MaskOutcome> parseBatchResponse(HttpResponse res, List<CandidatePii> chunk) {
        if (res.getStatusCode() != 200) {
            return allFailed(chunk, 'HTTP ' + res.getStatusCode() + ': ' + res.getBody());
        }

        Map<String, Object> body;
        try {
            body = (Map<String, Object>) JSON.deserializeUntyped(res.getBody());
        } catch (Exception e) {
            return allFailed(chunk, 'Unreadable response: ' + res.getBody());
        }

        List<Object> results = (List<Object>) body.get('results');
        if (results == null) {
            return allFailed(chunk, String.valueOf(body.get('detail')));
        }

        List<MaskOutcome> outcomes = new List<MaskOutcome>();
        for (Object o : results) {
            Map<String, Object> row = (Map<String, Object>) o;
            Map<String, Object> r = (Map<String, Object>) row.get('result');

            MaskOutcome m = new MaskOutcome();
            m.jobApplicantId = String.valueOf(row.get('job_applicant_id'));
            if (r != null) {
                m.status                 = (String) r.get('status');
                m.jobApplicantName       = (String) r.get('job_applicant_name');
                m.maskedContentVersionId = (String) r.get('masked_content_version_id');
                m.detail                 = (String) r.get('detail');
                Object regions = r.get('redacted_regions');
                m.redactedRegions = (regions == null) ? null : Integer.valueOf(regions);
            }
            outcomes.add(m);
        }
        return outcomes;
    }

    private static List<MaskOutcome> allFailed(List<CandidatePii> chunk, String detail) {
        List<MaskOutcome> outcomes = new List<MaskOutcome>();
        for (CandidatePii p : chunk) {
            MaskOutcome m = new MaskOutcome();
            m.jobApplicantId = p.jobApplicantId;
            m.status = 'error';
            m.detail = detail;
            outcomes.add(m);
        }
        return outcomes;
    }
}
