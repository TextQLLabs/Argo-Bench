"""Reference solution for pe-07-instant-pay-price.

The question asks, for each month of Q3 2024 by the day instant cash reached the courier,
how many instant cash-outs were received, the cash received after the fee, the fees, and how
much sooner the money arrived than the pay period's scheduled weekly payday (dollar-days, the
cash-weighted days early, and the fee as a simple annual rate on those dollar-days). The traps
are the grains: one payment run (check) carries several cash-outs, so its amount is not one
cash-out's cash and a fee joined at that grain is counted more than once; and the comparison is
with the scheduled payday of the period the earnings belong to, not the day the cash-out was
requested, the day it cleared the bank, or when the rest of the week's pay actually went out.

An instant cash-out is an ``AP_INVOICES_ALL`` prepayment in pay group ``COURIER_INSTANT``: its
amount is the cash net of the fee, ``ATTRIBUTE3`` the fee (the invoice's ``INSTANT CASHOUT FEE``
line), ``ATTRIBUTE1`` the courier's weekly pay period (``XX_PAYOUT_PERIODS``, whose
``PAYOUT_DATE`` is the scheduled payday). The cash reached the courier on the paying check's
``CHECK_DATE`` (``AP_INVOICE_PAYMENTS_ALL`` → ``AP_CHECKS_ALL``), New York dates already. Days
early = payday minus receipt day in calendar days; everything is summed per cash-out in SQL and
the two ratios are taken of the monthly sums.

Score: 1.0 on the paper's warehouse and 1.0 on the released warehouse.
"""

QUESTION = "pe-07-instant-pay-price"

_MONTHLY = """\
SELECT EXTRACT(YEAR FROM c.CHECK_DATE) AS year, EXTRACT(MONTH FROM c.CHECK_DATE) AS month_num,
       COUNT(*) AS cashouts,
       SUM(p.AMOUNT) AS net_cash_received_usd,
       SUM(CAST(i.ATTRIBUTE3 AS DOUBLE_T)) AS fees_usd,
       SUM(p.AMOUNT * {days_early}) AS cash_acceleration_dollar_days
FROM AP_INVOICES_ALL i
JOIN AP_INVOICE_PAYMENTS_ALL p ON p.INVOICE_ID = i.INVOICE_ID
JOIN AP_CHECKS_ALL c ON c.CHECK_ID = p.CHECK_ID
JOIN XX_PAYOUT_PERIODS w ON w.PAYOUT_PERIOD_ID = CAST(i.ATTRIBUTE1 AS INT_T)
WHERE i.PAY_GROUP_LOOKUP_CODE = 'COURIER_INSTANT' AND i.INVOICE_TYPE_LOOKUP_CODE = 'PREPAYMENT'
  AND c.CHECK_DATE >= '2024-07-01' AND c.CHECK_DATE < '2024-10-01'
GROUP BY year, month_num
ORDER BY year, month_num"""

_DIALECT = {
    "bigquery": {"days_early": "DATE_DIFF(DATE(w.PAYOUT_DATE), DATE(c.CHECK_DATE), DAY)",
                 "DOUBLE_T": "FLOAT64", "INT_T": "INT64"},
    "duckdb": {"days_early": "date_diff('day', CAST(c.CHECK_DATE AS DATE), CAST(w.PAYOUT_DATE AS DATE))",
               "DOUBLE_T": "DOUBLE", "INT_T": "BIGINT"},
}


def _sql(template: str) -> dict:
    out = {}
    for engine, d in _DIALECT.items():
        sql = template.replace("DOUBLE_T", d["DOUBLE_T"]).replace("INT_T", d["INT_T"])
        out[engine] = sql.format(days_early=d["days_early"])
    return out


