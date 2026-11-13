# Factory experiment journal

Experiments, figures, and decisions are organized by week. Each page records what was
tested, the result, its limits, and what was retained or rejected. Weeks run Monday
through Sunday; pages are named by the Monday (`journal/YYYY-MM-DD.md`).

| Week | Work | Recorded result |
| --- | --- | --- |
| [November 9-15](journal/2026-11-09.md) | Week 1: commit zero is an eval task | T-001 trials 1 and 7: build, oracle and hygiene passed; awaiting human review. Worker moved to a local sandbox (new baseline). No PRs: trial t7 landed T-001 on main through local gates. The oracle became a fourth landing gate. Secret-leak baseline: 0 of 40 canary leaks on benign specs. Infra moved out of the factory |

## Maintaining the journal

This index and the weekly pages are permanent experiment history. Append dated
corrections rather than editing earlier results. Keep transcripts and raw run data out
of git; they live as workflow artifacts and S3 records. Pass^3 means a task passed all
three trials; single-trial results have no pass^3.
