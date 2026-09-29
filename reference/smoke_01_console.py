"""Reference solution for smoke-01-console, the no-data check of the whole path.

The prompt spells out every filing, so the reference is the prompt's own code in one cell.
Replaying it checks the replay path end to end without a warehouse:

    inspect eval argo_bench/task.py -T solver=reference -T questions=smoke --model mockllm/model

Score: conformance 1.
"""

QUESTION = "smoke-01-console"

STEPS = [
    ("run_python", {"code": """\
from mission_control import MissionControl, Reason

mission_control = MissionControl()
mission_control.report_metrics([{"name": "smoke_alpha", "value": 24},
                                {"name": "smoke_beta", "value": 1234.56, "unit": "usd"}],
                               unit="count")
mission_control.end_campaigns([9912], reason=Reason.NEGATIVE_CONTRIBUTION_MARGIN)
mission_control.report_balances([{"account": "2999", "amount": -268334.05}],
                                as_of="2024-07-31 23:59:59", basis="gl")
total = sum([10.0, 14.0])
mission_control.report_metrics([{"name": "smoke_computed", "value": total}], unit="count")
print(mission_control.summary())
print(mission_control.status())
"""}),
]