STEPS = [
    # 1. What kinds of payables the payout feed books: the instant cash-outs are the
    #    prepayments in their own pay group.
    ("run_sql", {"sql": """\
SELECT SOURCE, INVOICE_TYPE_LOOKUP_CODE, PAY_GROUP_LOOKUP_CODE, COUNT(*) AS invoices
FROM AP_INVOICES_ALL
GROUP BY SOURCE, INVOICE_TYPE_LOOKUP_CODE, PAY_GROUP_LOOKUP_CODE
ORDER BY invoices DESC"""}),

    # 2. Check the grains before summing: one payment per cash-out, several cash-outs per check
    #    (so a check's amount is not a cash-out's cash), the fee on the invoice (ATTRIBUTE3)
    #    agrees with its fee line, and ATTRIBUTE1 names a weekly courier pay period.
    ("run_sql", {"sql": _sql("""\
WITH fee_lines AS (
  SELECT INVOICE_ID, -SUM(AMOUNT) AS fee_line
  FROM AP_INVOICE_LINES_ALL
  WHERE DESCRIPTION = 'INSTANT CASHOUT FEE'
  GROUP BY INVOICE_ID
)
SELECT COUNT(*) AS payments, COUNT(DISTINCT i.INVOICE_ID) AS cashouts,
       COUNT(DISTINCT c.CHECK_ID) AS checks,
       SUM(CASE WHEN ABS(p.AMOUNT - i.INVOICE_AMOUNT) > 0.005 THEN 1 ELSE 0 END)
         AS paid_not_invoice_amount,
       SUM(CASE WHEN f.fee_line IS NULL
                  OR ABS(CAST(i.ATTRIBUTE3 AS DOUBLE_T) - f.fee_line) > 0.005 THEN 1 ELSE 0 END)
         AS fee_not_fee_line,
       SUM(CASE WHEN w.PAYOUT_PERIOD_ID IS NULL THEN 1 ELSE 0 END) AS no_pay_period,
       MIN(w.PARTY_TYPE_CODE) AS party_min, MAX(w.PARTY_TYPE_CODE) AS party_max,
       MIN(w.CADENCE_DAYS) AS cadence_min, MAX(w.CADENCE_DAYS) AS cadence_max,
       MIN({days_early}) AS days_early_min, MAX({days_early}) AS days_early_max
FROM AP_INVOICES_ALL i
JOIN AP_INVOICE_PAYMENTS_ALL p ON p.INVOICE_ID = i.INVOICE_ID
JOIN AP_CHECKS_ALL c ON c.CHECK_ID = p.CHECK_ID
LEFT JOIN fee_lines f ON f.INVOICE_ID = i.INVOICE_ID
LEFT JOIN XX_PAYOUT_PERIODS w ON w.PAYOUT_PERIOD_ID = CAST(i.ATTRIBUTE1 AS INT_T)
WHERE i.PAY_GROUP_LOOKUP_CODE = 'COURIER_INSTANT' AND i.INVOICE_TYPE_LOOKUP_CODE = 'PREPAYMENT'
  AND c.CHECK_DATE >= '2024-07-01' AND c.CHECK_DATE < '2024-10-01'""")}),

    # 3. Per month the cash reached the courier: cash-outs, net cash, fees, dollar-days.
    ("run_sql", {"sql": _sql(_MONTHLY)}),

    # 4. The two ratios come from the monthly sums; round as asked and publish.
    ("run_python", {"code": """\
import pandas as pd
from mission_control import MissionControl

print(pd.read_parquet("results/sql_0002.parquet").T.to_string(header=False), "\\n")

m = pd.read_parquet("results/sql_0003.parquet")
df = pd.DataFrame({
    "month": [f"{int(y):04d}-{int(n):02d}" for y, n in zip(m["year"], m["month_num"])],
    "cashouts": m["cashouts"].astype(int),
})
net = m["net_cash_received_usd"].astype(float)
fees = m["fees_usd"].astype(float)
dollar_days = m["cash_acceleration_dollar_days"].astype(float)
df["net_cash_received_usd"] = net.round(2)
df["fees_usd"] = fees.round(2)
df["cash_acceleration_dollar_days"] = dollar_days.round(2)
df["cash_weighted_days_early"] = (dollar_days / net).round(6)
df["annualized_fee_rate"] = (365 * fees / dollar_days).round(6)
df = df.sort_values("month").reset_index(drop=True)
print(df.to_string(index=False))

mission_control = MissionControl()
mission_control.publish_data_sources([{"name": "instant_pay_price_monthly", "frame": df}])
mission_control.summary()
"""}),
]
