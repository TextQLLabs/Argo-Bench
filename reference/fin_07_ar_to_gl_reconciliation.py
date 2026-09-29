"""Reference solution for fin-07-ar-to-gl-reconciliation.

The question asks for a year-end tie-out of the receivable, account 1101, to the Receivables
subledger, from a subledger that shows today's open items (January activity included), with
the difference explained by reconciling items from a fixed list of ten labels. Four of the
labels are real subledger populations that are not reconciling items of 1101, and must be
left out: adjustments (there are none), account credit and refund credit memos applied to
the invoices (the part of an invoice paid with account credit is booked to customer credit
payable, 2107, never to 1101, and the applications take exactly that part off the open
item), and on-account credit balances (credit memo items, outside the invoice population).
The one the prompt flags as needing proof is recognition: which invoices in the population
the ledger has not recognized yet.

The ledger is ``GL_JE_LINES`` on ``SEGMENT3 = '1101'`` effective by December 31, posted or
not; the subledger position is today's ``AMOUNT_DUE_REMAINING`` on the invoice payment
schedules of invoices dated by December 31 (``RA_CUST_TRX_TYPES_ALL.TYPE = 'INV'``). A cash
application (``AR_RECEIVABLE_APPLICATIONS_ALL``) takes an invoice off the open item when it
is made, but the ledger credits 1101 only when the receipt clears
(``AR_CASH_RECEIPT_HISTORY_ALL.STATUS = 'CLEARED'``) and debits it back when a receipt is
returned (``REVERSED``, and the application is then ``UNAPP``). Hence: applied cash not
cleared by year-end; receipts cleared by year-end and returned after it. Order invoices are
recognized by their receivable distributions (``RA_CUST_TRX_LINE_GL_DIST_ALL``, class REC,
on 1101, dated by the invoice date); membership invoices (``OKS CONTRACTS``) carry none, and
the ledger's other 1101 debits (the order feed and Payables) equal the membership charges
made by year-end to the cent, so a membership invoice is recognized when it is charged: the
unrecognized amount is what was billed and not charged by December 31. Invoices left
incomplete have no open item but carry their 1101 distribution less the receipts cleared
against them (plus returns) by year-end.

Score: 1.0 on the paper's warehouse and 1.0 on the released warehouse.
"""

QUESTION = "fin-07-ar-to-gl-reconciliation"

#: The invoices dated by year-end, with where they came from and whether autoinvoice
#: completed them.
INVOICES = """\
inv AS (
  SELECT t.CUSTOMER_TRX_ID, t.INTERFACE_HEADER_CONTEXT AS source, t.COMPLETE_FLAG AS complete
  FROM RA_CUSTOMER_TRX_ALL t
  JOIN RA_CUST_TRX_TYPES_ALL y ON y.CUST_TRX_TYPE_ID = t.CUST_TRX_TYPE_ID AND y.ORG_ID = t.ORG_ID
  WHERE y.TYPE = 'INV' AND t.TRX_DATE <= DATE '2024-12-31')"""

