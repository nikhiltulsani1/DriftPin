# Account Access — Password Reset & Session Management

## Overview

This document describes requirements for the account access module, covering
password reset and session lifecycle behavior.

## Requirements

Users must be able to reset their password via a link emailed to their
registered address. The reset link expires 24 hours after issuance and can
only be used once.

Passwords must be at least 12 characters long and contain at least one number
and one symbol. The system rejects any reset submission that does not meet
this policy and displays the specific reason for rejection.

Sessions expire after 30 minutes of inactivity. When a session expires, the
user is redirected to the login page and any unsaved form data is discarded.

After 5 consecutive failed login attempts within a 15 minute window, the
account is locked for 30 minutes. A locked account displays a clear message
explaining the lockout and its expected duration.

Administrators can manually unlock a locked account from the admin console.
Manual unlock actions are recorded in the audit log with the administrator's
identity and a timestamp.

Users receive an email notification whenever their password is changed,
whether the change was initiated by the user or by an administrator.
