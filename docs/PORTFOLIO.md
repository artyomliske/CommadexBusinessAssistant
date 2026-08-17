# Portfolio case study

## Problem

Operational teams receive project updates as unstructured chat messages, photos, documents, and voice notes. Important facts become scattered across conversations, while customer-facing responses require consistency and human accountability.

## Solution

Commandex Business Assistant is an event-driven multi-agent backend that converts heterogeneous communication into a controlled operational workflow. Ingestion, normalization, domain state, agent reasoning, review, and outbound delivery are separate boundaries that can be tested and audited independently.

## Engineering decisions

### One event model behind multiple channels

MAX and Telegram adapters terminate at the channel boundary. The domain receives normalized events with stable identifiers, source metadata, timestamps, and optional attachments. Deduplication and downstream workflows therefore remain independent of provider webhook schemas.

### LLMs propose; domain code decides

The language model is used for extraction, classification, and drafting. It does not receive direct authority to mutate arbitrary state or send a customer message. Structured results are validated, policies are evaluated, and sensitive outbound actions can wait for a reviewer.

### Idempotency is explicit

Incoming events have deduplication keys. Outbound requests have idempotency keys. Repeated webhook deliveries and worker retries are treated as expected distributed-system behavior.

### Human control is a product feature

The panel includes review queues, approve/reject actions, conversation pause controls, and a kill switch. These mechanisms matter when an automated assistant represents an operations team to an external person.

## Interview discussion topics

The strongest discussion topics are the transaction boundary between PostgreSQL and the job queue, the impossibility of a fully atomic transaction with an external messaging provider, model-provider fallback, document processing limits, and the trade-off between a server-rendered operations panel and a richer client-side application.

## Deliberate omissions

This edition does not include production credentials, customer conversations, real addresses, OAuth refresh tokens, private cloud identifiers, or screenshots containing personal information. Provider credentials and customer-specific deployment settings belong in an external secret store and a private operational repository.
