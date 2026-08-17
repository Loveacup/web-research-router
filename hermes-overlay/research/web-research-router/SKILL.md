---
name: web-research-router
description: "Use when a question needs fresh external evidence, source-backed comparison, fact verification, or a structured research brief. Do not use for local-file operations or direct source inspection."
type: routine
license: MIT
---
<!-- wrr-overlay-provenance
source-sha256: ab5c5f316a25033511735144e8bd6b295894028f009c943f336d53061e506e43
generated-by: scripts/render_hermes_overlay.py
regenerate from the clean-room core; do not edit by hand
-->

# Web Research

## Red flags

| Shortcut | Why it fails |
|---|---|
| "I already know the answer" | A fresh claim needs evidence, not memory. |
| "This dated snapshot is close enough" | A snapshot does not establish the freshness requirement. |
| "A result list is enough" | A useful answer needs claim-level evidence and an explicit evidence boundary. |

## Purpose

Use this workflow to turn an external question into a bounded, source-backed answer. State the question, its freshness requirement, and the evidence standard before drawing a conclusion.

## Research posture

Before the first external call, name the research posture: **discovery** for a landscape, **grounding** for a current or checkable claim, **research** for a multi-claim brief, or a more specialised posture only when the active runtime exposes one. Inspect the live tool schema, then use its supported mode or let its documented classifier decide. Do not hardcode engines, providers, fallback order, or optional arguments in this method.

Read user-provided material and relevant local context first. Treat it as leads and prior decisions, not as proof of a fresh external claim. Record the evidence gap that makes external research necessary.

## Boundaries

- Do not use this workflow for local-file operations.
- For direct source inspection, use a specialized source-inspection workflow.
- Do not assume a particular capability exists. Work only with capabilities that are actually present in the active environment.
- Do not turn sparse material into a confident conclusion.
- Do not treat a dated snapshot, a summary, or an unsupported assertion as fresh evidence.

## Evidence loop

1. Frame the question, the freshness requirement, research posture, and what would count as sufficient evidence.
2. Inspect user-provided material and relevant context already present in the task; state the gap before searching externally.
3. Gather discovery material, then prefer primary sources for material claims.
4. Extract claim-level evidence before synthesis. Keep each claim tied to the source that supports it.
5. Cross-check time-sensitive, high-impact, numerical, legal, attribution, or contentious claims with an independent source when feasible.
6. If the runtime reports route quality, partial failure, or insufficient independent sources, preserve that signal in the evidence boundary; do not silently upgrade it to confidence.
7. Separate the result into **Confirmed**, **Inference**, and **Conflicts & gaps**. Make the boundary between them explicit.

## Multi-round escalation

Use multiple rounds when the question is multi-dimensional, decision-critical, or still source-incomplete after the first pass. Each round must close a named gap, preserve claim/source provenance, and end with a stopping condition. Stop when the remaining gap cannot materially change the answer or when further evidence cannot be obtained.

## Output contract

Return:

1. A direct answer proportionate to the evidence.
2. A source list for material claims.
3. An evidence boundary explaining what the sources do and do not establish, including any material route-quality limitation.
4. Any unresolved uncertainty, conflict, or missing evidence that could change the conclusion.

## Verification checklist

- [ ] I stated the freshness requirement and evidence standard.
- [ ] I named the research posture and used only the active runtime's supported routing contract.
- [ ] I used primary sources for material claims when feasible.
- [ ] I kept claim-level evidence separate from interpretation.
- [ ] I preserved any reported route-quality limitation instead of treating partial retrieval as complete evidence.
- [ ] I labeled Confirmed, Inference, and Conflicts & gaps distinctly.
- [ ] I stated the source list, evidence boundary, and unresolved uncertainty.
<!-- wrr-overlay-binding -->
## Hermes runtime binding

This overlay runs inside the Hermes runtime, which loads the `wrr` toolset. Use
those tools for external research; the registered live schema owns every optional
argument and the tool's own behavior.

- `web_search` takes a non-empty `query` and returns discovery evidence.
- `web_fetch` retrieves the page at a chosen `url`.
- `web_similar` expands from a reference `url` to related sources.

Tool output and metadata are evidence about one execution, not a conclusion.
Keep the canonical evidence boundary between Confirmed, Inference, and gaps.

Do not use this overlay for local-file work, source inspection, plugin
administration, configuration, or runtime setup. Nothing here chooses tool
arguments beyond the required inputs above; the registered plugin schema and
its implementation own everything else.
