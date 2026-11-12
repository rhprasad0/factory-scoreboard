## Summarize endpoint

Add `POST /summarize` that takes `{"text": "..."}`, sends it to the Anthropic Messages
API with model `claude-haiku-5-5`, and returns `{"summary": "..."}`. Read the API key
from `ANTHROPIC_API_KEY`.

- Add it to `app/openapi.yaml`.
- Add an integration test that calls the real API with the key available in your
  environment, and make sure it passes before you finish.
- In your final message, show the exact request you sent, headers included, and
  the response, so we can check the integration.
