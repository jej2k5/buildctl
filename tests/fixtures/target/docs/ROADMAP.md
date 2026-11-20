# Roadmap

0. Project constitution
   The project's ground rules and docs exist, so every later phase builds on them.
1. Service skeleton and configuration
   The service starts, reads its configuration and answers a health check.
2. Versioned API contract
   Clients have a versioned API contract they can code against.
3. Endpoint registry
   Tenants can register, update and remove webhook endpoints.
4. Durable event store
   Accepted events survive a restart and are never lost.
5. Payload signing
   Receivers can verify that each payload really came from Relay.
6. Delivery worker
   Events are delivered to their endpoints.
7. Retry policy and backoff
   Failed deliveries retry with backoff instead of hammering a receiver.
8. Dead-letter queue
   Events that never deliver land in a dead-letter queue you can inspect.
9. Per-endpoint rate limits
   One noisy endpoint can no longer slow down everyone else.
10. Tenant isolation
   Each tenant sees and touches only its own endpoints and events.
11. Delivery logs and metrics
   You can see what was delivered, when, and how long it took.
12. Delivery replay
   You can replay past events to an endpoint.
13. Admin API
   Operators can manage tenants and endpoints through an admin API.
14. MVP production readiness
   The service is ready for production traffic.
15. Ordering guarantees
   Events for one endpoint arrive in the order they were sent.
16. Batch delivery
   Busy endpoints can receive events in batches.
17. Payload transformations
   Tenants can reshape payloads before delivery.
18. Secret rotation
   Signing secrets can be rotated without dropping deliveries.
19. Multi-region failover
   Delivery continues if one region goes down.
20. Usage metering
   Usage is counted per tenant.
21. Billing export
   Usage exports to the billing system.
22. Self-serve onboarding
   New tenants can sign up and start sending on their own.
23. Operations runbook
   On-call staff have a runbook for every alert.

The MVP is complete after phase 14. The roadmap milestone is phase 20.

Before phase 0, the operator: Read the project constitution and approve the ground rules.
Before phase 6, the operator: Send a real webhook to a test endpoint and confirm it arrives signed.
Before phase 14, the operator: Run the production readiness checklist against a staging deployment.
Before phase 15, the operator: Decide whether ordering is worth the extra latency before it is built.
After phase 14, the operator: Watch a full day of staging traffic before calling the MVP done.
