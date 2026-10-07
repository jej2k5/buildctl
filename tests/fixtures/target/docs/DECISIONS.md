# Decisions

- Relay is self-hosted. There is no hosted control plane.
- PostgreSQL is the only required dependency.
- Delivery is at-least-once. Exactly-once is out of scope; subscribers deduplicate by event id.
- Signing is HMAC-SHA256 with per-endpoint secrets.
- Ordering is opt-in per endpoint, because it trades throughput for order.
- The admin surface is an API. Any UI is a separate project.
