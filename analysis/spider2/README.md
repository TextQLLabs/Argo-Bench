# How enterprise-shaped is Spider 2.0?

The numbers in the paper's appendix "How enterprise-shaped is Spider 2.0?" come from
`spider2_shape.py`, run on the public Spider 2.0 repository. `spider2_shape.json` is its
output at the pinned commit.

```sh
git clone --filter=blob:none https://github.com/xlang-ai/Spider2
git -C Spider2 checkout cafb867313aab4e674652054198f383cf4018943
pip install sqlglot
python spider2_shape.py --spider2 Spider2 --out spider2_shape.json
```

It takes about two seconds and reads only files Spider 2.0 releases:

| Measurement | Input | JSON key |
|---|---|---|
| Where each question's database comes from | `spider2-snow/spider2-snow.jsonl`, the 632-task manifest at `9615ebf`, and the hand attributions in the script | `provenance`, `synthetic`, `release_632` |
| Tables that copy another table's schema | `spider2-snow/resource/databases/*/*/DDL.csv` | `structure` |
| Logical tables each gold answer reads | gold SQL of both suites, `methods/gold-tables/spider2-snow-gold-tables.jsonl` | `tables_read` |

`tables_read.per_task` lists each question's database, its logical-table count from
each source, and the raw gold-table list, so any single count can be checked by hand.
