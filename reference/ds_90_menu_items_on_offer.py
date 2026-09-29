"""Reference solution for ds-90-menu-items-on-offer.

The question asks how big the menu catalogue customers could order from was at the close of
each month of 2024: the menu items on offer and the storefronts they belong to. The trap is
that the item master cannot say what is on offer. ``MTL_SYSTEM_ITEMS_B`` keeps every item
ever synced, and its status never moves: every menu item is ``Active`` with
``ENABLED_FLAG = 'Y'``, including the menus of storefronts that have since left the
platform. Counting active item rows created by the month's close overcounts, more each month
as storefronts leave.

An item is on offer while its storefront is on the platform. The storefront's supplier record
carries that: ``AP_SUPPLIERS`` (``VENDOR_TYPE_LOOKUP_CODE = 'MERCHANT'``) with
``START_DATE_ACTIVE`` on or before the month end and ``END_DATE_ACTIVE`` empty or after it
(the end date is the day it came off). Items reach their storefront through the inventory
organisation: ``HR_ORGANIZATION_INFORMATION`` (context ``XX_STOREFRONT``) maps the item's
``ORGANIZATION_ID`` to the supplier's ``VENDOR_ID`` in ``ORG_INFORMATION1``. Menu items are
``ITEM_TYPE = 'FG'``, every section; the ``SVC`` rows are the platform's fee and tip lines.

Score: 1.0 on the paper's warehouse and 1.0 on the released warehouse.
"""

QUESTION = "ds-90-menu-items-on-offer"

_CATALOGUE = """\
WITH month_ends AS (
  {month_ends}
), menus AS (
  -- Each storefront's menu: its menu items, every section.
  SELECT CAST(o.ORG_INFORMATION1 AS INT64) AS vendor_id, COUNT(*) AS items
  FROM MTL_SYSTEM_ITEMS_B i
  JOIN HR_ORGANIZATION_INFORMATION o
    ON o.ORGANIZATION_ID = i.ORGANIZATION_ID AND o.ORG_INFORMATION_CONTEXT = 'XX_STOREFRONT'
  WHERE i.ITEM_TYPE = 'FG'
  GROUP BY 1
)
SELECT e.month_end, SUM(m.items) AS menu_items, COUNT(*) AS storefronts
FROM month_ends e
JOIN AP_SUPPLIERS s
  ON s.START_DATE_ACTIVE <= e.month_end
 AND (s.END_DATE_ACTIVE IS NULL OR s.END_DATE_ACTIVE > e.month_end)
JOIN menus m ON m.vendor_id = s.VENDOR_ID
WHERE s.VENDOR_TYPE_LOOKUP_CODE = 'MERCHANT'
GROUP BY e.month_end
ORDER BY e.month_end"""

STEPS = [
    # 1. What the item master says: item type and status, and how many of the items belong
    #    to storefronts whose supplier record has been end-dated. Every row is Active, so the
    #    status cannot tell what is still on offer; the storefront's dates have to.
    ("run_sql", {"sql": """\
SELECT i.ITEM_TYPE AS item_type, i.ENABLED_FLAG AS enabled_flag,
       i.INVENTORY_ITEM_STATUS_CODE AS status, COUNT(*) AS items,
       SUM(CASE WHEN s.END_DATE_ACTIVE IS NOT NULL THEN 1 ELSE 0 END)
         AS items_of_end_dated_storefronts
FROM MTL_SYSTEM_ITEMS_B i
LEFT JOIN HR_ORGANIZATION_INFORMATION o
  ON o.ORGANIZATION_ID = i.ORGANIZATION_ID AND o.ORG_INFORMATION_CONTEXT = 'XX_STOREFRONT'
LEFT JOIN AP_SUPPLIERS s ON s.VENDOR_ID = CAST(o.ORG_INFORMATION1 AS INT64)
GROUP BY 1, 2, 3
ORDER BY items DESC"""}),

    # 2. At each month end, the menus of the storefronts on the platform that day: started on
    #    or before it and not yet end-dated.
    ("run_sql", {"sql": {
        "bigquery": _CATALOGUE.format(month_ends="""\
SELECT LAST_DAY(m) AS month_end
  FROM UNNEST(GENERATE_DATE_ARRAY(DATE '2024-01-01', DATE '2024-12-01', INTERVAL 1 MONTH)) AS m"""),
        "duckdb": _CATALOGUE.format(month_ends="""\
SELECT CAST(last_day(m) AS DATE) AS month_end
  FROM generate_series(DATE '2024-01-01', DATE '2024-12-01', INTERVAL 1 MONTH) AS t(m)"""),
    }}),

    # 3. Shape to the contract and publish.
    ("run_python", {"code": """\
import pandas as pd
from mission_control import MissionControl

df = pd.read_parquet("results/sql_0002.parquet")
df["month"] = pd.to_datetime(df["month_end"]).dt.strftime("%Y-%m")
df["menu_items"] = df["menu_items"].astype(int)
df["storefronts"] = df["storefronts"].astype(int)
df = df[["month", "menu_items", "storefronts"]].sort_values("month").reset_index(drop=True)
print(df.to_string(index=False))

mission_control = MissionControl()
mission_control.publish_data_sources([
    {"name": "menu_catalogue_month_end_2024", "frame": df},
])
mission_control.summary()
"""}),
]
