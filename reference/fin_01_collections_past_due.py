"""Reference solution for fin-01-collections-past-due.

The question asks for Collections' January list: customer invoices still open in the
extract and more than 90 days past due at December 31, 2024, with each invoice's genuinely
collectible amount after the unapplied cash sitting on its own receipts, kept only above
$5.00, membership subscription invoices out of every figure; and filed as collection
attempts keyed by the order the invoice bills. The trap is the premise: the naive 90-day
aging runs to hundreds of thousands of open items, most of them already paid (the
customer's receipt was taken in full but only partly applied, and the unapplied cash on it
covers the open balance) and most of the rest $5.00 or less; only a few hundred are worth
chasing. Reversed receipts are not cash, and cash on an unrelated receipt must not be used.

An open item is an ``AR_PAYMENT_SCHEDULES_ALL`` row with ``CLASS = 'INV'`` and
``STATUS = 'OP'``; order invoices are ``RA_CUSTOMER_TRX_ALL.INTERFACE_HEADER_CONTEXT =
'ORDER ENTRY'`` (membership invoices come from ``OKS CONTRACTS``). More than 90 days past due
at December 31 is a ``DUE_DATE`` before October 2. A receipt's unapplied cash is its
``AMOUNT`` less its ``CASH``/``APP`` applications (``AR_RECEIVABLE_APPLICATIONS_ALL``), for
cash receipts (``TYPE = 'CASH'``) not reversed (``STATUS <> 'REV'``); an invoice's own
receipts are those with an application to it, each counted once. The invoice bills the order
whose ``ORDER_NUMBER`` is ``INTERFACE_HEADER_ATTRIBUTE1``; the filing is its ``HEADER_ID``.

Score: 1.0 on the paper's warehouse and 1.0 on the released warehouse.
"""

QUESTION = "fin-01-collections-past-due"

