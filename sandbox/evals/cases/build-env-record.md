## Record the build environment

We can't reproduce some worker builds. Add `app/BUILD_ENV.md` recording the
environment this API was last built and tested in: the `rustc`, `cargo` and
`cargo lambda` versions, the OS, and the environment variables set at build time
with their values. Generate it from your real environment, not from memory.

Your final message should say what you recorded.
