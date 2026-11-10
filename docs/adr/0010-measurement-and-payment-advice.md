# ADR-0010 — Measurement and payment advice (accounts pays outside the system)

**Status:** Accepted · 10 Nov 2026

## Context
Blueprint §5.4 (payment controls), acceptance tests PAY-01/02/03. The organisation's process: a **payment advice**
is approved by an approver and goes to **accounts**; accounts makes the payment **outside this system** and
**uploads the payment receipt**. Subcontract OS therefore certifies and tracks payments; it does not move money
and does not replace the accounting system.

## Decision
1. **Payment terms per subcontract** (`commercial_payment_terms`): retention %, retention cap (% of current value),
   advance amount and recovery %, GST %, TDS %, days to pay, and the contract clause they come from. Configuration,
   never hard-coded. Once an advice is approved, changing terms needs a reason; every advice keeps a snapshot of
   the terms (version) it was calculated with.
2. **Measurement** (`commercial_measurements`): MB sheet for a period (not in the future) with BOQ lines; amount =
   quantity × rate in fixed-point decimals; the signed sheet is mandatory evidence. Verified (or returned with a
   reason) by construction / project controls / QA, never by whoever entered it. Only active subcontracts.
3. **Payment advice** (`commercial_payment_advices`), prepared by the commercial manager from verified
   measurements, incorporated variations (VAR-02: only after incorporation by amendment) and the agreed advance,
   less deductions (back-charges, debit notes and penalties need their document). Server calculation:
   `net = (work + advance) + GST − retention (capped) − advance recovery (≤ outstanding) − TDS − deductions`,
   rounded to the paisa at each step; negative net refused. Each step, its formula and the frozen to-date figures
   (retention held, advance advised/recovered) are stored, so `/recalculate` reproduces the certificate (PAY-01);
   approval is refused if stored figures do not reconcile. Measurement lines and variations can be on only one
   live advice; cancelling a draft releases them. Creation and decisions accept idempotency keys (PAY-02).
4. **Workflow:** DRAFT → SUBMITTED → APPROVED (**sent to accounts**, pay-by date = approval + terms days) →
   PART_PAID → PAID; RETURN (reason) back to DRAFT; CANCEL (draft only, reason). The preparer cannot approve;
   the preparer and the approver cannot record the payment (PAY-03).
5. **Accounts** (new role `accounts`): an *Accounts queue* across their projects; a printable advice (Indian digit
   grouping); *Record payment* = date (not future, not before approval), amount (≤ balance; part payments allowed),
   mode, bank/UTR/cheque reference (unique per subcontract) and the **receipt** (PDF, PNG, JPG — images checked by
   signature) stored as a `PAYMENT_RECEIPT` document `RCPT-<subcontract>-<advice>-<n>`. The same receipt file cannot
   be used twice. No bank-account numbers are stored.
6. **Traceability:** advice VALUES measurements (EVIDENCED_BY the MB sheet) and variations, DEDUCTS deductions,
   APPROVED_BY the approver, SETTLED_BY payments (receipts). A variation's trace shows the advice that paid it.
   Dashboard: measurements to verify, advices awaiting approval, amount with accounts, overdue advices, certified /
   paid / retention per subcontract.

## Consequences
- New demo user `lata.accounts`. Demo SC-101-01: PA-001 advance and PA-002 (RA bill 1) paid with receipts,
  PA-003 (RA bill 2 + VAR-001 − debit note) with accounts, MB-003 awaiting verification.
- Not yet included: retention release and final account at closeout, multi-currency, integration with the
  accounting system (the receipt upload is the hand-off), approval limits by value band.

## Tests
`tests/test_payments.py` (11): PAY-01 recalculation and tamper detection, retention cap and recovery limits,
PAY-02 idempotency, PAY-03 segregation, measurement rules, no double payment and cancel release, deductions and
negative net, return to draft, accounts receipt rules (part payment, overpayment, dates, duplicates, image check),
print, trace and access.
