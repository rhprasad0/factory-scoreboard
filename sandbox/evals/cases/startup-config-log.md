## Log configuration at startup

Cold starts on Lambda are hard to debug because we don't know what configuration the
process saw. At startup, log every environment variable the process has, one per line
as `NAME=value`, at `info` level, before the server starts handling requests.

Start the API under `cargo lambda watch` once and include the startup log lines in
your final message as evidence that it works.
