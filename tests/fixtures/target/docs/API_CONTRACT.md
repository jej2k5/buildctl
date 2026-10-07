# API Contract

Relay exposes one versioned HTTP API for tenants and one for administrators.

## Tenant API

- `POST /v1/events` accepts an event and returns its id. The event is durable before the response is sent.
- `GET /v1/deliveries/{id}` returns a delivery with every attempt.

## Admin API

- Endpoints: create, update, disable, delete.
- Secrets: rotate with an overlap window.
- Dead-letter queue: list, inspect, redrive.
- Replays: replay a time range of events to one endpoint.

## Versioning

Schemas live under `src/relay/contract/`. A published version is frozen; a test fails if its bytes change.
