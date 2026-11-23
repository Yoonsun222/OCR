# Implementation Plan Index

These plans implement the approved secure multimodal RAG specification without using company documents or unapproved model artifacts.

## Dependency order

```text
Common security contracts
  +-- Windows offline toolkit
  +-- Track B native tables/charts
  +-- Track A explicit label grounding
          \            /
           Track C mixed-document retrieval
```

1. [Common security contracts](2026-11-24-common-security-contracts.md) freezes Evidence, authorization, generation, deletion, error, and observability interfaces.
2. [Windows offline toolkit](2026-11-24-windows-offline-toolkit.md) provides bundle verification, `doctor`, configuration, synthetic demo, and reproducibility artifacts.
3. [Track B native tables and charts](2026-11-24-track-b-native-tables-charts.md) implements the first product wedge using synthetic CSV/XLSX fixtures and native OPC parsing.
4. [Track A explicit label grounding](2026-11-24-track-a-explicit-label-grounding.md) implements the separate drawing-label spike using synthetic SVG and recorded OCR interchange.
5. [Track C mixed-document retrieval](2026-11-24-track-c-mixed-document-retrieval.md) begins integration only after A/B evidence adapters pass the common conformance suite.

## Execution gates

- Common contracts and synthetic fixtures must pass before any adapter work is merged.
- The Windows `doctor` and offline synthetic demo must pass before approved artifacts are introduced.
- Track B is the first narrow business-value pilot.
- Track A remains explicit visible-label localization; inferred engineering meaning is excluded.
- Track C cannot begin full integration until Track A and Track B expose contract-conformant Evidence.
- Real company documents remain blocked until the organizational, security, license, retention, and artifact-intake approvals in the specification are complete.

## Recommended execution mode

Use `superpowers:subagent-driven-development` for task-by-task implementation and two-stage review. Use `superpowers:executing-plans` only when inline batch execution with explicit checkpoints is preferred. Each plan requires TDD and a small commit after every task.

