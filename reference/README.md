# Reference solutions

Twenty questions have a reference solution here: the four that the paper and its website walk
through, and sixteen more. `smoke_01_console.py` replays the no-data smoke question, which
checks the replay path itself.

Each file is one question. Its docstring says what the question asks, what it leaves out, the
rule the solution files and the tables that carry it. `STEPS` is the sequence of tool calls an
agent would make (`run_sql`, `list_tables`, `describe_table`, `run_python`), in order. The
k-th `run_sql` saves its result as `results/sql_000k.parquet`, and the last `run_python` files
the answer through Mission Control. A statement the two engines spell differently is given
once per engine. A question whose prompt names entities drawn from the warehouse defines
`steps(prompt)` instead and reads them from the prompt.

## Replaying

`-T solver=reference` replays a question's reference through the same two servers a model
gets, with no model. What it files is kept in the sample's store like any run's, and
`inspect view` shows the calls and their results:

```bash
.venv/bin/inspect eval argo_bench/task.py -T solver=reference \
    -T questions=ds-22-margin-monthly-per-order --model mockllm/model
.venv/bin/inspect eval argo_bench/task.py -T solver=reference -T questions=reference \
    --model mockllm/model                                  # all twenty
.venv/bin/inspect eval argo_bench/task.py -T solver=reference -T questions=smoke \
    --model mockllm/model                                  # no data needed
```

`-T engine=bigquery` (with `database`, `credentials`) replays them on BigQuery. Every
statement stays under the 20 GiB per-query scan cap except three of ds-22's, which read the
whole year's books: replay that one on DuckDB.

## The solutions

Scores are the benchmark's grade (0 to 1) of what the reference files, on the paper's
warehouse and on the released one, each against its own held-out key.

| question | area | what it solves | paper's | released |
| --- | --- | --- | --- | --- |
| `col-10-refund-partnerships` | Fraud | couriers who split never-arrived refunds with their regulars: who a regular is, and how many make a partnership | 0.98 | 0.94 |
| `ds-22-margin-monthly-per-order` | Dashboards | Finance's contribution margin per order, from the journal lines the books tie to each order | 1.00 | 1.00 |
| `fc-12-true-ups-may` | Forecasting | May's minimum-pay true-ups once the dispatcher cuts the hours: the hours set what is owed, demand sets the pay | 0.94 | 0.87 |
| `mer-71-silent-takeovers` | Fraud | storefronts whose payout account was taken over the way the reported ones were, and never put back | 1.00 | 1.00 |
| `rem-01-uncompensated-failures` | Compliance | orders that failed through no fault of the customer, refunded at what was charged | 1.00 | 1.00 |
| `cf-02-balance-sheet-reconciliation` | Core Finance | summary accounts against the sum of their base accounts, every period of 2024 | 1.00 | 1.00 |
| `dhc-01-closure-order-sales` | Core Finance | storefronts that kept selling while a health closure order was in force | 1.00 | 1.00 |
| `nec-01-courier-1099-nec` | Core Finance | the courier 1099-NEC run: who gets a form, and what it shows | 1.00 | 1.00 |
| `ds-33-orders-by-rating-shown` | Dashboards | orders by the star rating the storefront was showing when they were placed | 1.00 | 1.00 |
| `ds-34-commission-by-plan` | Dashboards | storefront commission by plan and component, at the plan in force when the order was placed | 1.00 | 1.00 |
| `ds-83-busy-kitchen-holds` | Dashboards | orders Busy Kitchen held, and the minutes it added to their quoted prep times | 1.00 | 1.00 |
| `ds-90-menu-items-on-offer` | Dashboards | the menu catalogue customers could order from at each month end | 1.00 | 1.00 |
| `pe-07-instant-pay-price` | Dashboards | what couriers paid to cash out early, and how much sooner the money came | 1.00 | 1.00 |
| `pe-11-support-customer-exposure` | Dashboards | the refund desk's backlog in customer-hours: simultaneous and week-old claims | 1.00 | 1.00 |
| `fin-01-collections-past-due` | Finance & Accounting | invoices over 90 days past due, net of unapplied cash on their own receipts | 1.00 | 1.00 |
| `fin-07-ar-to-gl-reconciliation` | Finance & Accounting | the receivable account against the subledger at year end, with January settled back out | 1.00 | 1.00 |
| `fin-15-customer-credit-liabilities` | Finance & Accounting | refund and credit payables against the subledger at year end | 1.00 | 1.00 |
| `mer-14-platform-funded-at-shells` | Fraud | what the platform funded at the shell storefronts trust & safety confirmed | 1.00 | 1.00 |
| `rng-01-bust-out-holds-sep` | Fraud | the storefronts laundering stolen cards right now: ramp on new cards, still paid, not yet dark | 1.00 | 1.00 |
| `rng-30-silent-victims-make-whole` | Fraud | what each silently hijacked storefront is owed to be made whole | 1.00 | 1.00 |
