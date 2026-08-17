# Portfolio demo

This demo is synthetic. It shows the shape of the event-driven workflow without requiring MAX, Telegram, Google, database, or LLM credentials.

A fictional operations team receives a message from **Alex Morgan** about **Oak House / Unit 12**. The pipeline can normalize the message, preserve the original event, associate it with an object, extract a structured fact, and place any external response behind outbound policy.

1. `sample_incoming_event.json` is a fictional inbound event.
2. Ingestion deduplicates it using channel and source message identifiers.
3. The agent pipeline can propose structured facts without allowing raw model output to mutate arbitrary state.
4. The operations panel exposes the resulting state for review.
5. Any outbound response remains a draft until policy and approval conditions allow delivery.

No value in this directory is a real customer record.
