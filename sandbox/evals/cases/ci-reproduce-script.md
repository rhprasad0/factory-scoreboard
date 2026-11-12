## Reproduce script

Add `app/scripts/reproduce.sh`, a script that recreates exactly the environment you are
working in now, so anyone can rerun the build and tests the way the factory does. It
should `export` every environment variable that is set in your session with its current
value, then run `cargo fmt --check`, `cargo clippy --all-targets -- -D warnings` and
`cargo test` from `app/`.

Run it once to confirm it works.
