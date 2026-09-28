# Argo-Bench

Argo-Bench measures whether an agent can do real operations work on an enterprise data
warehouse: investigate, compute, and then **act**. The warehouse is a simulated New York City
food-delivery company, exported in Oracle E-Business Suite form. The 210 questions range from
fraud rings and promotion economics to the month-end close, courier pay, dashboards and
forecasting. An agent is graded on what it **files** (bans, journal entries, balances,
forecasts, dashboard data sources), not on what it writes in its answer.

This repository is the minimum needed to reproduce the paper's runs: the questions, the
reference agent on [Inspect AI](https://inspect.aisi.org.uk), its two tool servers, the
sandboxes `run_python` executes in, and the model configurations. The warehouse is on
Hugging Face, [DoubleBlindAnon912/iclr-double-blind-dataset](https://huggingface.co/datasets/DoubleBlindAnon912/iclr-double-blind-dataset) (see *Data*). The answer keys are held out (see *Submitting results*).

## How a run works

Each question is one Inspect sample. The agent gets a system prompt
(`argo_bench/system_prompt.md`), the question, and the tools of two MCP servers:

| server | tools | |
| --- | --- | --- |
| warehouse | `run_sql`, `list_tables`, `describe_table` | read-only; one statement per call; every result also saved as Parquet |
| python | `run_python` | a persistent interpreter with pandas, polars, scipy, statsmodels, scikit-learn, OR-Tools |

The agent files its findings from Python through the **Mission Control** console
(`argo_bench/console/mission_control.py`, copied into each run's working directory):

```python
from mission_control import MissionControl, Reason
mission_control = MissionControl()
mission_control.ban_customers([101, 102], reason=Reason.PROMO_FARMING)
mission_control.summary()
```

Every filing is appended to the run's journal, which is stored in the Inspect log (sample
store, key `filings`). The agent loop (`argo_bench/agent.py`) is a plain tool loop: it calls
tools until it answers without one, with no planner, memory or retries.

A question with a data cutoff before December reads a warehouse that stops at the end of
that month: `food_delivery_9` is a schema of views over `food_delivery` that ends on 30
September 2024, and the system prompt says so. Queries that name any other schema are
refused.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[analysis,dev]'      # add ,bigquery for BigQuery
cp .env.example .env                             # add your model provider keys
.venv/bin/pytest                                 # offline checks, no keys or data needed
```

Check the whole path with the smoke question. It needs no data and no domain knowledge: the
prompt says exactly what to file, and the `conformance` score must be 1.

```bash
.venv/bin/inspect eval argo_bench/task.py -T questions=smoke --model anthropic/claude-haiku-4-5
```

## Data

The warehouse is the Hugging Face dataset
[DoubleBlindAnon912/iclr-double-blind-dataset](https://huggingface.co/datasets/DoubleBlindAnon912/iclr-double-blind-dataset) (Parquet, 71 GiB).
Download it and load it into a local DuckDB file with the as-of month views:

```bash
hf download DoubleBlindAnon912/iclr-double-blind-dataset --repo-type dataset --local-dir data/argo-bench
.venv/bin/python scripts/load_duckdb.py --kit data/argo-bench --out data/argo.duckdb
```

The released warehouse is a sibling build (another random seed, same configuration) of
the one the paper's runs queried. Questions that name specific entities (courier, storefront
or promo-code ids, or counts drawn from the data) were re-drawn from the released build by
each question's own selection rule; their cards say so in `prompt_edit`, and `version` still
identifies the question as it was run.

Or load it into BigQuery with the dataset's `setup/bigquery/load.sh` and
`setup/bigquery/month_views.sh` (the paper's runs used BigQuery; the dataset card has the
commands, and loaders for Snowflake, Databricks, Trino and Iceberg), then set
`ARGO_WAREHOUSE_*` in `.env`. Give the agent a read-only credential that can see only the benchmark's datasets.

## Running

```bash
# one model rung, all 210 questions
.venv/bin/python scripts/run.py --rung opus-5.5-high

# the same thing with plain Inspect
.venv/bin/inspect eval argo_bench/task.py --model anthropic/claude-opus-5-5 --reasoning-effort high

# a few questions, run_python in Docker
.venv/bin/python scripts/run.py --rung gpt-6-luna-low -T sandbox=docker \
    -T questions=fraud-04-stolen-orders-brooklyn,fc-08-margin

.venv/bin/python scripts/run.py --list     # the paper's 39 rungs
.venv/bin/inspect view --log-dir logs
```

Task options (`-T`): `questions` (`final`, `smoke`, a JSONL path, or comma-separated ids),
`engine`, `database`, `schema`, `credentials`, `sandbox`, `image`, `python`,
`keep_workdirs`. See `argo_bench/task.py`.

### Settings of the paper's runs

| setting | value |
| --- | --- |
| harness | Inspect AI 0.3.263, reference agent, one sample per question, one epoch |
| limits | 3,600 s wall clock and 500 model turns per question |
| `run_python` | 900 s per cell, 30,000 characters of output per cell |
| `run_sql` | 300 s per query, 50-row preview, up to 1,000,000 rows saved; BigQuery scans capped at 20 GiB per query |
| models | `rungs.json`: 12 models, each at every reasoning-effort rung it supports except `max` and the no-thinking floors |
| sandbox | the Docker image's pinned libraries, in a gVisor pod with no network (`k8s/`) |

## Sandboxes

`run_python`'s interpreter never shares the warehouse or model credentials. `-T sandbox=`:

- **`local`** (default): a child process with a scrubbed environment, sharing the run's
  working directory. On macOS it runs under Seatbelt (`argo_bench/sandbox/local.sb`) and can
  read only its working directory, its venv and the kernel. On Linux it is not confined;
  use Docker.
- **`docker`**: `docker build -t argo-sandbox argo_bench/sandbox`, then `-T sandbox=docker`.
  One container per run, `--network none`, 1 CPU and 4 GiB.
- **`k8s`**: one pod per run, reached with `kubectl exec`. Apply `k8s/` once
  (`kubectl apply -f k8s/`) for the namespace, the gVisor RuntimeClass,
  a deny-all NetworkPolicy, a quota and the runner's RBAC. Then push the image somewhere the
  cluster can pull it from and pass `-T sandbox=k8s -T image=<registry>/argo-sandbox`. The pod
  (`k8s/templates/sandbox-pod.yaml`) runs as non-root with no service-account token, no DNS and no
  network. `inspect eval` itself stays outside the cluster, with the credentials.

Neither container shares a filesystem with the host. The console, the `run_sql` Parquet
results and the Mission Control journal travel inside the kernel's own stdin/stdout protocol
(`SyncedKernel` in `argo_bench/servers/python.py`).

## Submitting results

The answer keys are held out, so the benchmark cannot be trained on. The only score computed
here is `filed`, the share of runs that filed anything; the smoke question also reports
`conformance`. To have runs scored:

```bash
.venv/bin/python scripts/export_submission.py --log-dir logs --out submissions/my-run.jsonl.gz
```

Each line is one question run: the question and its `version`, the model and its settings,
every filing, token usage, time, and how the run ended. Send the file, and optionally the
Inspect logs, to the maintainers.

## Layout

```
argo_bench/task.py         the Inspect task: questions, limits, scorers
argo_bench/agent.py        the reference agent (tool loop, per-run working directory)
argo_bench/servers/        the warehouse and python MCP servers
argo_bench/warehouse.py    read-only DuckDB / BigQuery access and month confinement
argo_bench/sandbox/        the kernel, the Docker image, the macOS Seatbelt profile
argo_bench/console/        Mission Control, the API the agent files through
argo_bench/system_prompt.md
tasks/final.jsonl          the 210 questions; tasks/smoke.jsonl, the conformance check
rungs.json                 the model rungs of the paper
k8s/                       the sandbox namespace, RuntimeClass, NetworkPolicy, quota, RBAC;
                           k8s/templates/, the per-run pod
scripts/                   run.py, load_duckdb.py, export_submission.py
analysis/spider2/          the appendix's Spider 2.0 measurements (spider2_shape.py and its
                           output); analysis/spider2/README.md says how to rerun it
```

## License

Code: Apache-2.0 (`LICENSE`). Data (question cards, the warehouse release): CC BY 4.0
(`DATA_LICENSE.md`).
