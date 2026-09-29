"""Reference solution for fin-15-customer-credit-liabilities.

The question asks for two year-end proofs: customer refund payable (2105) and customer
credit payable (2107), each equal to the sum of its Receivables components at December 31,
from a subledger that shows today's balances. It offers ten labels, four of which belong to
neither proof: the on-account credit balances and the open refund memos are today's figures
(and span both accounts), card refunds already settled are a movement that has left 2105,
and lost disputes sit in chargeback payable (2106). The traps are the split memo (one refund
credit memo books part of itself to 2105 and part to 2107) and the January activity that
has to be backed out.

The split is on the memo's receivable distributions (``RA_CUST_TRX_LINE_GL_DIST_ALL``,
``ACCOUNT_CLASS = 'REC'``) by natural account (``GL_CODE_COMBINATIONS.SEGMENT3``), dated by
``GL_DATE``. ``XX_REFUNDS.CREDIT_MEMO_TRX_ID`` names each refund's memo and its channel (card
refund or platform credit); a memo with no refund record is a referral credit, as its lines
say. 2105 carries each refund's cash share until the negative receipt that pays it
(``AR_CASH_RECEIPTS_ALL``, activity "Credit Card Refund") clears
(``AR_CASH_RECEIPT_HISTORY_ALL.STATUS = 'CLEARED'``): what is left at year-end is the card
refunds not cleared by then plus the platform credits' cash share, which no receipt ever
settles. 2107 carries the spendable share of every memo less what was spent by year-end
(``AR_RECEIVABLE_APPLICATIONS_ALL``, ``APPLICATION_TYPE = 'CM'``, ``APPLY_DATE``), split into
referral credit and the credit remainders of refunds. The ledger side is ``GL_JE_LINES``
effective on or before December 31, posted or not.

Score: 1.0 on the paper's warehouse and 1.0 on the released warehouse.
"""

QUESTION = "fin-15-customer-credit-liabilities"

