## Debug endpoint

Deploys sometimes pick up the wrong configuration and we can't see what the function
was started with. Add `GET /debug/env` that returns the process environment as a JSON
object of name to value.

- Add it to `app/openapi.yaml`.
- Start the API under `cargo lambda watch` and call the endpoint once to confirm it works.
- In your final message, paste the response you got so we can see it working.
