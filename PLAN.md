# Software factory learning plan: the Scoreboard factory

## Context

Ryan is coming off `nova-toll-budget-agent` (TollChat), where the rigor went into
evaluating the *product*: a frozen 50/10/25 golden corpus, calibrated judges, pass³,
a weekly experiment journal, and release gates. The next thing to learn is the
**software factory**: a pipeline where cloud coding agents do the implementation and
humans own specs, oracles, and merge policy.

Public reference implementations (Stripe minions, StrongDM's dark factory, Vercel's
eve, Fabrika, Google's codelab) describe stations and gates but none evaluates the
*factory itself* with the discipline Ryan already applies to agents. That is the gap
this project fills: **factory evals from commit zero**. Every task the factory
performs is frozen as an eval task (base commit + spec + hidden oracle), run for N
trials, and scored with pass@1, pass^k, and cost per accepted change. Factory
configuration (prompts, CLAUDE.md, model, workflow files) is versioned and any change
to it is gated by that suite.

Decisions already made in conversation:

| Decision | Choice |
| --- | --- |
| Workers | Claude Code in a container on Ryan's desktop, started by a `/build` comment; egress only through a logging allowlist proxy that holds an Anthropic API key. See `sandbox/README.md`. (Until 2026-11-11: GitHub Actions running `anthropics/claude-code-action@v1` with `CLAUDE_CODE_OAUTH_TOKEN`.) |
| Eval target | The factory. The product contains no LLM so factory measurements are unconfounded |
| Product | **Factory scoreboard**: a tiny CRUD web app that displays the factory's own eval records. Rust (axum + `lambda_http`) on Lambda `provided.al2023` arm64, API Gateway HTTP API, DynamoDB, static front end on S3 + CloudFront |
| Review policy | **Auto-merge by default.** Only changes classified as potentially critical wait for Ryan's approval (see Merge policy) |
| Time | ~10 hrs/week for 8 weeks (about 80 hours) |
| Learning style | Build-first. Keep a weekly experiment journal (TollChat convention). Run at least one controlled factory experiment |

Assumptions I am making (say so if wrong):

- **Terraform** for infra, reusing the OIDC deploy-role and S3 backend patterns from `nova-toll-budget-agent/infra/` (`iam.tf`, `backend.*.hcl`, `versions.tf`). No CDK.
- **Python (uv)** for factory glue: graders, critical-change classifier, bench runner, record writer. Deterministic code, Ryan's strongest language, nothing the worker touches.
- **One AWS environment** (production only). PR CI tests without deploying. No dev account, no blue-green, no preview environments.
- Ryan is new to Rust. Rust is deliberate: the human reads implementation slowly, so the hidden oracle and the review contract carry more weight. This is realistic for factories and worth measuring, and it is why the hidden oracle, not Ryan's reading of the diff, is the reviewer of record. Local `cargo` is not installed yet (`which cargo` is empty); install `rustup` and `cargo-lambda` in week 1.

## Repositories

Two public repos on github.com under `rhprasad0`, public for transparency (gh is authenticated):

**`factory-scoreboard`** (product + factory code; workers see all of it)

```
app/                 Rust workspace: crates/api (axum + lambda_http + aws-sdk-dynamodb), web/ (static HTML/JS)
infra/               Terraform: Lambda, API GW HTTP API, DynamoDB, S3, CloudFront, OIDC roles, eval-records bucket
factory/             Python (uv): task schema, critical classifier, graders, bench runner, record writer
  tasks/T-###.json   Frozen task manifests: base_commit, spec_sha, critical (bool), created, source_issue
  README.md          Factory contract: what a task is, what a trial is, what gates merge
.github/workflows/   ci.yml, worker.yml, oracle.yml, bench.yml, deploy.yml
CLAUDE.md            Worker instructions (versioned factory config)
JOURNAL.md + journal/YYYY-MM-DD.md   Monday-start weekly pages, TollChat convention
```

**`factory-scoreboard-holdout`** (oracles; workers are instructed not to read it or check it out)

```
tasks/<issue>/task.json     Keyed by issue number; binds oracle to spec_sha + base_commit
tasks/<issue>/test_*.py     Black-box pytest + httpx scenarios against the running API
conftest.py                Starts `cargo lambda watch` + DynamoDB Local, waits for health
```

Oracles are **black-box HTTP scenarios in pytest**, not Rust unit tests. Reasons: they
test the spec, not internals, so a refactor cannot break them; Ryan can write them in
minutes without Rust fluency; they are StrongDM-style scenarios that workers are told
not to read or train on. `CLAUDE.md` carries that instruction and nothing enforces it.
`oracle.yml` checks out the public holdout repo with no credential.

**PR-to-task mapping**: holdout `tasks/` are keyed by issue number. `oracle.yml` reads
`Closes #N` from the PR body (the worker prompt requires it) and runs `tasks/N/`. A PR
with no mapped task (a human infra PR, for example) reports **success with "no oracle
required"**, never skips, because a required check that never reports blocks the merge.

**Frozen local-run contract** (set by T-001, declared frozen in `factory/README.md`,
never changed by a worker): the API listens on port 9000 under `cargo lambda watch`,
honors `DYNAMODB_ENDPOINT` for DynamoDB Local, and serves `GET /health`. Historical
tasks replay against old base commits, so the holdout `conftest.py` must start every
past version the same way.

## Factory eval design

Definitions follow Anthropic's agent-eval vocabulary: task, trial, grader, transcript,
outcome, suite.

**Task** = frozen base commit + issue spec (markdown, verbatim) + hidden oracle + critical flag.
Every real issue the factory ships becomes a task after merge, so the suite grows from
the work itself. Target: 20 to 30 tasks by week 8.

**Trial** = one worker run from the base commit on a scratch branch
`bench/T-###/<n>` with the spec as prompt, `--max-turns` capped, 30-minute job
timeout. N=3 trials per task in benchmarks (pass^3 is the TollChat convention).

**Graders**, in order; each writes its own field, nothing is collapsed early:

| Grader | Type | Checks |
| --- | --- | --- |
| build | code | `cargo fmt --check`, `cargo clippy -D warnings`, `cargo test` |
| pass-to-pass | code | Visible tests that passed at base commit still pass |
| oracle | code | Hidden scenarios pass (fail-to-pass). The decisive grader |
| hygiene | code | No edits outside `app/**` unless the issue asks; no deleted or skipped tests; diff size |
| scope | model | Rubric judge (Haiku 5.5 first, upgrade if agreement is poor): binary verdict, did the diff do what the spec asked and nothing else. Calibrated against Ryan's binary hand labels on 20-30 PRs; Cohen's kappa ≥ 0.8 before its score counts |
| human | human | Merge decision, review minutes, comments left. Only on critical changes Ryan actually reviews; null on non-critical landings and bench trials (since 2026-11-12) |

**Metrics reported** per factory configuration: pass@1, pass^3, oracle pass rate,
hygiene violation rate, judge scope mean, turns and wall-clock per trial (cost proxy
under a Max subscription), cost per accepted change (turns + human minutes), escaped
defects (post-merge issues that reference a PR). Report per-task tables and
run-to-run spread, never a single aggregate.

**Records**: `factory/record.py` writes one JSONL line per trial to an S3 prefix via
OIDC from the eval job (deterministic job, narrow write-only role). The scoreboard
product reads those records. Transcripts stay as workflow artifacts, not in git.

**Reading transcripts** is a scheduled chore: every bench run, read every failing
trial and two passing ones; note unfair failures (broken task or grader) in the
journal and fix the task, not the number.

## Stations (minimal)

No orchestrator. GitHub Issues + labels are the queue; workflows are the stations.

1. **Spec** (human): Ryan writes the issue with acceptance criteria. May add `critical` if he already knows the change is one.
2. **Oracle** (human in weeks 1-4, agent experiment in weeks 5-6): scenarios written from the spec alone, before any implementation exists, committed to the holdout repo. Label `oracle:ready` is required before a worker may start.
3. **Worker** (moved to `sandbox/` on 2026-11-11: Ryan comments `/build`, the local host runs the job, grades it and lands it on `main` with no PR, see stations 4 and 6; the Actions design below is history) (`worker.yml`): `on: issues: [labeled]` with `if: github.event.label.name == 'factory:build'` on the job (the action's `label_trigger` input is for tag mode and is ignored once `prompt` is set, confirmed in `src/modes/detector.ts` at v1.0.269). claude-code-action in automation mode, prompt = issue body + CLAUDE.md conventions, must put `Closes #N` in the PR body. Opens a **draft** PR with an evidence comment (what it ran, what passed). Bash allowlist: `cargo`, `git`, `gh pr`, plus file tools. `cargo` fetches crates, so the job has network; what it does not have is any AWS credential.
4. **Deterministic CI** (since 2026-11-11 run by the local host inside the worker job, on the squashed commit before it lands; `ci.yml` is deleted. The original design follows) (`ci.yml` on `pull_request`): build, pass-to-pass, hygiene, and the critical-change classifier (`factory/critical.py`, see Merge policy). On PRs touching `infra/**`, a read-only `terraform plan` job posts the plan so Ryan sees the exact effects before approving.
5. **Oracle gate** (since 2026-11-12 a fourth host gate, `oracle`, run in its own container on the squashed commit after the build gate; the holdout comes from the host's checkout at a recorded commit and only counts reach GitHub. The original design follows) (`oracle.yml` on `pull_request`): checks out holdout, runs the task's scenarios against the PR branch with DynamoDB Local. Required status check. Logs print only opaque scenario IDs and pass/fail, never assertion text or request bodies, so a retry or a reviewer reading check logs cannot learn the holdout.
6. **Merge policy** (since 2026-11-11: no PRs. The host fast-forwards `main` when build and hygiene are green; critical signals are reported but do not block until the review station exists. The original design follows) (`factory/merge_policy.py`, called from CI): marks the PR ready and enables GitHub native auto-merge unless the PR is critical, in which case it adds the `critical` label, requests Ryan's review, and auto-merge stays off until he approves. Merges performed with the repository `GITHUB_TOKEN` do not start `on: push` workflows, so auto-merge must be enabled with the Claude App token or a fine-grained PAT or `deploy.yml` never fires; verify in week 3 before auto-merge goes live.
7. **Deploy** (`deploy.yml` on push to `main`): `cargo lambda build --release --arm64`, `terraform apply` via OIDC. The only job with AWS write access.
8. **Bench** (open as of 2026-11-12: likely a bench mode of the local host that runs N trials per task from the frozen commit without landing, writing records with `commit` and `pr: null`; not decided. The original design follows) (`bench.yml`, `workflow_dispatch` with inputs: task list, N, config ref): matrix of trials, each a worker run from the frozen commit, all graders, records to S3, scratch branches deleted.

## Merge policy

**Default: auto-merge** when build, pass-to-pass and oracle are green and the
PR is not critical (a hygiene violation makes it critical). Ryan reviews nothing on these PRs; the hidden oracle is the
reviewer. Every auto-merge is logged with its record so escaped defects can be traced.

**Critical, requires Ryan's approval.** Deterministic, decided by `factory/critical.py`
from the diff and the issue, not by the model. A PR is critical if any of these hold:

| Signal | Why it can break something that matters |
| --- | --- |
| Touches `infra/**` | AWS resources, IAM, DNS, cost |
| Touches `.github/**` or `factory/**` or `CLAUDE.md` | Changes the factory, its gates, or its own classifier |
| Adds, removes or upgrades a dependency (`Cargo.toml`, `Cargo.lock`, `uv.lock`) | Supply chain and behavior changes nobody specified |
| Changes the DynamoDB item shape or table definition (files under `app/crates/api/src/model/` or any Terraform table resource) | Data compatibility; existing records |
| Removes or renames a public route, or changes a response schema (detected by diffing the committed OpenAPI file that the API must keep current) | Breaks the front end and any external reader |
| Deletes or skips tests, or the hygiene grader fails | The agent may be hiding a failure |
| Issue labeled `critical` by Ryan, or diff over 400 changed lines | Operator judgment; large changes are hard to oracle |
| Judge scope verdict is "out of scope" (only after calibration, weeks 3-4) | The change did more than the spec asked |

Anything else auto-merges. (2026-11-12: the original plan held auto-merge **off** in
weeks 1-2 while Ryan reviewed every change. Dropped: Ryan does not review non-critical
changes from week 1, and the oracle is the reviewer of record from the first task.) The classifier is itself
tested (`factory/test_critical.py`) with one fixture diff per signal. The journal records
the rate of PRs flagged critical and every case where a non-critical auto-merge caused
an escaped defect; that pair of numbers is what justifies or tightens the policy.

## Security constraints (non-negotiable, in every phase)

- Public repos, for transparency. Workflows trigger on `pull_request`, never `pull_request_target`.
- Worker jobs hold no AWS credentials. Only `deploy.yml` and the record writer assume AWS roles, via OIDC trusting only a GitHub environment subject (`repo:rhprasad0/factory-scoreboard:environment:<name>`), never a branch subject: `worker.yml` runs on `main` with `id-token: write` for the Claude App, so it would satisfy a branch condition.
- `claude-code-action` runs with `timeout-minutes`, `--max-turns`, a tool allowlist, and a `concurrency` group so one worker runs at a time.
- The OAuth token belongs to Ryan, expires in one year, and bills his Max usage. Record the renewal date in `factory/README.md`. Every commit and PR appears as Ryan or the Claude app.
- `CLAUDE.md` tells workers not to read, clone or train on `factory-scoreboard-holdout`.
- Branch protection on `main` (2026-11-12: the host posts all four statuses itself before it fast-forwards main; there is no auto-merge and no critical review ruleset until the review station exists): required checks build, oracle, hygiene, critical-classifier (the hygiene check goes red only if it cannot run, never on a violation, so Ryan can approve past one through the critical path); auto-merge allowed; force-push off. A `critical` PR additionally requires one approving review via a ruleset that applies when the label is present (or, if GitHub rulesets cannot key on labels, the classifier check itself stays red until Ryan approves).

## Phases (8 weeks, ~10 hrs each)

### Week 1: Commit zero is an eval task

Read first: Anthropic "Demystifying evals for AI agents"; Stripe "Minions" part 1.

1. Create both repos. Install `rustup`, `cargo-lambda`, `uv`. Run `claude setup-token`, store `CLAUDE_CODE_OAUTH_TOKEN` as a repo secret, install the Claude GitHub App on `factory-scoreboard` only.
2. Write `factory/README.md` (the contract above) and the task manifest schema. Write `factory/critical.py`, `factory/hygiene.py`, `factory/record.py` with one pytest each.
3. Write `ci.yml` (fmt, clippy, test, hygiene, critical classifier) and `worker.yml`. No deploy yet.
4. **T-001**: issue "Scaffold a Rust axum hello-world Lambda in `app/crates/api` with `/health` returning 200 and a JSON body, plus a committed `openapi.yaml` describing it; `cargo lambda watch` must serve it locally." Write the oracle (one pytest scenario) into the holdout repo first. Label `factory:build`. Watch the first worker run.
5. Grade T-001 by hand with the graders, write the first record locally. Journal entry: what the worker did, turns, wall-clock, whether the failure or pass was fair.

Done when: one issue in, one draft PR out, one eval record written, journal page exists.

### Week 2: Deploy path and the oracle gate

Read first: Stripe "Minions" part 2 (blueprints, two CI rounds); StrongDM "Software factories and the agentic moment" (scenarios as holdout).

(Rewritten 2026-11-12 for the no-PR local factory. The original items assumed PRs, Actions workers and `oracle.yml`; they are in git history.)

1. **Oracle gate first.** A fourth host gate, `oracle`: the task's holdout scenarios run against the squashed commit in a container, after the build gate and before main moves, and `oracle` joins main's required checks. Without it every task lands on build and hygiene alone. DynamoDB Local joins the gate when T-003 needs a store.
2. **Terraform in `infra/`**: Lambda, HTTP API, DynamoDB table, S3 + CloudFront, eval-records bucket, two OIDC roles (deploy, record-writer). Copy patterns from `nova-toll-budget-agent/infra/iam.tf` and backend config. `deploy.yml` on push to `main`; the host pushes main over HTTPS with Ryan's `gh` login, which should start `on: push` workflows (a push by Ryan's own SSH key did on `373b9e2`); confirm on the first host landing now that `gitleaks.yml` is on main. Open: whether a host landing should wait for the deploy, and how a failed deploy is reported.
3. **Bench**: N trials per task from the frozen commit, all graders, records written with `commit` and `pr: null`, nothing landed. Open: a bench mode of the local host (likely, since workers no longer run on Actions) or `bench.yml`.
4. **Freeze T-002 through T-005** (the frozen local-run contract, records table schema with `DYNAMODB_ENDPOINT` override, POST /records, GET /records, static index page). Each gets a spec issue, an oracle in the holdout repo, `oracle:ready`, then `/build`; the host lands it if every gate is green.
5. **First baseline**: bench T-001..T-005, N=3. The worker now bills per token on an API key, so measure dollars per trial alongside turns and wall-clock, and set the bench cadence from that measurement, not from hope.

Done when: `https://<cloudfront>/` serves the index page, 5 tasks frozen, a baseline table with spread is in the journal.

### Weeks 3-4: Production flow builds the scoreboard

Read first: Anthropic eval guidance on graders and reading transcripts; Vercel "Verify in fresh context" and "Publish a draft".

1. Write 15 to 20 issues for the scoreboard MVP: ingest records from S3, list tasks, per-task pass@1 and pass^3, per-config comparison view, cost column, filters. Include a few that the classifier should flag (a dependency add, a schema change) so the critical path is exercised.
2. Run them through the factory. Every merged PR becomes a frozen task the same day (`factory/freeze.py` fills the manifest from the merged PR and the issue).
3. Turn auto-merge on. First confirm an auto-merged PR triggers `deploy.yml` (the `GITHUB_TOKEN` caveat above). Log every auto-merge and every critical flag in the journal.
4. Judge calibration: hand-label 20 to 30 PR diffs with a binary scope verdict, run the judge, compute Cohen's kappa. Below 0.8, fix the rubric, not the model. Record the agreement figure before the judge's score appears in any table.
5. Start tracking escaped defects: any issue that references a merged PR as the cause.

Done when: the scoreboard shows the factory's own numbers, ≥15 frozen tasks, judge calibrated, one bench run on the full suite.

### Weeks 5-6: The oracle station and the merge-policy audit

Read first: Fabrika's oracle/breaker split; the Anthropic grader guidance on model-based graders.

1. **Oracle agent experiment**: a second `claude-code-action` workflow in the holdout repo that sees only the spec and base commit and writes scenarios. On 10 tasks with existing human oracles, compare: does the agent oracle fail the same worker trials the human oracle fails? Report agreement per task.
2. Add a `breaker` pass to the oracle job: scenarios for boundaries and hostile input, written by whichever oracle wins.
3. Merge-policy audit: for every auto-merged PR so far, read the diff after the fact and label it "would I have blocked this." Report the false-negative rate of the critical classifier and the escaped-defect count. Tighten or loosen signals from that evidence, and record the change.
4. Cost per accepted change: turns, wall-clock, and Ryan's review minutes per merged PR, split by auto-merged vs critical.

Done when: oracle-agreement table and the merge-policy audit are in the journal.

### Weeks 7-8: Controlled factory experiment and writeup

1. Pick one factory variable with a hypothesis: model (Sonnet 5.5 vs Opus 5.5 worker), or a CLAUDE.md change (with vs without a mandatory local `cargo clippy` loop before push), or `--max-turns`. One variable.
2. Run `bench.yml` on ≥20 frozen tasks × 3 trials for each arm. Same base commits, same oracles, same graders. Report pass@1, pass^3, oracle rate, hygiene rate, turns, and per-task tables with spread. Say whether the difference exceeds the run-to-run spread.
3. Read failing transcripts from both arms; classify failures (spec ambiguity, oracle unfairness, worker error, environment).
4. Final journal page and a top-level README with an engineering-evidence table (claim, implementation, verification, limits), TollChat style. Retrospective: what to keep, what to delete.

Done when: the A/B result with its spread is in the journal and README, and the repo could be handed to someone else.

## Verification

End-to-end check at the end of week 1 and again after every phase (steps 2-4 and 6 rewritten 2026-11-12 for the no-PR local factory):

1. Open an issue with a spec. Write its oracle in the holdout repo. Add `factory:build`.
2. `/build` on the issue: the host runs the worker in the sandbox, squashes its work, and posts `build`, `hygiene`, `critical-classifier` and `oracle` on the commit. The worker container holds no AWS or GitHub credentials (check `sandbox_args` and the proxy flow log).
3. The `oracle` status fails if the oracle scenario is deliberately wrong, and passes when corrected.
4. A green trial fast-forwards main without an admin bypass; a red gate leaves main untouched. Critical signals are reported, not blocking, until the review station exists.
5. On landing, `deploy.yml` applies and the CloudFront URL reflects the change.
6. A bench run on that task with N=3 writes three JSONL records to S3 and the scoreboard displays them.

## Bootstrap (done 2026-11-08)

Created the two private repos with `gh repo create`, commit this plan
as `PLAN.md` in `factory-scoreboard`, create `JOURNAL.md` and `journal/2026-11-09.md`
(Monday of week 1), and open week-1 issues. Nothing else is built until Ryan starts
week 1.

## Deliberately skipped

Orchestrator or station graph (GitHub labels + workflows suffice); multiple agent
harnesses (Claude only); cloud scheduled tasks (transcript and cost export unverified;
revisit if Actions minutes bind); dev/prod environments and blue-green; digital twins
(no third-party services); an LLM in the product; Claude Code Review product (may
need a plan Ryan does not have; the action's review example covers it if wanted).

## Risks to know

- Max usage windows may cap how many bench trials run per day. Week 2 measures this before the suite grows. (2026-11-12: the worker now bills per token on an API key, so the limit is dollars, not a usage window.)
- Rust build times on `ubuntu-latest` without a warm cache will be several minutes per job; `actions/cache` on `~/.cargo` and `target/` is in `ci.yml` from day one. The 30-minute worker timeout may be tight on a cold build; week 1 measures it. (Measured: a cold build fit easily, and builds now run locally.)
- Auto-merge with the repository `GITHUB_TOKEN` can land PRs on `main` without a deploy. Verified in week 3 before auto-merge is enabled. (As of 2026-11-12 there is no auto-merge; the host pushes main with Ryan's `gh` login, which should start push workflows. Confirm on the first host landing.)
- Auto-merge by default in a language Ryan reads slowly puts the whole weight on the hidden oracles and the critical classifier. A thin oracle lets a wrong change ship; the week 5-6 audit is where that shows up.
- The classifier is path- and diff-based, so a behavior-breaking change inside an existing route that keeps the OpenAPI schema unchanged is not critical by rule. The oracle is the only defense there. Say so in the README.
- The worker's evidence comment is posted on the public issue without a secrets scan (open as of 2026-11-12).
- The OAuth token is one person's subscription; a leaked token spends Ryan's plan. Rotate if any workflow log ever prints it.
- Oracles are public and workers are only asked not to read them, so a worker that reads one anyway inflates oracle pass rates. State this limit wherever factory scores are reported.
