"""Reference solution for nec-01-courier-1099-nec.

A 1099-NEC reports what a courier was paid in the calendar year, gross of the fee we charge
for an instant cash-out. Couriers are paid two ways, and the payables ledger books them
differently: a weekly STANDARD invoice (pay group ``COURIER_WEEKLY``) for everything earned
that week, paid the Tuesday after, net of what the courier already drew; and a PREPAYMENT
invoice per instant cash-out (``COURIER_INSTANT``), an ``INSTANT CASHOUT`` line less an
``INSTANT CASHOUT FEE`` line, paid net the same day. The traps: summing the payments nets
the fee out of every cash-out; summing the weekly invoices dated 2024 is the earned basis
(the last week of December is paid in January 2025, while the cash-outs drawn against it
were paid in 2024); and adding weekly and prepayment invoices together counts every
cash-out twice.

The rule filed: cash basis, by check date in 2024 (``AP_CHECKS_ALL.CHECK_DATE``). Each
courier's reportable amount is what those checks paid on the courier's invoices
(``AP_INVOICE_PAYMENTS_ALL``), with the cash-out fee on each prepayment invoice added back
(so a cash-out counts at its gross ``INSTANT CASHOUT`` amount). A courier gets a form at $600
or more. Filed: ``couriers_filed``, ``reported_compensation_usd`` and one metric per courier
the prompt names (their VENDOR_IDs, read from the prompt), 0 for those under $600.

Score: 1.0 on the paper's warehouse and 1.0 on the released warehouse.
"""

import re

QUESTION = "nec-01-courier-1099-nec"

#: Each courier's pay disbursed in 2024, gross of the cash-out fee, to the cent.
_PAID = """\
WITH fee AS (
  SELECT INVOICE_ID, SUM(AMOUNT) AS fee
  FROM AP_INVOICE_LINES_ALL WHERE DESCRIPTION = 'INSTANT CASHOUT FEE' GROUP BY INVOICE_ID
),
paid AS (
  SELECT c.VENDOR_ID, ROUND(SUM(p.AMOUNT - COALESCE(f.fee, 0)), 2) AS paid_usd
  FROM AP_INVOICE_PAYMENTS_ALL p
  JOIN AP_CHECKS_ALL c ON c.CHECK_ID = p.CHECK_ID
  JOIN AP_INVOICES_ALL i ON i.INVOICE_ID = p.INVOICE_ID
  LEFT JOIN fee f ON f.INVOICE_ID = p.INVOICE_ID
  WHERE i.PAY_GROUP_LOOKUP_CODE IN ('COURIER_WEEKLY', 'COURIER_INSTANT')
    AND c.CHECK_DATE >= '2024-01-01' AND c.CHECK_DATE < '2025-01-01'
  GROUP BY c.VENDOR_ID
)"""


def _named(prompt: str) -> list[int]:
    """The couriers the prompt asks about: the list of VENDOR_IDs that closes it."""
    return [int(v) for v in re.findall(r"\d+", prompt.rsplit(":", 1)[1])]


def steps(prompt: str) -> list:
    named = _named(prompt)
    ids = ", ".join(str(v) for v in named)
    return [
        # 1. How couriers are paid in the payables ledger: invoices by pay group and type,
        #    and what their lines carry.
        ("run_sql", {"sql": """\
SELECT i.PAY_GROUP_LOOKUP_CODE, i.INVOICE_TYPE_LOOKUP_CODE, l.DESCRIPTION,
       COUNT(*) AS lines, ROUND(SUM(l.AMOUNT), 2) AS amount
FROM AP_INVOICE_LINES_ALL l
JOIN AP_INVOICES_ALL i ON i.INVOICE_ID = l.INVOICE_ID
WHERE i.PAY_GROUP_LOOKUP_CODE LIKE 'COURIER%'
GROUP BY 1, 2, 3
ORDER BY 1, 2, 3"""}),

        # 2. When the money went out: payments on courier invoices by pay group and check
        #    year. The weekly payment is net of the week's cash-outs, and the last week of
        #    2024 is paid in January 2025.
        ("run_sql", {"sql": """\
SELECT i.PAY_GROUP_LOOKUP_CODE, c.STATUS_LOOKUP_CODE,
       EXTRACT(YEAR FROM c.CHECK_DATE) AS check_year, COUNT(*) AS payments,
       ROUND(SUM(p.AMOUNT), 2) AS paid, MIN(c.CHECK_DATE) AS first_check,
       MAX(c.CHECK_DATE) AS last_check
FROM AP_INVOICE_PAYMENTS_ALL p
JOIN AP_CHECKS_ALL c ON c.CHECK_ID = p.CHECK_ID
JOIN AP_INVOICES_ALL i ON i.INVOICE_ID = p.INVOICE_ID
WHERE i.PAY_GROUP_LOOKUP_CODE LIKE 'COURIER%'
GROUP BY 1, 2, 3
ORDER BY 1, 2, 3"""}),

        # 3. The form run: couriers paid $600 or more in 2024 and the total their forms show.
        ("run_sql", {"sql": _PAID + """
SELECT COUNT(*) AS couriers_paid,
       COUNT(CASE WHEN paid_usd >= 600 THEN 1 END) AS couriers_filed,
       ROUND(SUM(CASE WHEN paid_usd >= 600 THEN paid_usd ELSE 0 END), 2)
         AS reported_compensation_usd
FROM paid"""}),

        # 4. What the form will say for each courier who asked.
        ("run_sql", {"sql": _PAID + f"""
SELECT VENDOR_ID, paid_usd FROM paid
WHERE VENDOR_ID IN ({ids})
ORDER BY VENDOR_ID"""}),

        # 5. File the run's two figures and one metric per courier asked about.
        ("run_python", {"code": f"""\
import pandas as pd
from mission_control import MissionControl

run = pd.read_parquet("results/sql_0003.parquet").iloc[0]
asked = pd.read_parquet("results/sql_0004.parquet").set_index("VENDOR_ID")["paid_usd"]
named = {named!r}
form = {{v: float(asked.get(v, 0.0)) for v in named}}
form = {{v: (amount if amount >= 600 else 0.0) for v, amount in form.items()}}
print(f"{{int(run.couriers_filed):,}} forms, ${{float(run.reported_compensation_usd):,.2f}} "
      f"reported; {{sum(a > 0 for a in form.values())}} of the {{len(named)}} couriers asked "
      "about get one")

mission_control = MissionControl()
mission_control.report_metrics(
    [{{"name": "couriers_filed", "value": int(run.couriers_filed), "unit": "count"}},
     {{"name": "reported_compensation_usd", "value": float(run.reported_compensation_usd)}}]
    + [{{"name": str(v), "value": amount}} for v, amount in form.items()])
mission_control.summary()
"""}),
    ]
