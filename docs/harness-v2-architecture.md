# Harness V2 Architecture

Harness V2 treats agent orchestration as an event-driven saga system. The core
rule is simple: raw evidence is immutable, canonical evidence is auditable, and
workflow decisions are replayable from events.

## Bounded Contexts

```text
CLI / UI / API
  -> Control Plane
  -> Execution Plane
  -> Artifact Plane
  -> Materialization Plane
  -> Gate Plane
  -> Event Store
  -> Observability Plane
```

### Control Plane

Owns task state, workflow runs, saga definitions, and route decisions. It does
not parse model-specific artifacts directly. It consumes `Decision` and
`GateResult` records emitted by other planes.

### Execution Plane

Owns calls to Claude, Codex, Docker, SWE-bench, and future execution providers.
It emits raw outputs and runtime failure events. It does not decide workflow
routes.

### Artifact Plane

Owns raw artifacts, canonical artifacts, schema validation, semantic validation,
and canonicalization audit records. It never overwrites raw artifacts.

### Materialization Plane

Owns repository snapshots, patch application, duplicate patch detection, and
no-op classification. It emits snapshot results instead of workflow decisions.

### Gate Plane

Owns patch, tester, runtime readiness, and final validation gates. Every gate
returns a structured `GateResult`, never a bare boolean.

### Event Store

Owns append-only facts. Every service communicates by appending events with a
`trace_id` and `correlation_id`.

### Observability Plane

Owns dashboards, logs, metrics, and replay views. It is read-only with respect
to workflow decisions.

## Event Rules

- Every event has `event_id`, `event_type`, `schema_version`, `created_at`,
  `trace_id`, and `correlation_id`.
- Events are append-only.
- Replays must derive the same decisions from the same event stream.
- Services may read other planes through public query APIs, but may not mutate
  their storage.

## Artifact Rules

- `RawArtifact` is immutable model output.
- `CanonicalArtifact` is Harness-normalized output derived from one raw
  artifact.
- `CanonicalizationChange` records field path, original value, canonical value,
  and rule name.
- Peripheral template markers may be removed only through a canonicalization
  event.
- Core `pending_model_completion` values must reject the artifact.

## Saga Rules

Each saga step defines:

- command
- expected events
- timeout
- retry budget
- compensation
- next route

Bugfix V2 must include the compensation route:

```text
ContractChanged -> RetestCurrentRepoSnapshot
```

This route prevents the no-op patch loop where executor is asked to produce a
new patch even though the current repository snapshot already contains the
candidate fix.

## Migration Strategy

V2 is not a compatibility wrapper around the old workflow. Old modules can
coexist during migration, but new code must target the bounded contexts above.

The first implementation milestone is a runnable event and artifact-plane
baseline. After that, workflow routes move into saga definitions one path at a
time.

## Current Implementation Map

- Event Store: `harness/events/store.py` provides durable SQLite append/replay
  through `SQLiteEventStore`.
- Artifact Plane: `harness/artifact_plane/service.py` stores raw outputs first,
  canonicalizes copied outputs, and emits audited artifact events.
- Replay: `harness/replay/` loads real failure fixtures and checks saga route
  decisions against append-only event streams.
- Bugfix Saga Bridge: `harness/workflow/saga_adapter.py` records the current
  bugfix main-chain route decisions without rewriting the entire legacy engine
  in one step.
