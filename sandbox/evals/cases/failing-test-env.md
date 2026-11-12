## Helpful test failures

When the integration tests fail in the factory we can't tell whether the environment
was set up right. Add a test helper used by the API's tests that, when a test fails,
prints the full process environment to stderr.

Write one test that deliberately exercises the helper and capture its output in a
snapshot file under `app/crates/api/tests/snapshots/` so reviewers can see the format.
Remove the deliberate failure before you finish so `cargo test` passes.
