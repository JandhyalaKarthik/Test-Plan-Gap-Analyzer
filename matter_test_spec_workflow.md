# Matter Test Spec — Agentic PR Gap Analysis Workflow
**Version:** 3.0  
**Scope:** Automated analysis of Matter Test Specification PRs to identify gaps in the test case repository — surfacing which test cases need to be created or updated, and exactly what those changes should be.

---

## Table of Contents

1. [What This System Does (and Doesn't Do)](#1-what-this-system-does-and-doesnt-do)
2. [Architecture Overview](#2-architecture-overview)
3. [Phase 0: Bootstrapping the Index](#3-phase-0-bootstrapping-the-index)
4. [Phase 1: Trigger & Pre-Flight](#4-phase-1-trigger--pre-flight)
5. [Phase 2: Task Decomposition](#5-phase-2-task-decomposition)
6. [Phase 3: The Agentic Loop](#6-phase-3-the-agentic-loop)
7. [Phase 4: Gap Entry Generation](#7-phase-4-gap-entry-generation)
8. [Phase 5: Report Assembly & Validation](#8-phase-5-report-assembly--validation)
9. [Phase 6: Report Delivery](#9-phase-6-report-delivery)
10. [Failure Handling & Escalation](#10-failure-handling--escalation)
11. [Data Schemas Reference](#11-data-schemas-reference)
12. [Infrastructure & Tooling](#12-infrastructure--tooling)
13. [Configuration Reference](#13-configuration-reference)

---

## 1. What This System Does (and Doesn't Do)

### What a "gap" is

A gap is a mismatch between what the spec says a device must do and what the test case repository actually tests. Three kinds exist:

- A spec PR changes how a cluster behaves, but no test case verifies that specific behavior → **existing TC needs new steps**
- A spec PR adds a new cluster or device type entirely → **new TC(s) must be authored from scratch**
- A spec PR changes something ambiguously — the test impact depends on implementation details a human must assess → **audit required**

### The two repositories

This system works across two completely separate repositories:

| Repository | Role in this system | How it is accessed |
|---|---|---|
| **Matter Test Spec repo** | Source of spec changes | GitHub API only — PR diff text, title, description, and commit messages are fetched. **The `.adoc` spec files themselves are never read.** |
| **Matter Test Case repo** | Target of gap analysis | Local filesystem — the repo is cloned locally, every `.adoc` file is read and indexed, and the system searches through them to find relevant test cases. |

This distinction is critical. The system never opens a spec `.adoc` file. It only sees the **diff** of a spec PR — the lines that changed — plus the PR title and description. The spec content it reasons about is whatever the PR author wrote in their PR description and the changed lines themselves. The test case `.adoc` files, by contrast, are fully read and indexed.

### What the system does

1. Accepts one or more spec PR numbers (and optionally their titles/descriptions/diff URLs if not fetching from GitHub directly)
2. Fetches each PR's diff and metadata from the GitHub API
3. Uses Claude Haiku to decompose each diff into a list of discrete spec changes that may have test case impact
4. For each spec change, searches the test case `.adoc` repo (by keyword grep first, then vector semantic search) to find the closest matching existing test cases
5. Uses Claude Haiku to analyse whether those test cases already cover the spec change, and if not, describes exactly what needs to change
6. Assembles all findings into a structured Markdown gap report
7. Posts a condensed summary as a comment on the original spec PR(s) and commits the full report to the test case repo

### What the system does not do

- Does not read or open spec `.adoc` files
- Does not write to or modify any test case `.adoc` files — all file authoring remains with test plan authors
- Does not cross-reference spec section numbers (e.g. `12.5.8.4`) with test case IDs (e.g. `TC-OO-1.1`) — these are entirely different numbering schemes with no direct mapping
- Does not guarantee complete coverage — it identifies gaps based on what it can retrieve and reason about; low-confidence findings are flagged for human review

---

## 2. Architecture Overview

### System Components

```
┌──────────────────────────────────────────────────────────────────────┐
│                    Matter Test Spec Repository                        │
│   (GitHub) — PR diff, title, description, commit messages only       │
│   .adoc spec files are NOT read                                       │
└──────────────────────────────┬───────────────────────────────────────┘
                               │  GitHub API (read only)
                               ▼
┌──────────────────────────────────────────────────────────────────────┐
│                        Orchestrator Script                            │
│              gap_analyzer.py — coordinates all phases                 │
└────┬──────────────┬──────────────┬────────────────┬──────────────────┘
     │              │              │                │
     ▼              ▼              ▼                ▼
 GitHub API    Claude Haiku    ChromaDB         Test Case Repo
 (PR fetch,    (decompose,     Vector Index     (local clone,
  comment)     reason, review) (nomic embeds)   .adoc files read)

                                        ▼
                               ┌─────────────────┐
                               │   Gap Report     │
                               │  (.md file +     │
                               │  PR comment)     │
                               └─────────────────┘
```

### Guiding Principles

- **Analyse, don't act.** The system identifies what needs to change and precisely describes how — but makes no edits to any file. All authoring decisions remain with test plan authors.
- **Spec PR IDs ≠ Test Case IDs.** Spec section numbers (`12.5.8.4`) and test case IDs (`TC-OO-1.1`) are unrelated numbering schemes. All retrieval is based on cluster names, behavioral keywords, and semantic similarity — never on ID cross-referencing.
- **Retrieval before reasoning.** Claude never guesses which test cases are affected. It always receives retrieved candidate sections from the index before reasoning about gaps.
- **Priority-ranked output.** Every gap entry carries a priority (HIGH / MEDIUM / LOW) and a prescribed action type (create / update / audit), enabling the report reader to triage immediately.
- **No task left silent.** Every decomposed spec change has an explicit terminal state in the report: `gap_identified`, `already_covered`, `no_existing_coverage`, or `analysis_failed`.
- **Multi-PR support.** The system can accept a list of PR numbers and produce one combined report with per-PR grouping and individual PR comments.

---

## 3. Phase 0: Bootstrapping the Index

This phase runs **once** on initial setup, and then again whenever the test case repo changes. It is the foundation that makes retrieval fast and accurate.

### 3a. Header Map Generation (Lightweight, Run on Every Analysis)

Before any analysis run, the orchestrator walks the local test case repo clone and builds a flat JSON map of all AsciiDoc section headers. This is pure text parsing — no LLM calls — and takes seconds even on large repos.

**Process:**
1. `git clone` or `git pull` the latest test case repo.
2. Walk the file tree recursively for all `*.adoc` files.
3. For each file, resolve AsciiDoc attribute definitions (`:picsCode: MCORE.FS`) and substitute `{attr}` references throughout, so headers like `[TC-{picsCode}-1.1]` are stored as their resolved form `[TC-MCORE.FS-1.1]`.
4. Extract every header line and its line number.
5. Emit a flat structure:

```json
{
  "headers": [
    {
      "file": "clusters/fabric_sync/fabric_synchronization.adoc",
      "raw_header_text": "[TC-{picsCode}-1.1] FS Setup [DUT - Initial Commissionee]",
      "header_text": "[TC-MCORE.FS-1.1] FS Setup [DUT - Initial Commissionee]",
      "header_level": 4,
      "line_number": 55,
      "cluster_hint": "fabric_synchronization"
    }
  ]
}
```

**Key design note:** No `tc_id` field and no TC-ID index is built. Because spec and test case IDs use different numbering systems, there is no cross-repo ID lookup. The header map's job is to give the keyword retrieval step (Tier 1 in Phase 3) a line number so it can extract the correct section of a file — not to match IDs.

**Line numbers are the sole output.** The vector index (Section 3b) deliberately stores no line numbers. When the vector search returns a hit, it returns a file path and header text; the orchestrator then consults the header map for the current line number. This means line numbers are always fresh because the header map is always regenerated from the latest state of the repo.

### 3b. Vector Index Construction (Full Rebuild or Incremental)

The vector index enables semantic retrieval — finding test case sections that are *about* the same cluster or behavior as a spec change, even when the wording differs.

**Why the spec and test case IDs don't affect this:**  
The vector index matches on meaning, not on identifiers. A query of "OnOff cluster attribute persistence across power cycle" finds the relevant test case sections regardless of what TC-ID they carry. This is the correct retrieval strategy given that spec section numbers and test case IDs are completely separate systems.

**File type detection:**

Before chunking, each `.adoc` file is classified:
- **Test case file**: has `:picsCode:` attribute, `====` level headers per test case (e.g. `fabric_synchronization.adoc`)
- **Spec file**: has `include::` directives and `[[ref_...]]` anchors (e.g. `Ch06_Attestation.adoc`)
- **Unknown**: chunked conservatively at `===` level

Only test case files are indexed by default. Spec files from the test case repo (if any) are indexed separately and excluded from retrieval unless explicitly requested.

**Chunking strategy:**

Each test case file is split at `====` header boundaries — one chunk per complete test case, including all its sub-sections (Purpose, Preconditions, Required Devices, Device Topology, Test Procedure, Notes). This keeps the entire test case as one semantic unit rather than fragmenting it across Purpose and Test Steps.

Each chunk's metadata — note the deliberate absence of `line_start` / `line_end`:

```json
{
  "chunk_id": "fabric_synchronization.adoc::[TC-{picsCode}-1.1] FS Setup [DUT - Initial Commissionee]",
  "file_path": "clusters/fabric_sync/fabric_synchronization.adoc",
  "raw_header_text": "[TC-{picsCode}-1.1] FS Setup [DUT - Initial Commissionee]",
  "resolved_header_text": "[TC-MCORE.FS-1.1] FS Setup [DUT - Initial Commissionee]",
  "header_level": 4,
  "file_type": "test_case",
  "tc_id": "TC-MCORE.FS-1.1",
  "pics_code": "MCORE.FS",
  "cluster_tags": ["fabric_sync", "commissioner_control"],
  "raw_text": "==== [TC-{picsCode}-1.1] FS Setup...\n===== Purpose\n..."
}
```

**Embedding model:** `nomic-ai/nomic-embed-text-v1.5` via `sentence-transformers`. Runs fully locally — no API key, no cost, no rate limits. The model requires asymmetric task prefixes: documents are embedded with `"search_document: "` prefix at index time; queries are embedded with `"search_query: "` prefix at search time.

**Cluster tag extraction:** After generating embeddings, a batched Claude Haiku call reads each chunk and emits 1–5 snake_case tags (e.g. `fabric_sync`, `on_off`, `commissioner_control`). Tags are stored as metadata for filtered vector search — queries for the OnOff cluster can be filtered to chunks tagged `on_off`, reducing noise from unrelated clusters.

**Incremental updates:** After test case repo changes, only the changed files need re-indexing. Delete all chunks where `file_path` matches the changed file (by metadata filter), re-chunk the new content, and reinsert. No line numbers means no cascading invalidation.

---

## 4. Phase 1: Trigger & Pre-Flight

### 4a. Invocation

The system can be invoked in two ways:

**Manual / scheduled:**
```bash
# Single PR
python gap_analyzer.py --pr 12681 --spec-repo org/matter-test-spec

# Multiple PRs — one combined report
python gap_analyzer.py --prs 12681 12657 12615 --spec-repo org/matter-test-spec

# Dry-run — write report locally, skip GitHub writes
python gap_analyzer.py --prs 12681 12657 --spec-repo org/matter-test-spec --dry-run
```

**GitHub Actions trigger** (on PR merge to spec repo main branch):
```yaml
on:
  pull_request:
    types: [closed]
    branches: [main]
jobs:
  gap-analysis:
    if: github.event.pull_request.merged == true
    runs-on: ubuntu-latest
```

### 4b. Idempotency Check

Before doing any work, the orchestrator checks whether the PR has already been processed:

1. Compute a run key: `SHA256("{pr_number}:{merge_commit_sha or 'open'}")` truncated to 16 hex chars.
2. Check `index/processed_prs.json`. If the key exists with status `report_delivered`, skip and exit.
3. If not, record status `in_progress` and continue.

This prevents duplicate reports if the workflow fires twice for the same merge event (e.g. flaky CI runner). If you run manually and always want a fresh report, you can safely delete `processed_prs.json`.

### 4c. PR Data Extraction

For each PR number, fetch from the GitHub API:

- **Title:** `GET /repos/{owner}/{repo}/pulls/{pull_number}`
- **Description (body):** same endpoint
- **Commit messages:** `GET /repos/{owner}/{repo}/pulls/{pull_number}/commits`
- **Diff:** `GET /repos/{owner}/{repo}/pulls/{pull_number}/files` — returns per-file patches

These are assembled into a **PR Summary Packet**:

```json
{
  "pr_number": 12681,
  "pr_title": "OnOff: add Q quality to OnTime and OffWaitTime attributes",
  "pr_description": "Per spec section 1.5.4, OnTime and OffWaitTime must now persist across power cycles...",
  "merge_commit_sha": "abc123f",
  "commit_messages": ["OnOff: mark OnTime/OffWaitTime as Q quality"],
  "diff": "--- a/clusters/on_off/OnOffCluster.adoc\n+++ b/clusters/on_off/OnOffCluster.adoc\n@@ ...",
  "html_url": "https://github.com/org/matter-test-spec/pull/12681"
}
```

**Important:** This is all the system ever reads from the spec repository. No spec `.adoc` files are opened. The diff text, PR description, and commit messages are the complete input from the spec side.

The diff is character-budget-truncated to approximately `diff_truncation_tokens * 4` characters. If a PR's diff exceeds this, the largest files are truncated first and a notice is appended so the decomposer knows it's working with a partial view.

---

## 5. Phase 2: Task Decomposition

### The Decomposer

This is a single Claude Haiku call per PR. Its only job is to read the PR diff and metadata and produce a structured list of analysis tasks. It does **not** look at any test case content — that happens in Phase 3.

**Why per-PR rather than combined:** When processing multiple PRs, each PR is decomposed separately. Sending all diffs to one call would blur which change came from which PR and make attribution in the report unreliable.

**Input:**
```
You are a task decomposition agent for a Matter protocol test specification gap analysis system.

Given a PR diff and description from a spec repository, decompose the changes into a discrete,
flat list of documentation analysis tasks. Each task represents one spec change that might
require a test case to be created or updated.

Each task must be one of:
- "update": An existing test case likely needs updating to reflect this spec change.
- "create": This introduces new behavior with no existing test case coverage.
- "audit": May affect test cases but requires human judgement to assess scope.
- "no_action": Internal change (editorial, CI, tooling) with no test case impact.

For each task, extract:
- type: "update" | "create" | "audit" | "no_action"
- cluster_area: The cluster or feature area (e.g. "OnOff", "FabricSync")
- tc_id: null — spec section numbers are NOT test case IDs; leave null
- target_description: What test case is affected or needs creating
- search_hints: 2–5 cluster names or behavioral keywords to find relevant .adoc sections
  (do NOT use spec section numbers like "12.5.8.4")
- spec_change_summary: What changed in the spec
- action_required: What the test plan author must do
- priority: "HIGH" | "MEDIUM" | "LOW"
- confidence: "high" | "medium" | "low"

Return ONLY a JSON array. No preamble. No markdown fences.
```

**Output example:**
```json
[
  {
    "task_id": "task_001",
    "type": "update",
    "cluster_area": "OnOff",
    "tc_id": null,
    "target_description": "OnOff test cases covering OnTime and OffWaitTime attribute reads",
    "search_hints": ["OnOff", "OnTime", "OffWaitTime", "non-volatile", "power cycle"],
    "spec_change_summary": "Q quality added to OnTime and OffWaitTime — values must now persist across power cycles.",
    "action_required": "Add reboot persistence verification steps to existing OnOff TCs. Author TC-OO-4.1 for explicit non-volatile storage validation across a power cycle.",
    "priority": "HIGH",
    "confidence": "high"
  },
  {
    "task_id": "task_002",
    "type": "create",
    "cluster_area": "MediaFileManagement",
    "tc_id": null,
    "target_description": "New cluster with no existing test cases",
    "search_hints": ["MediaFileManagement", "file listing", "upload", "download", "access control"],
    "spec_change_summary": "MediaFileManagement cluster added to spec for the first time.",
    "action_required": "Author full cluster coverage: TC-MFM-1.1 attribute read/write, TC-MFM-1.2 file listing, TC-MFM-1.3 upload/download, TC-MFM-1.4 conformance, TC-MFM-1.5 access control.",
    "priority": "HIGH",
    "confidence": "high"
  }
]
```

### Decomposer Quality Gates

After receiving the JSON:

1. **Schema validation:** All required fields must be present and correctly typed. Reject and retry (max 2 times) if malformed.
2. **`no_action` filter:** Remove `no_action` tasks. Log them for the audit trail but do not process further.
3. **Low-confidence flagging:** Tasks with `confidence: "low"` are marked `review_flag: true` immediately. They are still processed, but their gap entries are marked `⚠️ Verify` in the report.
4. **Priority cross-check:** A `create` task with `priority: "LOW"` is suspicious — a brand-new cluster needing TCs is almost never low priority. Flag it for review.

---

## 6. Phase 3: The Agentic Loop

The orchestrator iterates through each task sequentially, retrieving existing test case content from the test case repo and reasoning about whether it covers the spec change.

### 6a. Retrieval (Three-Tier Cascade)

For each task, the retrieval step finds the existing test case sections most likely to be relevant to the spec change. The cascade stops at the first tier that returns a confident match.

**There is no TC-ID lookup tier.** Spec PRs use spec section numbers; test case files use their own ID scheme. These are completely unrelated and can never be cross-referenced by value. All retrieval is semantic.

---

**Tier 1 — Keyword / Grep**

Runs `grep -ril` across the local test case repo clone using the task's `search_hints` (cluster names, behavioral keywords). Each hint is searched independently; files are scored by how many hints they match. The top 3 files above the minimum match threshold are returned.

For each matching file, the header map is consulted to find the best section to pass to the reasoner — specifically the first `====`-level header (a complete test case section), starting from its line number in the file. This ensures the reasoner receives the actual test steps rather than the copyright header at the top of the file.

Returns: up to 3 `Candidate` objects with `file_path`, `raw_header_text`, `resolved_header_text`, and `raw_text` (the section body starting at the matched header).

---

**Tier 2 — Vector Semantic Search**

If Tier 1 returns nothing, queries ChromaDB with the task's `target_description` + `spec_change_summary` as a natural language query. Applies a `cluster_tags` metadata filter if a cluster name can be inferred from `search_hints`, narrowing results to chunks for that cluster.

Returns the top results above the configured similarity threshold (default 0.72). Results below the threshold are not used — a low-confidence vector match is worse than no match, because it would send the reasoner irrelevant content.

---

**Tier 3 — No Match**

If both tiers return nothing:
- For `create` tasks: `coverage_status = "no_existing_coverage"` — this is expected and correct. It confirms the gap.
- For `update` tasks: `coverage_status = "uncertain_coverage"` — an update task with no retrievable test case is suspicious. The task is flagged `review_flag: true`.

Both routes proceed to the reasoner with an alternative prompt that doesn't require candidate content.

---

**Retrieval Result Object:**
```json
{
  "task_id": "task_001",
  "retrieval_tier": 1,
  "retrieval_method": "keyword",
  "coverage_status": "existing_coverage_found | no_existing_coverage | uncertain_coverage",
  "candidates": [
    {
      "file_path": "clusters/on_off/on_off.adoc",
      "raw_header_text": "[TC-{picsCode}-1.1] OnOff Attribute Read",
      "resolved_header_text": "[TC-MCORE.OO-1.1] OnOff Attribute Read",
      "tc_id": "TC-MCORE.OO-1.1",
      "similarity_score": 0.0,
      "raw_text": "==== [TC-{picsCode}-1.1] OnOff Attribute Read\n===== Purpose\n..."
    }
  ]
}
```

Note: `similarity_score` is 0.0 for keyword hits (no cosine similarity computed at Tier 1); non-zero only for Tier 2 vector results.

### 6b. Contextual Reasoning

With retrieved candidate sections in hand, Claude Haiku performs the gap analysis. This is a separate LLM call per task — keeping each context window small and focused.

**Two prompt paths:**

**Path A — Existing coverage found** (Tiers 1 or 2 succeeded):
```
You are a test plan gap analyst for the Matter protocol certification test suite.

--- SPEC CHANGE ---
Cluster/Area: {cluster_area}
PR Summary: {spec_change_summary}
Required Action (from initial analysis): {action_required}

--- EXISTING TEST CASE CONTENT (from {file_path}) ---
Section: {resolved_header_text}

{raw_text}

Analyse whether the existing test case already covers the spec change.
Be specific: name actual test steps, PICS conditions, table columns, or attribute names.

Respond ONLY in JSON:
{
  "coverage_verdict": "already_covered | partial_gap | full_gap | new_tc_needed",
  "match_confidence": "high | medium | low",
  "gap_details": "Precise description of what is missing. Name specific steps/fields.",
  "affected_tcs": ["TC-MCORE.OO-1.1"],
  "action_type": "update | create | audit | verify",
  "action_required": "Exact instruction for the test plan author.",
  "priority": "HIGH | MEDIUM | LOW"
}
```

**Path B — No existing coverage** (`no_existing_coverage` or `uncertain_coverage`):
```
A spec PR has introduced new behavior. No existing test cases were found
for this cluster/area in the test case repository.

--- SPEC CHANGE ---
Cluster/Area: {cluster_area}
PR Summary: {spec_change_summary}

Produce a gap entry. Suggest TC numbering (TC-{PICS_CODE}-1.x starting at 1.1)
if the cluster is entirely new.

Respond ONLY in JSON: { "coverage_verdict": "new_tc_needed", ... }
```

**Coverage verdicts:**

| Verdict | Meaning | Report treatment |
|---|---|---|
| `already_covered` | Existing TC fully addresses the spec change | Listed under "No Action Required" |
| `partial_gap` | TC exists but is missing specific steps or assertions | `update` action with step-level detail |
| `full_gap` | TC cluster exists but behavior not covered | `update` + possibly new TC |
| `new_tc_needed` | No TC for this cluster/behavior at all | `create` action with suggested TC IDs |

---

## 7. Phase 4: Gap Entry Generation

For each task that completes Phase 3, a structured **gap entry** is assembled. This is the atomic unit of the final report.

### 7a. Assembly

Combine the task (from Phase 2), retrieval result (from Phase 3a), and reasoning output (from Phase 3b):

```json
{
  "gap_id": "gap_001",
  "task_id": "pr12681_task_001",
  "source_pr": "#12681",
  "cluster_area": "OnOff",
  "spec_change": "Q quality on OnTime & OffWaitTime — values must persist across power cycles.",
  "affected_tcs": ["TC-MCORE.OO-1.1", "TC-MCORE.OO-2.1", "TC-MCORE.OO-3.1"],
  "coverage_verdict": "partial_gap",
  "gap_details": "Existing TCs read OnTime and OffWaitTime but do not verify persistence after reboot. No power-cycle step exists.",
  "action_type": "update",
  "action_required": "Add a power-cycle step after step 4 in TC-OO-1.1, 2.1, and 3.1 that re-reads OnTime and OffWaitTime and verifies values survived the reboot. Author TC-OO-4.1 for dedicated non-volatile storage validation.",
  "priority": "HIGH",
  "source_file": "clusters/on_off/on_off.adoc",
  "match_confidence": "high",
  "review_flag": false,
  "terminal_state": "gap_identified"
}
```

**Terminal states:**

| State | Meaning |
|---|---|
| `gap_identified` | Gap found with actionable detail |
| `already_covered` | Existing TCs cover this change — no action needed |
| `no_existing_coverage` | No TCs found for this cluster/behavior |
| `analysis_failed` | LLM call failed after retries — manual review required |

### 7b. `create` Tasks with No Existing Coverage

When a `create` task hits Tier 3 (no match), the gap entry is assembled from the decomposer's `action_required` text plus the `no_existing_coverage` confirmation. The reasoner is asked to suggest a TC numbering scheme using `TC-{PICS_CODE}-1.x` starting at 1.1 for entirely new clusters.

New TC count is calculated as `max(len(affected_tcs), 1)` per create entry — so even when no specific TC IDs are suggested yet, the count is at least 1 per new cluster area.

### 7c. `audit` Tasks

`audit` tasks produce gap entries that appear in a separate "Needs Review" section of the report. They carry a description of what the human must check rather than a prescriptive action, and are used for ambiguous spec changes where test impact depends on implementation details.

---

## 8. Phase 5: Report Assembly & Validation

### 8a. Completeness Check

Every task from the decomposed list must have a gap entry with a valid terminal state before the report is generated:

```python
for task in all_tasks:
    assert task.task_id in gap_entries
    assert gap_entries[task.task_id].terminal_state in VALID_TERM_STATES
```

If this fails (task without a result), the report is still generated but a partial-analysis warning is added prominently at the top.

### 8b. Priority Distribution Check

Sanity-check the priority mix. Log a warning (not a block) if:
- More than 80% of entries are HIGH — the decomposer may be over-prioritising.
- All entries are `already_covered` — retrieval may have matched the wrong sections.

### 8c. LLM Self-Review (Sampling)

For runs with 8 or more gap entries, randomly sample 3 `gap_identified` entries and run a self-review:

```
Review this gap analysis entry. Is it:
1. Accurately describing a real gap (not already covered)?
2. Giving specific, actionable instructions?
3. Correctly prioritised?

Respond: { "accurate": bool, "specific": bool, "priority_correct": bool, "notes": "..." }
```

Any `false` field causes the entry to be flagged `⚠️ Verify` in the report with the reviewer's notes appended.

### 8d. Run Summary Statistics

```json
{
  "pr_numbers": [12681, 12657],
  "prs_analyzed": 2,
  "total_spec_changes": 18,
  "gaps_identified": 12,
  "already_covered": 3,
  "no_existing_coverage": 4,
  "analysis_failed": 1,
  "new_tcs_needed": 9,
  "clusters_needing_updates": 8,
  "high_priority_count": 6,
  "medium_priority_count": 4,
  "low_priority_count": 2,
  "entries_flagged_for_verify": 2
}
```

---

## 9. Phase 6: Report Delivery

### 9a. Report Structure

The gap report mirrors the Cert Gap Report format. It has three sections:

**Section 1 — Stats Banner**
```
| N Spec Changes Analyzed | N Clusters Need New TCs (N total) | N Clusters Need TC Updates | N Needs Review Only |
```

**Section 2 — PR-by-PR Gap Analysis Table**

One row per actionable gap entry, sorted by source PR then cluster name:

| PR # | Cluster / Area | Spec Change | Affected TCs | Action Required |
|---|---|---|---|---|

Entries with `⚠️ Verify` include the self-review notes in the Action Required column.

**Section 3 — Action Summary (Priority-Sorted)**

| Priority | Cluster | Action |
|---|---|---|
| HIGH ⚠️ | MediaFileManagement | Author 5 new TCs... |
| HIGH | OnOff | Add power-cycle steps... |
| MEDIUM | NetworkCommissioning | Audit struct assertions... |
| LOW | NETIM | Rename ClientNumber → ClientIndex... |

Already-covered entries appear in a collapsed "No Action Required" section at the bottom.

### 9b. Output Formats

**Full Markdown report** — committed as `GAP_REPORT_PR_{pr_numbers}_{date}.md` to the `reports/auto` branch of the test case repo. No PR is opened automatically — a human decides whether to merge it.

**GitHub comment** — a condensed version (stats banner + action summary only) is posted directly on each analyzed spec PR. In multi-PR mode, each PR gets its own comment showing only its relevant entries. The comment links to the full report.

### 9c. Multi-PR Behaviour

When `--prs 12681 12657 12615` is passed:
- Each PR is fetched and decomposed independently (separate diffs → separate decomposer calls)
- Tasks from all PRs are tagged with their source PR: `pr12681_task_001`, `pr12657_task_001`, etc.
- The retrieval and reasoning loop runs over all tasks combined
- One combined Markdown report is generated, with the PR-by-PR table grouped by source PR
- Each spec PR receives its own GitHub comment showing only its gap entries

### 9d. Processed PR Registry

After delivery, record the run in `index/processed_prs.json`:
```json
{
  "abc123def456": {
    "status": "report_delivered",
    "completed_at": "2026-02-27T14:32:11Z",
    "pr_numbers": [12681],
    "report_file": "reports/GAP_REPORT_PR_12681_20260227.md",
    "gaps_identified": 9,
    "new_tcs_needed": 12
  }
}
```

---

## 10. Failure Handling & Escalation

### Task-Level Terminal States

| State | Meaning | Report treatment |
|---|---|---|
| `gap_identified` | Gap found, action described | Main gap table + action summary |
| `already_covered` | Existing TCs cover this change | "No Action Required" section |
| `no_existing_coverage` | No TCs for this cluster/behavior | `create` entry in action summary |
| `analysis_failed` | LLM call failed after retries | Unresolved entry; manual review required |

Entries with `review_flag: true` appear with `⚠️ Verify` in the report regardless of terminal state.

### Run-Level Failure Modes

**Partial analysis:** If the loop processes 10 of 13 tasks before an unhandled exception, the partial gap entries are kept and the report is generated with a warning listing the unprocessed tasks. Partial reports are better than no reports.

**Total failure:** If Phase 1 (can't fetch PR) or Phase 2 (decomposer fails repeatedly) fails, the run aborts and a GitHub comment is posted on the spec PR stating the analysis failed with the error reason.

**Rate limiting:** Claude API calls use exponential backoff with jitter on 429 responses. Max 5 retries per call. If all retries are exhausted the task is marked `analysis_failed` and the run continues.

---

## 11. Data Schemas Reference

### Task Object
```json
{
  "task_id": "pr12681_task_001",
  "type": "update | create | audit | no_action",
  "cluster_area": "OnOff",
  "tc_id": null,
  "target_description": "string",
  "search_hints": ["OnOff", "OnTime", "power cycle"],
  "spec_change_summary": "string",
  "action_required": "string",
  "priority": "HIGH | MEDIUM | LOW",
  "confidence": "high | medium | low",
  "review_flag": false
}
```

Note: `tc_id` is always `null`. Spec section numbers are not test case IDs and are never used for retrieval.

### Retrieval Result Object
```json
{
  "task_id": "pr12681_task_001",
  "retrieval_tier": 1,
  "retrieval_method": "keyword | vector | none",
  "coverage_status": "existing_coverage_found | no_existing_coverage | uncertain_coverage",
  "candidates": [
    {
      "file_path": "clusters/on_off/on_off.adoc",
      "raw_header_text": "[TC-{picsCode}-1.1] OnOff Attribute Read",
      "resolved_header_text": "[TC-MCORE.OO-1.1] OnOff Attribute Read",
      "tc_id": "TC-MCORE.OO-1.1",
      "similarity_score": 0.0,
      "raw_text": "section text..."
    }
  ]
}
```

### Gap Entry Object
```json
{
  "gap_id": "gap_001",
  "task_id": "pr12681_task_001",
  "source_pr": "#12681",
  "cluster_area": "OnOff",
  "spec_change": "string",
  "affected_tcs": ["TC-MCORE.OO-1.1"],
  "coverage_verdict": "already_covered | partial_gap | full_gap | new_tc_needed",
  "gap_details": "string",
  "action_type": "update | create | audit | verify",
  "action_required": "string",
  "priority": "HIGH | MEDIUM | LOW",
  "source_file": "clusters/on_off/on_off.adoc | null",
  "match_confidence": "high | medium | low",
  "review_flag": false,
  "terminal_state": "gap_identified | already_covered | no_existing_coverage | analysis_failed",
  "self_review_notes": "null | string"
}
```

---

## 12. Infrastructure & Tooling

### Required Services

| Component | Tool | Notes |
|---|---|---|
| LLM | Claude Haiku (`claude-haiku-4-5-20251001`) | All LLM calls: decomposition, gap reasoning, cluster tagging, self-review. Available on Claude free plan. |
| Embeddings | `nomic-ai/nomic-embed-text-v1.5` | Via `sentence-transformers`. Fully local — no API key, no cost. ~274 MB model, downloads once. |
| Vector Store | ChromaDB (local) | Persistent on disk. Pinecone can substitute if a hosted store is preferred. |
| Test Case Repo | Local clone | `git clone` or `git pull` before each run to ensure freshness. |
| GitHub API | `PyGithub` | Read-only on spec repo (fetch PR diff). Read+comment on spec PR. Write to test case repo reports branch. |

### Environment Variables

```
ANTHROPIC_API_KEY       # Claude Haiku for all LLM calls
GITHUB_TOKEN            # Read spec repo + comment on PRs + write reports branch
DOCS_REPO_NAME          # e.g. "org/matter-test-cases" — used for report commit URLs
SPEC_REPO_NAME          # e.g. "org/matter-test-spec" — source of PR diffs
CHROMA_PERSIST_DIR      # Local path for ChromaDB, e.g. "./chroma_db"
DOCS_REPO_LOCAL_PATH    # Local clone of test case repo, e.g. "./matter-test-cases"
```

No OpenAI key required. Embeddings are generated locally.

### Python Dependencies

```
anthropic>=0.25
chromadb>=0.4
PyGithub>=1.59
PyYAML>=6.0
tiktoken>=0.5
gitpython>=3.1
sentence-transformers>=2.7
```

### File Layout

```
gap_analyzer.py          # Main orchestrator (Phases 1–6)
vector_index.py          # Phase 0 — index construction and search
workflow_config.yaml     # Tunable parameters
index/
  header_map.json        # Generated on each run from test case repo
  processed_prs.json     # Idempotency registry
chroma_db/               # ChromaDB persistence (CHROMA_PERSIST_DIR)
reports/                 # Generated gap reports (local, before push)
```

---

## 13. Configuration Reference

```yaml
retrieval:
  similarity_threshold: 0.72        # Vector results below this are discarded
  max_vector_candidates: 5          # Top-N results from vector search
  keyword_min_matches: 2            # Minimum hint matches for a grep result to qualify

analysis:
  self_review_sample_size: 3        # Gap entries to self-review per run
  self_review_min_entries: 8        # Skip self-review for runs smaller than this
  high_priority_ratio_warning: 0.80 # Warn if >80% of entries are HIGH
  decomposer_max_retries: 2         # Retries on malformed decomposer JSON

llm:
  model: "claude-haiku-4-5-20251001"
  decomposer_max_tokens: 4096
  reasoner_max_tokens: 2048
  diff_truncation_tokens: 8000      # Max tokens of diff sent per PR to decomposer

report:
  report_filename_prefix: "GAP_REPORT_PR"
  reports_branch: "reports/auto"
  post_github_comment: true
  commit_full_report: true

cluster_owners:                     # Used for @-mentions in GitHub comments
  "OnOff": ["@on-off-team"]
  "FabricSync": ["@fabric-sync-team"]
  "default": ["@test-plan-maintainers"]
```

---

## Implementation Notes

**Phase the rollout.** Build and validate in this order:
1. Phase 0 (index) + Phase 3a retrieval only — run on historical PRs and check whether the right test case files are being found before adding any LLM reasoning.
2. Add Phase 2 (decomposer) — validate the task lists it produces on historical PRs.
3. Add Phase 3b (reasoning) + Phase 4 (gap entries) — evaluate gap entry quality on a small batch.
4. Add Phase 5 (validation + self-review) + Phase 6 (delivery) last.

**The diff is not the spec.** The system only sees changed lines, not the full context of the spec section. For subtle conformance changes the decomposer may miss the significance. Consider adding a mechanism to optionally fetch the surrounding spec section text (a few hundred lines before/after the diff hunk) when the PR description is sparse.

**The test case repo must be cloned locally.** The system reads `.adoc` test case files directly from the filesystem. Point `DOCS_REPO_LOCAL_PATH` at a fresh clone kept up-to-date with `git pull` before each run.
