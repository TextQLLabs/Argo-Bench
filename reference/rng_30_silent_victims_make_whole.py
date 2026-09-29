"""Reference solution for rng-30-silent-victims-make-whole.

The storefronts whose payout account was switched to an attacker's account through the
merchant portal and which never reported it, so support never put it back, and what we owe
each of them: every payout run that went to the substituted account, from the switch through
the end of the year. Finding them is the question ``mer_71_silent_takeovers.py`` answers, and
this reference uses its rule as it stands: over the 2024 portal changes of a payout account
that were never restored, the contact e-mail changed within three days before the bank account,
or the new account is one a change of that kind had put on another storefront and that was
still there.

The new part is the amount. A payout run is a merchant payment in ``AP_CHECKS_ALL`` (one per
storefront and weekly run, ``CHECK_DATE`` the day it is paid). None of these storefronts' payout
accounts changed again after the switch, so every run paid after the switch went to the
substituted account, up to the last run of the year. The trap is the run dated the day of the
switch: it is paid at the start of that day and was cut the day before, so it went to the
storefront's own account, before the attacker's change later that day. The rule filed: per
storefront, the sum of the non-voided payout runs paid at or after the switch and before
1 January 2025. Every step but the last returns counts only; the last returns the storefronts
owed, never the attacker's accounts.

Score: 1.0 on the paper's warehouse and 1.0 on the released warehouse.
"""

import importlib.util
from pathlib import Path

QUESTION = "rng-30-silent-victims-make-whole"

_spec = importlib.util.spec_from_file_location(
    "mer_71_silent_takeovers", Path(__file__).with_name("mer_71_silent_takeovers.py"))
_mer71 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mer71)

STEPS = [
    # 1-3. mer-71's calls: the payout-account changes in the audit trail, the reported
    #      takeovers' pattern (the e-mail changed a day or two before the bank account), and
    #      why a shared account is not the tell on its own. Counts only.
    *_mer71.STEPS[:3],

    # 4. The victims by mer-71's rule, and what each was paid after the switch: the payout
    #    runs paid at or after it through the end of the year, the run paid earlier on the
    #    switch day (to the storefront's own account), and any later change of the account.
    ("run_sql", {"sql": _mer71._sql("""\
, victims AS (
  SELECT payee_key, VENDOR_ID, MIN(changed_at) AS switched_at
  FROM rule
  WHERE restored_at IS NULL AND (email_tell OR onto_taken_account)
  GROUP BY payee_key, VENDOR_ID
),
later AS (
  SELECT v.payee_key, COUNT(a.AUDIT_ID) AS later_account_changes
  FROM victims v
  LEFT JOIN XX_AUDIT_TRAIL a
    ON a.SOURCE_TABLE = 'IBY_PMT_INSTR_USES_ALL' AND a.SOURCE_KEY_VALUE = v.payee_key
   AND a.CHANGED_DATE > v.switched_at
  GROUP BY v.payee_key
),
runs AS (
  SELECT VENDOR_ID, CHECK_DATE, AMOUNT
  FROM AP_CHECKS_ALL
  WHERE STATUS_LOOKUP_CODE <> 'VOIDED'
    AND CHECK_DATE >= '2024-01-01' AND CHECK_DATE < '2025-01-01'
    AND VENDOR_ID IN (SELECT VENDOR_ID FROM victims)
)
SELECT v.VENDOR_ID, v.switched_at, l.later_account_changes,
       COUNT(CASE WHEN r.CHECK_DATE >= v.switched_at THEN 1 END) AS payout_runs,
       COUNT(CASE WHEN CAST(r.CHECK_DATE AS DATE) = CAST(v.switched_at AS DATE)
                   AND r.CHECK_DATE < v.switched_at THEN 1 END) AS runs_earlier_on_switch_day,
       MAX(r.CHECK_DATE) AS last_run,
       ROUND(SUM(CASE WHEN r.CHECK_DATE >= v.switched_at THEN r.AMOUNT ELSE 0 END), 2)
         AS make_whole_usd
FROM victims v
JOIN later l ON l.payee_key = v.payee_key
LEFT JOIN runs r ON r.VENDOR_ID = v.VENDOR_ID
GROUP BY v.VENDOR_ID, v.switched_at, l.later_account_changes
ORDER BY v.VENDOR_ID""")}),

    # 5. File the make-whole schedule and the method.
    ("run_python", {"code": """\
import pandas as pd
from mission_control import MissionControl

owed = pd.read_parquet("results/sql_0004.parquet")
owed["make_whole_usd"] = owed["make_whole_usd"].astype(float).round(2)
print(f"{len(owed)} storefronts, ${owed['make_whole_usd'].sum():,.2f} owed over "
      f"{int(owed['payout_runs'].sum())} payout runs "
      f"(${owed['make_whole_usd'].min():,.2f} to ${owed['make_whole_usd'].max():,.2f} each); "
      f"{int(owed['later_account_changes'].sum())} later account changes; "
      f"{int(owed['runs_earlier_on_switch_day'].sum())} runs paid earlier on a switch day "
      "left out")

mission_control = MissionControl()
mission_control.file_schedules([{
    "name": "make_whole", "key_columns": ["vendor_id"],
    "rows": [{"vendor_id": int(r.VENDOR_ID), "make_whole_usd": float(r.make_whole_usd)}
             for r in owed.itertuples()],
}])
mission_control.note(
    "Victims: 2024 portal changes of a payout account (XX_AUDIT_TRAIL on "
    "IBY_PMT_INSTR_USES_ALL) that support never restored, where the contact e-mail changed "
    "within three days before the bank change (as in every reported takeover) or the new "
    "account was one such a change had put on another storefront. Amount: the non-voided "
    "merchant payout runs (AP_CHECKS_ALL) paid at or after the switch through 31 December; "
    "none of these accounts changed again, and the run dated the switch day was paid before "
    "the switch, to the storefront's own account.")
mission_control.summary()
"""}),
]