STEPS = [
    # 1. The ledger: 1101 at December 31, every journal effective by then, posted or not, by
    #    where the journal came from.
    ("run_sql", {"sql": """\
SELECT h.JE_SOURCE AS source, h.JE_CATEGORY AS category, l.STATUS AS status, COUNT(*) AS lines,
       ROUND(SUM(COALESCE(l.ACCOUNTED_DR, 0)), 2) AS debits,
       ROUND(SUM(COALESCE(l.ACCOUNTED_CR, 0)), 2) AS credits
FROM GL_JE_LINES l
JOIN GL_JE_HEADERS h ON h.JE_HEADER_ID = l.JE_HEADER_ID
JOIN GL_CODE_COMBINATIONS c ON c.CODE_COMBINATION_ID = l.CODE_COMBINATION_ID
WHERE c.SEGMENT3 = '1101' AND l.EFFECTIVE_DATE <= DATE '2024-12-31'
GROUP BY h.JE_SOURCE, h.JE_CATEGORY, l.STATUS
ORDER BY source, category, status"""}),

    # 2. The subledger: invoices dated by year-end and what their payment schedules still
    #    show open today.
    ("run_sql", {"sql": f"""\
WITH {INVOICES},
item AS (
  SELECT CUSTOMER_TRX_ID, SUM(AMOUNT_DUE_ORIGINAL) AS original,
         SUM(AMOUNT_DUE_REMAINING) AS remaining
  FROM AR_PAYMENT_SCHEDULES_ALL WHERE CLASS = 'INV'
  GROUP BY CUSTOMER_TRX_ID)
SELECT i.source, i.complete, COUNT(*) AS invoices, COUNT(p.CUSTOMER_TRX_ID) AS with_open_item,
       ROUND(SUM(p.original), 2) AS original, ROUND(SUM(p.remaining), 2) AS remaining_today
FROM inv i LEFT JOIN item p ON p.CUSTOMER_TRX_ID = i.CUSTOMER_TRX_ID
GROUP BY i.source, i.complete
ORDER BY i.source, i.complete"""}),

    # 3. How each invoice was booked: its receivable distributions on 1101 and elsewhere
    #    (dated by year-end), against the account credit applied to it.
    ("run_sql", {"sql": f"""\
WITH {INVOICES},
booked AS (
  SELECT d.CUSTOMER_TRX_ID,
         SUM(CASE WHEN c.SEGMENT3 = '1101' THEN d.AMOUNT ELSE 0 END) AS receivable_1101,
         SUM(CASE WHEN c.SEGMENT3 <> '1101' THEN d.AMOUNT ELSE 0 END) AS receivable_elsewhere
  FROM RA_CUST_TRX_LINE_GL_DIST_ALL d
  JOIN GL_CODE_COMBINATIONS c ON c.CODE_COMBINATION_ID = d.CODE_COMBINATION_ID
  WHERE d.ACCOUNT_CLASS = 'REC' AND d.GL_DATE <= DATE '2024-12-31'
  GROUP BY d.CUSTOMER_TRX_ID),
tendered AS (
  SELECT APPLIED_CUSTOMER_TRX_ID AS CUSTOMER_TRX_ID, SUM(AMOUNT_APPLIED) AS credit_applied
  FROM AR_RECEIVABLE_APPLICATIONS_ALL
  WHERE APPLICATION_TYPE = 'CM' AND STATUS = 'APP'
  GROUP BY APPLIED_CUSTOMER_TRX_ID)
SELECT i.source, i.complete, COUNT(*) AS invoices,
       COUNT(b.CUSTOMER_TRX_ID) AS with_receivable_accounting,
       ROUND(SUM(b.receivable_1101), 2) AS booked_to_1101,
       ROUND(SUM(b.receivable_elsewhere), 2) AS booked_elsewhere,
       ROUND(SUM(t.credit_applied), 2) AS account_credit_applied
FROM inv i
LEFT JOIN booked b ON b.CUSTOMER_TRX_ID = i.CUSTOMER_TRX_ID
LEFT JOIN tendered t ON t.CUSTOMER_TRX_ID = i.CUSTOMER_TRX_ID
GROUP BY i.source, i.complete
ORDER BY i.source, i.complete"""}),

    # 4. The cash applied to those invoices, by whether the receipt was dated, cleared and
    #    returned by year-end (the clock the ledger follows).
    ("run_sql", {"sql": f"""\
WITH {INVOICES},
events AS (
  SELECT CASH_RECEIPT_ID,
         MIN(CASE WHEN STATUS = 'CLEARED' THEN CAST(TRX_DATE AS DATE) END) AS cleared_on,
         MIN(CASE WHEN STATUS = 'REVERSED' THEN CAST(TRX_DATE AS DATE) END) AS reversed_on
  FROM AR_CASH_RECEIPT_HISTORY_ALL
  WHERE STATUS IN ('CLEARED', 'REVERSED')
  GROUP BY CASH_RECEIPT_ID)
SELECT i.source, i.complete, a.STATUS AS application_status,
       r.RECEIPT_DATE <= DATE '2024-12-31' AS receipt_by_year_end,
       COALESCE(e.cleared_on <= DATE '2024-12-31', FALSE) AS cleared_by_year_end,
       COALESCE(e.reversed_on <= DATE '2024-12-31', FALSE) AS returned_by_year_end,
       e.reversed_on IS NOT NULL AS returned_ever,
       COUNT(*) AS applications, ROUND(SUM(a.AMOUNT_APPLIED), 2) AS amount
FROM AR_RECEIVABLE_APPLICATIONS_ALL a
JOIN AR_CASH_RECEIPTS_ALL r ON r.CASH_RECEIPT_ID = a.CASH_RECEIPT_ID
JOIN inv i ON i.CUSTOMER_TRX_ID = a.APPLIED_CUSTOMER_TRX_ID
LEFT JOIN events e ON e.CASH_RECEIPT_ID = a.CASH_RECEIPT_ID
WHERE a.APPLICATION_TYPE = 'CASH' AND r.TYPE = 'CASH'
GROUP BY source, complete, application_status, receipt_by_year_end, cleared_by_year_end,
         returned_by_year_end, returned_ever
ORDER BY source, complete, application_status, receipt_by_year_end, cleared_by_year_end,
         returned_by_year_end, returned_ever"""}),

    # 5. Subledger adjustments, if any.
    ("run_sql", {"sql": """\
SELECT COUNT(*) AS adjustments, ROUND(SUM(AMOUNT), 2) AS amount FROM AR_ADJUSTMENTS_ALL"""}),

    # 6. Assemble the reconciliation, prove it to the cent, file.
    ("run_python", {"code": """\
import pandas as pd
from mission_control import MissionControl

gl = pd.read_parquet("results/sql_0001.parquet")
pop = pd.read_parquet("results/sql_0002.parquet")
booked = pd.read_parquet("results/sql_0003.parquet")
cash = pd.read_parquet("results/sql_0004.parquet")
adjustments = pd.read_parquet("results/sql_0005.parquet")

MEMBERSHIP = "OKS CONTRACTS"
gl_receivable = (gl["debits"] - gl["credits"]).sum()
ar_remaining = pop["remaining_today"].sum()

complete, incomplete = cash[cash["complete"] == "Y"], cash[cash["complete"] == "N"]
applied = complete["application_status"] == "APP"
returned = complete["application_status"] == "UNAPP"
# Applied in the subledger, not yet credited to 1101 by a clearing at year-end.
not_settled = complete.loc[applied & ~complete["cleared_by_year_end"], "amount"].sum()
# Cleared (credited to 1101) by year-end, returned after it: the subledger has reopened them.
returned_after = -complete.loc[returned & complete["cleared_by_year_end"]
                               & ~complete["returned_by_year_end"], "amount"].sum()
# Membership invoices are recognized when charged: the ledger's non-invoicing 1101 debits
# equal the membership charges made by year-end.
membership_charged = cash.loc[(cash["source"] == MEMBERSHIP) & (cash["application_status"] == "APP")
                              & cash["receipt_by_year_end"], "amount"].sum()
membership_debits = gl.loc[gl["source"] != "Receivables", "debits"].sum()
membership_billed = pop.loc[pop["source"] == MEMBERSHIP, "original"].sum()
not_recognized = -(membership_billed - membership_charged)
# Incomplete invoices: booked to 1101, less receipts cleared against them by year-end,
# plus those returned by year-end.
incomplete_gl = (booked.loc[booked["complete"] == "N", "booked_to_1101"].sum()
                 - incomplete.loc[incomplete["cleared_by_year_end"], "amount"].sum()
                 + incomplete.loc[incomplete["returned_by_year_end"], "amount"].sum())
credit_gap = (booked["booked_elsewhere"].fillna(0) - booked["account_credit_applied"].fillna(0)).abs().max()

items = {
    "applied_cash_not_yet_settled": not_settled,
    "returned_receipts_not_yet_clawed_back": returned_after,
    "invoices_not_recognized_in_gl": not_recognized,
    "incomplete_invoices_gl": incomplete_gl,
}
items = {k: round(float(v), 2) for k, v in items.items() if abs(v) >= 0.005}
positions = {"gl_receivable": round(float(gl_receivable), 2),
             "ar_invoices_remaining": round(float(ar_remaining), 2)}
difference = round(positions["gl_receivable"] - positions["ar_invoices_remaining"], 2)
residual = round(difference - sum(items.values()), 2)
for k, v in {**positions, **items}.items():
    print(f"{k:40s} {v:16,.2f}")
print(f"difference {difference:,.2f}; unexplained {residual:.2f}")
print(f"membership: 1101 debits outside invoicing {membership_debits:,.2f} vs charged by "
      f"year-end {membership_charged:,.2f}; account credit applied less its non-1101 booking "
      f"{credit_gap:.2f}; adjustments {int(adjustments['adjustments'].iloc[0])}")

mission_control = MissionControl()
mission_control.file_schedules([{
    "name": "ar_to_gl_reconciliation", "as_of": "2024-12-31", "key_columns": ["item"],
    "rows": [{"item": k, "amount": v} for k, v in {**positions, **items}.items()],
}])
f = lambda x: f"{x:,.2f}"
mission_control.note(
    f"GL 1101 {f(positions['gl_receivable'])} (GL_JE_LINES effective by 2024-12-31, posted and "
    f"unposted) less today's remaining on invoices dated by year-end "
    f"{f(positions['ar_invoices_remaining'])} = {f(difference)}; the reconciling items sum to "
    f"{f(sum(items.values()))}, unexplained {residual:.2f}. "
    + "; ".join(f"{k} {f(v)}" for k, v in items.items()) + ". "
    "The subledger applies cash at application, the ledger credits 1101 when the receipt clears "
    "and debits it back when a receipt is returned (receipt history CLEARED / REVERSED). Order "
    "invoices are recognized by their 1101 REC distributions (all dated by year-end); membership "
    "invoices have no receivable distribution and are recognized when charged: the non-invoicing "
    f"1101 debits ({f(membership_debits)}) equal the membership charges receipted by year-end "
    f"({f(membership_charged)}), so billed-but-uncharged membership invoices are not in 1101. "
    "Incomplete invoices: their 1101 distribution less receipts cleared by year-end plus returns. "
    "Left out: approved_adjustments (no adjustments exist); customer_credits_tendered and "
    "refund_credit_memos (credit applied to an invoice is booked to 2107, not 1101, and takes "
    "exactly that part off the open item, so 1101 never carried it); on_account_credit_balances "
    "(credit memo items outside the invoice population, never in 1101).")
mission_control.summary()
"""}),
]
