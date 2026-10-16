set -a
source .env
set +a

curl -i \
  -X POST "$OPENAI_BASE_URL/responses" \
  -H "Authorization: Bearer $OPENAI_API_KEY" \
  -H "Content-Type: application/json" \
  -H "User-Agent: codex/0.1" \
  --data '{
    "model": "'"$OPENAI_MODEL"'",
    "input": "Reply with a JSON object containing {\"ok\": true}.",
    "text": {
      "format": {
        "type": "json_schema",
        "name": "health_check",
        "schema": {
          "type": "object",
          "properties": {
            "ok": {"type": "boolean"}
          },
          "required": ["ok"],
          "additionalProperties": false
        },
        "strict": true
      }
    }
  }'
