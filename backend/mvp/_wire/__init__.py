"""Wire adapters: translate client-facing request/response shapes to and from
the normalized Converse form. Each adapter owns SSE/JSON shape ONLY — never
DynamoDB, boto3, or budget state."""