STEPS = [
    # 1. The open invoice items at extract, by where the invoice came from and whether it
    #    was more than 90 days past due on December 31 (due before October 2).
    ("run_sql", {"sql": """\
SELECT t.INTERFACE_HEADER_CONTEXT AS source,
       ps.DUE_DATE < DATE '2024-10-02' AS over_90_days_past_due,
       COUNT(*) AS open_invoices, ROUND(SUM(ps.AMOUNT_DUE_REMAINING), 2) AS remaining_usd
FROM AR_PAYMENT_SCHEDULES_ALL ps
JOIN RA_CUSTOMER_TRX_ALL t ON t.CUSTOMER_TRX_ID = ps.CUSTOMER_TRX_ID
WHERE ps.STATUS = 'OP' AND ps.CLASS = 'INV'
GROUP BY source, over_90_days_past_due
ORDER BY source, over_90_days_past_due"""}),

    # 2. Unapplied cash on every receipt: the amount less what was applied, by receipt type
    #    and status (reversed receipts and the negative refund receipts are not cash in hand).
    ("run_sql", {"sql": """\
WITH applied AS (
  SELECT CASH_RECEIPT_ID, SUM(AMOUNT_APPLIED) AS applied
  FROM AR_RECEIVABLE_APPLICATIONS_ALL
  WHERE APPLICATION_TYPE = 'CASH' AND STATUS = 'APP'
  GROUP BY CASH_RECEIPT_ID),
receipt AS (
  SELECT r.TYPE, r.STATUS, r.AMOUNT, ROUND(r.AMOUNT - COALESCE(p.applied, 0), 2) AS unapplied
  FROM AR_CASH_RECEIPTS_ALL r LEFT JOIN applied p ON p.CASH_RECEIPT_ID = r.CASH_RECEIPT_ID)
SELECT TYPE AS receipt_type, STATUS AS receipt_status, COUNT(*) AS receipts,
       ROUND(SUM(AMOUNT), 2) AS amount_usd,
       SUM(CASE WHEN unapplied > 0.005 THEN 1 ELSE 0 END) AS receipts_with_unapplied_cash,
       ROUND(SUM(CASE WHEN unapplied > 0.005 THEN unapplied ELSE 0 END), 2) AS unapplied_usd
FROM receipt
GROUP BY TYPE, STATUS
ORDER BY receipt_type, receipt_status"""}),

    # 3. Collections' list: each open order invoice over 90 days past due, less the unapplied
    #    cash on its own (non-reversed) receipts, kept above $5.00, keyed by the order.
    ("run_sql", {"sql": """\
WITH due AS (
  SELECT ps.CUSTOMER_TRX_ID, t.INTERFACE_HEADER_ATTRIBUTE1 AS order_number,
         ps.AMOUNT_DUE_REMAINING AS remaining
  FROM AR_PAYMENT_SCHEDULES_ALL ps
  JOIN RA_CUSTOMER_TRX_ALL t ON t.CUSTOMER_TRX_ID = ps.CUSTOMER_TRX_ID
  WHERE ps.STATUS = 'OP' AND ps.CLASS = 'INV' AND t.INTERFACE_HEADER_CONTEXT = 'ORDER ENTRY'
    AND ps.DUE_DATE < DATE '2024-10-02'),
linked AS (
  SELECT DISTINCT a.APPLIED_CUSTOMER_TRX_ID AS CUSTOMER_TRX_ID, a.CASH_RECEIPT_ID
  FROM AR_RECEIVABLE_APPLICATIONS_ALL a
  JOIN due d ON d.CUSTOMER_TRX_ID = a.APPLIED_CUSTOMER_TRX_ID
  WHERE a.APPLICATION_TYPE = 'CASH' AND a.STATUS = 'APP'),
applied AS (
  SELECT CASH_RECEIPT_ID, SUM(AMOUNT_APPLIED) AS applied
  FROM AR_RECEIVABLE_APPLICATIONS_ALL
  WHERE APPLICATION_TYPE = 'CASH' AND STATUS = 'APP'
    AND CASH_RECEIPT_ID IN (SELECT CASH_RECEIPT_ID FROM linked)
  GROUP BY CASH_RECEIPT_ID),
own_cash AS (
  SELECT l.CUSTOMER_TRX_ID, SUM(ROUND(r.AMOUNT - p.applied, 2)) AS unapplied
  FROM linked l
  JOIN AR_CASH_RECEIPTS_ALL r ON r.CASH_RECEIPT_ID = l.CASH_RECEIPT_ID
  JOIN applied p ON p.CASH_RECEIPT_ID = l.CASH_RECEIPT_ID
  WHERE r.TYPE = 'CASH' AND r.STATUS <> 'REV'
  GROUP BY l.CUSTOMER_TRX_ID),
gap AS (
  SELECT d.order_number, d.remaining, COALESCE(c.unapplied, 0) AS own_unapplied,
         ROUND(d.remaining - COALESCE(c.unapplied, 0), 2) AS collectible
  FROM due d LEFT JOIN own_cash c ON c.CUSTOMER_TRX_ID = d.CUSTOMER_TRX_ID)
SELECT o.HEADER_ID AS order_id, ROUND(g.remaining, 2) AS open_usd,
       ROUND(g.own_unapplied, 2) AS own_unapplied_usd, g.collectible AS collectible_usd
FROM gap g
JOIN OE_ORDER_HEADERS_ALL o ON o.ORDER_NUMBER = CAST(g.order_number AS BIGINT)
WHERE g.collectible > 5.00
ORDER BY order_id"""}),

    # 4. Report the position and file one collection attempt per collectible invoice.
    ("run_python", {"code": """\
import pandas as pd
from mission_control import MissionControl, Reason

open_items = pd.read_parquet("results/sql_0001.parquet")
receipts = pd.read_parquet("results/sql_0002.parquet")
collect = pd.read_parquet("results/sql_0003.parquet")

orders = open_items[open_items["source"] == "ORDER ENTRY"]
aged = orders[orders["over_90_days_past_due"]]
cash = receipts[(receipts["receipt_type"] == "CASH") & (receipts["receipt_status"] != "REV")]
metrics = [
    {"name": "open_invoices_count", "value": int(orders["open_invoices"].sum()), "unit": "count"},
    {"name": "open_invoices_usd", "value": round(float(orders["remaining_usd"].sum()), 2)},
    {"name": "unapplied_cash_usd", "value": round(float(cash["unapplied_usd"].sum()), 2)},
    {"name": "collectible_invoices_count", "value": len(collect), "unit": "count"},
    {"name": "collectible_usd", "value": round(float(collect["collectible_usd"].sum()), 2)},
]
for m in metrics:
    print(f"{m['name']:28s} {m['value']:>16,}")
print(f"naive 90+ day list: {int(aged['open_invoices'].sum()):,} invoices, "
      f"${aged['remaining_usd'].sum():,.2f}; collectible after own-receipt cash: {len(collect):,} "
      f"(of which {int((collect['own_unapplied_usd'] > 0).sum())} partly offset)")
assert collect["order_id"].is_unique

mission_control = MissionControl()
mission_control.report_metrics(metrics)
mission_control.remediate_payments(
    items=[{"order_id": int(r.order_id), "amount": round(float(r.collectible_usd), 2)}
           for r in collect.itertuples()],
    action="recapture", reason=Reason.OTHER)
mission_control.note(
    "Open items: AR_PAYMENT_SCHEDULES_ALL CLASS='INV', STATUS='OP', order invoices only "
    "(INTERFACE_HEADER_CONTEXT='ORDER ENTRY'; membership OKS CONTRACTS invoices left out of every "
    "figure). Unapplied cash: receipt AMOUNT less its CASH/APP applications, cash receipts only, "
    "reversed receipts excluded. Collections' list: open order invoices due before 2024-10-02 "
    "(more than 90 days past due at 2024-12-31), less the unapplied cash on the receipts applied "
    "to that same invoice (each receipt once), kept when more than $5.00 remains; "
    f"{len(collect)} invoices, ${collect['collectible_usd'].sum():,.2f}, out of a naive 90+ day "
    f"list of {int(aged['open_invoices'].sum()):,}. Filed by the order each invoice bills "
    "(INTERFACE_HEADER_ATTRIBUTE1 = ORDER_NUMBER -> HEADER_ID).")
mission_control.summary()
"""}),
]