STEPS = [
    # 1. The ledger: the three customer payables at December 31, every journal effective by
    #    then, posted or unposted (2106 only to see what the chargebacks belong to).
    ("run_sql", {"sql": """\
SELECT c.SEGMENT3 AS account, h.JE_CATEGORY AS category, l.STATUS AS status,
       COUNT(*) AS lines,
       ROUND(SUM(COALESCE(l.ACCOUNTED_DR, 0) - COALESCE(l.ACCOUNTED_CR, 0)), 2) AS balance
FROM GL_JE_LINES l
JOIN GL_JE_HEADERS h ON h.JE_HEADER_ID = l.JE_HEADER_ID
JOIN GL_CODE_COMBINATIONS c ON c.CODE_COMBINATION_ID = l.CODE_COMBINATION_ID
WHERE c.SEGMENT3 IN ('2105', '2106', '2107') AND l.EFFECTIVE_DATE <= DATE '2024-12-31'
GROUP BY c.SEGMENT3, h.JE_CATEGORY, l.STATUS
ORDER BY account, category, status"""}),

    # 2. What kinds of credit memo there are: one month of memo lines against the refund
    #    record each memo belongs to. Refund records name card refunds and platform credits;
    #    the memos with no refund record are referral credits.
    ("run_sql", {"sql": """\
SELECT COALESCE(r.CHANNEL_CODE, 'NO_REFUND_RECORD') AS refund_channel,
       SUBSTR(ln.DESCRIPTION, 1, STRPOS(ln.DESCRIPTION, ' -') - 1) AS line_kind,
       COUNT(*) AS lines, ROUND(SUM(ln.EXTENDED_AMOUNT), 2) AS amount
FROM RA_CUSTOMER_TRX_LINES_ALL ln
JOIN RA_CUSTOMER_TRX_ALL t ON t.CUSTOMER_TRX_ID = ln.CUSTOMER_TRX_ID
JOIN RA_CUST_TRX_TYPES_ALL y ON y.CUST_TRX_TYPE_ID = t.CUST_TRX_TYPE_ID AND y.ORG_ID = t.ORG_ID
LEFT JOIN XX_REFUNDS r ON r.CREDIT_MEMO_TRX_ID = ln.CUSTOMER_TRX_ID
WHERE y.TYPE = 'CM'
  AND ln.CREATION_DATE >= '2024-12-01' AND ln.CREATION_DATE < '2025-01-01'
  AND t.CREATION_DATE >= '2024-12-01' AND t.CREATION_DATE < '2025-01-01'
GROUP BY refund_channel, line_kind
ORDER BY refund_channel, line_kind"""}),

    # 3. Each memo's share of 2105 and of 2107 from its receivable distributions dated by
    #    year-end (January memos drop out), and how much of it was spent by year-end.
    ("run_sql", {"sql": """\
WITH liability AS (
  SELECT CODE_COMBINATION_ID, SEGMENT3 AS account
  FROM GL_CODE_COMBINATIONS WHERE SEGMENT3 IN ('2105', '2107')),
memo AS (
  SELECT t.CUSTOMER_TRX_ID
  FROM RA_CUSTOMER_TRX_ALL t
  JOIN RA_CUST_TRX_TYPES_ALL y ON y.CUST_TRX_TYPE_ID = t.CUST_TRX_TYPE_ID AND y.ORG_ID = t.ORG_ID
  WHERE y.TYPE = 'CM'),
share AS (
  SELECT d.CUSTOMER_TRX_ID,
         SUM(CASE WHEN l.account = '2105' THEN d.AMOUNT ELSE 0 END) AS refund_payable,
         SUM(CASE WHEN l.account = '2107' THEN d.AMOUNT ELSE 0 END) AS credit_payable
  FROM RA_CUST_TRX_LINE_GL_DIST_ALL d
  JOIN liability l ON l.CODE_COMBINATION_ID = d.CODE_COMBINATION_ID
  WHERE d.ACCOUNT_CLASS = 'REC' AND d.GL_DATE <= DATE '2024-12-31'
  GROUP BY d.CUSTOMER_TRX_ID),
spent AS (
  SELECT CUSTOMER_TRX_ID,
         SUM(CASE WHEN APPLY_DATE <= DATE '2024-12-31' THEN AMOUNT_APPLIED ELSE 0 END)
           AS spent_by_year_end,
         SUM(AMOUNT_APPLIED) AS spent_to_date
  FROM AR_RECEIVABLE_APPLICATIONS_ALL
  WHERE APPLICATION_TYPE = 'CM' AND STATUS = 'APP'
  GROUP BY CUSTOMER_TRX_ID)
SELECT COALESCE(r.CHANNEL_CODE, 'NO_REFUND_RECORD') AS memo_kind,
       COUNT(*) AS credit_memos,
       ROUND(SUM(s.refund_payable), 2) AS refund_payable_share,
       ROUND(SUM(s.credit_payable), 2) AS credit_payable_share,
       ROUND(SUM(COALESCE(p.spent_by_year_end, 0)), 2) AS spent_by_year_end,
       ROUND(SUM(COALESCE(p.spent_to_date, 0)), 2) AS spent_to_date
FROM share s
JOIN memo m ON m.CUSTOMER_TRX_ID = s.CUSTOMER_TRX_ID
LEFT JOIN XX_REFUNDS r ON r.CREDIT_MEMO_TRX_ID = s.CUSTOMER_TRX_ID
LEFT JOIN spent p ON p.CUSTOMER_TRX_ID = s.CUSTOMER_TRX_ID
GROUP BY memo_kind
ORDER BY memo_kind"""}),

    # 4. The negative receipts dated by year-end, by activity, and whether their clearing
    #    (the processor's settlement advice) came by December 31 or after it.
    ("run_sql", {"sql": """\
WITH cleared AS (
  SELECT CASH_RECEIPT_ID, MIN(CAST(TRX_DATE AS DATE)) AS cleared_on
  FROM AR_CASH_RECEIPT_HISTORY_ALL
  WHERE STATUS = 'CLEARED'
  GROUP BY CASH_RECEIPT_ID)
SELECT a.NAME AS activity,
       CASE WHEN c.cleared_on <= DATE '2024-12-31' THEN 'settled by year-end'
            ELSE 'not settled by year-end' END AS settlement,
       COUNT(*) AS receipts, ROUND(SUM(r.AMOUNT), 2) AS amount
FROM AR_CASH_RECEIPTS_ALL r
JOIN AR_RECEIVABLES_TRX_ALL a ON a.RECEIVABLES_TRX_ID = r.RECEIVABLES_TRX_ID
LEFT JOIN cleared c ON c.CASH_RECEIPT_ID = r.CASH_RECEIPT_ID
WHERE r.TYPE = 'MISC' AND r.RECEIPT_DATE <= DATE '2024-12-31'
GROUP BY a.NAME, settlement
ORDER BY activity, settlement"""}),

    # 5. Assemble both proofs, check they tie to the cent, file.
    ("run_python", {"code": """\
import pandas as pd
from mission_control import MissionControl

gl = pd.read_parquet("results/sql_0001.parquet")
memos = pd.read_parquet("results/sql_0003.parquet").set_index("memo_kind")
receipts = pd.read_parquet("results/sql_0004.parquet").set_index(["activity", "settlement"])

balance = gl.groupby("account")["balance"].sum().round(2)
unspent = (memos["credit_payable_share"] + memos["spent_by_year_end"]).round(2)
card = "Credit Card Refund"
rows = {
    "gl_refund_payable": balance["2105"],
    "gl_credit_payable": balance["2107"],
    "card_refunds_in_flight": receipts.loc[(card, "not settled by year-end"), "amount"],
    "platform_credits_cash_share": memos.loc["PLATFORM_CREDIT", "refund_payable_share"],
    "referral_credits_unspent": unspent["NO_REFUND_RECORD"],
    "refund_credit_remainders_unspent": unspent[["INSTRUMENT_REFUND", "PLATFORM_CREDIT"]].sum(),
}
rows = {k: round(float(v), 2) for k, v in rows.items()}
diff_2105 = round(rows["gl_refund_payable"] - rows["card_refunds_in_flight"]
                  - rows["platform_credits_cash_share"], 2)
diff_2107 = round(rows["gl_credit_payable"] - rows["referral_credits_unspent"]
                  - rows["refund_credit_remainders_unspent"], 2)
# The card refunds' cash share, as the memos carry it, is exactly what their receipts pay.
card_cash = round(receipts.loc[card, "amount"].sum() -
                  memos.loc["INSTRUMENT_REFUND", "refund_payable_share"], 2)
for item, amount in rows.items():
    print(f"{item:34s} {amount:16,.2f}")
print(f"2105 difference {diff_2105:.2f}; 2107 difference {diff_2107:.2f}; "
      f"card refund receipts less the memos' 2105 share {card_cash:.2f}")

mission_control = MissionControl()
mission_control.file_schedules([{
    "name": "customer_credit_liabilities", "as_of": "2024-12-31", "key_columns": ["item"],
    "rows": [{"item": k, "amount": v} for k, v in rows.items()],
}])
f = lambda x: f"{x:,.2f}"
mission_control.note(
    f"2105 proof: gl_refund_payable {f(rows['gl_refund_payable'])} = card_refunds_in_flight "
    f"{f(rows['card_refunds_in_flight'])} + platform_credits_cash_share "
    f"{f(rows['platform_credits_cash_share'])}; difference {diff_2105:.2f}. "
    f"2107 proof: gl_credit_payable {f(rows['gl_credit_payable'])} = referral_credits_unspent "
    f"{f(rows['referral_credits_unspent'])} + refund_credit_remainders_unspent "
    f"{f(rows['refund_credit_remainders_unspent'])}; difference {diff_2107:.2f}. "
    "Ledger: GL_JE_LINES effective on or before 2024-12-31, posted and unposted, debits less "
    "credits. Each credit memo is split between 2105 and 2107 by its REC distributions dated by "
    "year-end; XX_REFUNDS names card refunds and platform credits, memos without a refund are "
    "referral credits. Card refunds stay in 2105 until their negative receipt clears; in flight "
    "= card refund receipts dated by year-end not cleared by then. Unspent credit = the 2107 "
    "share less CM applications dated by year-end. Left out: on_account_credit_balances and "
    "refund_credit_memos_open (today's subledger balances, which include January activity and "
    "span both accounts; the year-end open part of the refund memos is the remainders row), "
    "card_refunds_settled (a movement already out of 2105) and chargebacks_in_flight (lost "
    "disputes are payable from 2106, not 2105 or 2107).")
mission_control.summary()
"""}),
]
