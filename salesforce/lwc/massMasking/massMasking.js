import { LightningElement, wire, api } from 'lwc';
import { ShowToastEvent } from 'lightning/platformShowToastEvent';
import getJobApplicants from '@salesforce/apex/MassMaskingController.getJobApplicants';
import generatemassmasking from '@salesforce/apex/MassMaskingController.generatemassmasking';
import enqueueMasking from '@salesforce/apex/MassMaskingController.enqueueMasking'; // <--- ADD THIS

const BASE_URL = 'https://resume-masker-production.up.railway.app';

const COLUMNS = [
    { 
        label: 'Job Applicant ID', 
        fieldName: 'applicantId', 
        type: 'text' 
    },
    { 
        label: 'Job Title', 
        fieldName: 'jobTitle', 
        type: 'text' 
    },
    { 
        label: 'Candidate', 
        fieldName: 'contactName', 
        type: 'text' 
    }
];

export default class MassMasking extends LightningElement {
    @api recordId;
    columns = COLUMNS;
    jobApplicants = [];
    selectedIds = [];
    isLoading = false;

    @wire(getJobApplicants, { jobId: '$recordId' })
    wiredJobApplicants({ data, error }) {
        console.log('=== wiredJobApplicants called ===');
        console.log('recordId:', this.recordId);
        
        if (data) {
            console.log('Number of applicants:', data.length);
            this.jobApplicants = data.map((row, index) => {
                // Generate JA-XXXXX format
                const applicantId = this.generateApplicantId(row, index);
                
                return {
                    Id: row.Id,
                    Name: row.Name, // <--- IMPORTANT: Pass Name through
                    applicantId: applicantId,
                    jobTitle: row.SCSCHAMPS__Job_Title__c || 'N/A',
                    contactName: row.SCSCHAMPS__Contact_Talent__r
                        ? row.SCSCHAMPS__Contact_Talent__r.Name
                        : 'No Contact'
                };
            });
            console.log('Mapped applicants:', this.jobApplicants);
        } else if (error) {
            console.error('Error:', error);
            this.toast('Error', this.reduceError(error), 'error');
        }
    }

    // Method to generate JA-XXXXX format
    generateApplicantId(record, index) {
        // If Name already has JA- format, use it
        if (record.Name && record.Name.startsWith('JA-')) {
            return record.Name;
        }
        
        // Generate from Contact Name
        if (record.SCSCHAMPS__Contact_Talent__r) {
            const contactName = record.SCSCHAMPS__Contact_Talent__r.Name;
            // Get initials from contact name
            const nameParts = contactName.split(' ');
            let initials = '';
            for (let i = 0; i < nameParts.length; i++) {
                if (nameParts[i].length > 0) {
                    initials += nameParts[i].charAt(0);
                }
            }
            initials = initials.toUpperCase().substring(0, 3);
            
            // Use last 4 characters of ID for uniqueness
            const idSuffix = record.Id.substring(record.Id.length - 4);
            return 'JA-' + initials + idSuffix;
        }
        
        // Use sequential number
        const seqNumber = String(index + 1).padStart(3, '0');
        return 'JA-' + seqNumber;
    }

    handleRowSelection(event) {
        this.selectedIds = event.detail.selectedRows.map(row => row.Id);
        console.log('Selected IDs:', this.selectedIds);
    }

    get selectedCount() {
        return this.selectedIds.length;
    }

    get isButtonDisabled() {
        return this.isLoading || this.selectedIds.length === 0;
    }

    handleMassMasking() {
        if (this.selectedIds.length === 0) {
            this.toast('Error', 'Please select a record first...', 'error');
            return;
        }

        this.isLoading = true;

        // Always open the masking page, whatever the count. The page sends
        // the selection to the service in chunks and the service queues
        // anything past its own concurrency limit, so a selection of 20 or of
        // 200 is the page's business and not this button's.
        //
        // Branching here was why a selection over 10 masked correctly but
        // never redirected: that path ran the Apex Queueable, which finishes
        // server-side with the watermark settings compiled into the class and
        // raises a toast instead of navigating.
        this.launchDirectMasking();
    }

    // For 10 or fewer applicants (Direct Synchronous)
    launchDirectMasking() {
        generatemassmasking({ jobAppIdList: this.selectedIds })
            .then(ctx => {
                const displayNames = ctx.displayNames || '';
                const url = BASE_URL + '/candidate/MaskProfileIndex'
                    + '?sfURL=' + encodeURIComponent(ctx.orgUrl)
                    + '&uname=' + encodeURIComponent(ctx.uname)
                    + '&sfjobapplicantid=' + encodeURIComponent(ctx.ids)
                    + '&displaynames=' + encodeURIComponent(displayNames);

                window.open(url, '_blank');
            })
            .catch(error => {
                this.toast('Unable to launch masking', this.reduceError(error), 'error');
            })
            .finally(() => {
                this.isLoading = false;
            });
    }

    // For more than 10 applicants (Background Queueable)
    enqueueBackgroundMasking() {
        enqueueMasking({ jobAppIdList: this.selectedIds })
            .then(jobId => {
                this.toast('Success', 'Masking started for ' + this.selectedIds.length + ' applicants in the background. Job ID: ' + jobId, 'success');
                console.log('Queueable Job ID:', jobId);
            })
            .catch(error => {
                this.toast('Unable to enqueue masking', this.reduceError(error), 'error');
            })
            .finally(() => {
                this.isLoading = false;
            });
    }

    toast(title, message, variant) {
        this.dispatchEvent(new ShowToastEvent({ title, message, variant }));
    }

    reduceError(error) {
        return (error && error.body && error.body.message) || 
               (error && error.message) || 
               'Unknown error';
    }
}