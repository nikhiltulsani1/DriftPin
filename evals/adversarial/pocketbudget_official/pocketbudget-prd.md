# PocketBudget — Product Requirements Document

**Version:** 1.0
**Date:** July 2026
**Status:** Ready for QA

---

## 1. Overview

PocketBudget is a personal finance tracking app for Android. Users link their bank
accounts, categorise transactions, set monthly budgets per category, and receive
alerts when spending approaches or exceeds limits. An AI assistant answers questions
about spending patterns.

---

## 2. Target Users

Young professionals aged 22–35 who want visibility into their spending without
manually entering every transaction.

---

## 3. Requirements

### R-01: Transaction Sync
The app must sync transactions from linked bank accounts automatically every 24 hours.
Users can also trigger a manual sync at any time.

### R-02: Transaction Categorisation
All transactions must be automatically categorised on sync using rule-based matching
(merchant name → category). Users can manually override a category.
The system should remember user overrides and apply them to future transactions
from the same merchant.

### R-03: Budget Setup
Users can set a monthly budget for each spending category.
Budgets reset on the 1st of each month.
A user may not set a budget of zero.

### R-04: Alerts
The app must send a push notification when spending in any category reaches 80%
of the budget. A second notification is sent at 100%.
Notifications should not repeat if the user has already been notified at that threshold.

### R-05: AI Spending Assistant
Users can ask the AI assistant questions about their transactions and spending patterns.
The assistant must only answer questions about the user's own data.
The assistant should decline to answer questions unrelated to personal finance.

### R-06: Data Export
Users can export their transaction history as a CSV file.
The export includes: date, merchant, amount, category, and notes.
Exports are limited to the last 12 months of data.

### R-07: Multi-Currency
The app displays all amounts in the user's home currency.
Transactions in foreign currencies are converted at the rate on the transaction date.
Historical exchange rates are fetched from an external API.

### R-08: Account Linking
Users can link up to 3 bank accounts.
Linking requires OAuth authentication with the bank.
A linked account can be unlinked at any time.

### R-09: Spending Insights
At the end of each month, the app generates a spending summary: total spent,
breakdown by category, biggest single transaction, and month-over-month comparison.
The summary is available from the 1st of the following month.

---

## 4. Acceptance Criteria

**AC-01 (R-01):** Sync completes within 60 seconds for accounts with up to 500 transactions.
**AC-02 (R-01):** Manual sync button is visible on the Dashboard at all times.
**AC-03 (R-02):** After a user overrides a category for merchant "Swiggy," all future
Swiggy transactions are auto-categorised to the overridden category.
**AC-04 (R-03):** Budget entry field rejects values below ₹1.
**AC-05 (R-03):** Budgets reset at midnight on the 1st — server time.
**AC-06 (R-04):** No duplicate notifications for the same threshold in the same calendar month.
**AC-07 (R-05):** AI response contains no data belonging to other users.
**AC-08 (R-06):** CSV export file is generated within 10 seconds for up to 12 months of data.
**AC-09 (R-07):** Foreign currency amounts display with both the original amount and
the converted home-currency amount.
**AC-10 (R-08):** Unlinking an account does not delete historical transaction data.
**AC-11 (R-09):** Monthly summary is not available before the 1st of the following month.

---

## 5. User Flows

### New User
Sign up → Link bank account → Categorisation rules applied → Dashboard shown

### Daily Use
App open → Dashboard (transaction list, budget bars) → Tap transaction → View/edit category

### Budget Alert Flow
Spend logged → System checks % of budget → If threshold crossed → Push notification sent →
User taps notification → Opens relevant category budget screen

### Month-End
Last day of month → Spending summary generated overnight → Available from 1st

---

## 6. Out of Scope

- Web dashboard
- Joint accounts or shared budgets
- Investment tracking
- Loan or credit card balance tracking
- Manual transaction entry

---

## 7. Non-Functional Requirements

- App cold start: < 2s on mid-range Android
- Sync failure: retry up to 3 times before surfacing error to user
- All financial data encrypted at rest (AES-256)
- GDPR-compliant data deletion on account closure
- Minimum Android API 26
