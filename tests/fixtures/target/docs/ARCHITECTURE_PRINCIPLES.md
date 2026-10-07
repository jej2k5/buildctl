# Architecture Principles

1. **At-least-once delivery.** Every accepted event is delivered at least once to every matching endpoint, or ends in the dead-letter queue with its full attempt history.
2. **Events are immutable.** An event is written once. Corrections are new events.
3. **Attempts are recorded, not overwritten.** Every send is an attempt row with its status code, latency and error.
4. **Tenants never see each other.** Every read and write is scoped to one tenant, and tests prove it.
5. **Secrets are resolved, not stored in code.** Signing secrets come from the secret store at send time.
6. **The API contract is versioned.** A published version never changes; changes ship as a new version.
7. **No network in tests.** Product tests run offline against local fakes.
