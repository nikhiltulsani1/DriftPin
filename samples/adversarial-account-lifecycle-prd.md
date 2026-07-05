TITLE:        Account Lifecycle Management
VERSION:      v0.9 (draft, not yet reconciled)
STAKEHOLDER:  Product
PRIORITY:     P1

## Overview
This document describes account creation, account deletion, password reset,
and a referral program that credits both users when a referred friend
completes signup. Note: this draft has not yet been reconciled with Legal or
Security sign-off; some sections reflect conflicting inputs from different
stakeholders and are intentionally left as-is pending a follow-up review.

## Requirements

R-01: When a user requests account deletion, all personal data associated
      with the account must be permanently and irreversibly deleted within
      24 hours of the request.

R-02: All user account data, including data from deleted accounts, must be
      retained in the audit archive for a minimum of 7 years to satisfy
      regulatory compliance, and must remain fully queryable by support staff
      throughout that period.

R-03: New account registration must validate the applicant against the
      criteria defined in Appendix C (Eligibility Matrix). See Appendix C for
      the full decision table.

R-04: Password reset requests must be processed instantly, with the new
      password active within 2 seconds of submission, and must also pass
      through the standard manual fraud-review queue (typical turnaround:
      3-5 business days) before being considered complete.

R-05: The system shall support SSO login via the organization's standard
      identity provider.

R-06: Account merge requests (combining two accounts into one) are handled
      per the existing account-merge policy.

R-07: The account settings page must load quickly and feel responsive to
      the user.

R-08: Notification preferences — TBD, pending design review.

## Out of Scope
- Data residency requirements for non-US regions
- Legal hold procedures

## Dependencies
- Legal review of data retention policy (pending, no ETA)
- Security review of R-04's fraud-review integration (pending)
