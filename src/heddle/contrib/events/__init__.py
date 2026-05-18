"""heddle.contrib.events — event-sourcing patterns on top of Heddle.

Sprint 1 ships the wire-contract layer:

- :mod:`heddle.contrib.events.envelopes` — ``EventEnvelope`` /
  ``CommandMessage`` and their metadata models.
- :mod:`heddle.contrib.events.subjects` — NATS subject + stream-name
  helpers for the event-sourcing wire contract.
- :mod:`heddle.contrib.events.issuer_conventions` — reserved
  ``issued_by`` prefixes and the framework-issuer predicate.

Subsequent sprints add aggregate base classes, event/rejection logs,
command handler, dispatcher, framework projectors, and concrete
aggregates.

See ``heddle-contrib-events-m2-architecture-v7.md`` for the full plan.
"""
