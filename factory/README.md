# Factory contract

Cloud coding agents do the implementation here. Ryan owns specs, oracles and merge
policy. Every task the factory performs is frozen and re-runnable, so the factory itself
can be measured. [PLAN.md](../PLAN.md) has the full design.

## Vocabulary

- **Task**: a frozen base commit, the issue spec, an oracle and a critical flag. Manifests
  are `tasks/T-###.json` and follow `tasks/schema.json`. `spec_sha` is the SHA-256 of the
  issue body, verbatim, as UTF-8.
- **Trial**: one worker run from the base commit with the spec as its prompt. Benchmarks
  run three per task. Pass^3 means all three passed, and a single trial has no pass^3.
- **Grader**: one check on a trial. Build, pass-to-pass, oracle and hygiene are code, scope
  is a model, human is Ryan. Each writes its own field and nothing is collapsed early.
- **Record**: one JSONL line per trial, following `record.schema.json`, written by
  `record.py`. A null grader did not run.

## Frozen local-run contract

Historical tasks replay against old base commits, so every version must start the same
way. Set by T-001. Workers never change it.

- The API listens on port 9000 under `cargo lambda watch`.
- `GET /health` returns 200.
- The API honors `DYNAMODB_ENDPOINT` for DynamoDB Local once a store exists.

## Mapping a PR to its oracle

Holdout oracles are keyed by issue number, in `tasks/<issue>/`. `oracle.yml` reads
`Closes #N` from the PR body. A PR with no mapped task reports success with "no oracle
required". It never skips, because a required check that never reports blocks the merge.

## Gates run from the base branch

A worker PR can edit `factory/`, so CI must run `critical.py` and `hygiene.py` from a
checkout of `main`, against the PR's diff. Running the PR's own copy would let a change
rewrite the check that grades it.

## Merge policy

Weeks 1-2 have auto-merge off. From week 3 a PR auto-merges when build, pass-to-pass and
oracle are green and `critical.py` reports no signals. Otherwise it waits for Ryan's
approval.

`hygiene.py` reports a violation without failing its check, so Ryan can approve past one.
The violation fires `hygiene_failed`, which holds the PR. It exits non-zero only if it
could not run.

| Signal | Fires when |
| --- | --- |
| `infra` | The diff touches `infra/**`, which includes Terraform table definitions |
| `factory_config` | The diff touches `.github/**`, `factory/**` or `CLAUDE.md` |
| `dependency` | The diff touches any `Cargo.toml`, `Cargo.lock` or `uv.lock` |
| `data_model` | The diff touches `app/crates/api/src/model/**` |
| `api_contract` | `app/openapi.yaml` loses a route or changes an existing response or component |
| `hygiene_failed` | `hygiene.py` reports an edit outside `app/**`, or a deleted or skipped test |
| `labeled_critical` | Ryan labeled the issue `critical` |
| `oversized` | The diff is over 400 changed lines |

The judge scope signal joins this table only after calibration, in weeks 3-4.

`critical.py` exits non-zero if it cannot classify a diff, such as a path it cannot parse
or malformed OpenAPI. A diff that cannot be read never counts as safe.

## Running it

```bash
cd factory
uv run pytest
uv run python critical.py --base origin/main --head HEAD --label critical
uv run python hygiene.py --base origin/main --head HEAD
uv run python record.py --out ../records/local.jsonl < trial.json
```

`records/` is ignored by git. Raw run data lives in workflow artifacts and S3.

## Credentials

`CLAUDE_CODE_OAUTH_TOKEN` is Ryan's Max subscription token, stored as a repository
secret. **It expires on 2027-11-09**, so renew it before then. Both repos are public and
Actions logs are world-readable, so rotate it at once if any log ever prints it.

## Known limits

- The classifier reads paths and the OpenAPI file. A behavior-breaking change inside an
  existing route with an unchanged schema is not critical by rule, and the oracle is the
  only defense there.
- The classifier trusts `app/openapi.yaml` to be current. A route changed in code but
  not in that file does not fire `api_contract`.
- Hygiene matches Rust test attributes as text. It misses a test gutted by an early
  return, wrapped in a block comment, or hidden behind `#[cfg(not(test))]`, which is
  deliberately allowed because it is ordinary production Rust.
- Both repos are public for transparency. Oracles are visible, and `CLAUDE.md` asks
  workers not to read them. Nothing enforces that, so a worker that reads one anyway
  inflates oracle pass rates. State this limit wherever factory scores are reported.
