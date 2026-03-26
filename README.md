# Matter Test Plan Gap Analyzer

Automatically analyses spec PRs and identifies which test cases in the test case repository need to be created or updated. Produces a structured Markdown report and posts a summary comment on each spec PR.

---

## How it works

The system operates across two completely separate repositories:

| Repository | How it's accessed | What's read |
|---|---|---|
| **Spec repo** | GitHub API (read-only) | PR diff, title, description, commit messages. The `.adoc` spec files are never opened. |
| **Test case repo** | Local path or GitHub repo URL | The analyzer materializes the selected branch locally, then reads, chunks, and indexes the `.adoc` files under the requested subfolder. |

For each input PR, Claude Haiku decomposes the diff into a list of spec changes that may require test case work. For each change, the system searches the test case repo by keyword grep and vector similarity, then reasons about whether existing test cases cover the change. The result is a prioritised gap report.

No test case files are modified. The system is purely analytical.

---

## Repository layout

```
gap_analyzer.py          # Main orchestrator — run this
vector_index.py          # Index builder and semantic search
workflow_config.yaml     # Tunable parameters (edit this)
.github/workflows/
  gap_analysis.yml       # GitHub Actions trigger
chroma_db/               # ChromaDB vector store (auto-created)
index/
  header_map.json        # Auto-generated on each run
```

---

## Setup

### 1. Clone this repo

```bash
git clone https://github.com/your-org/matter-gap-analyzer
cd matter-gap-analyzer
```

### 2. Decide how you'll point at the test case repo

You can pass either:

- A local path, such as `./matter-test-cases`
- An `owner/repo` string, such as `your-org/matter-test-cases`
- A GitHub URL, such as `https://github.com/your-org/matter-test-cases`

For remote repos, `gap_analyzer.py` clones the requested branch automatically. Private repos work as long as the configured token has access. If your `.adoc` files live below the repo root, pass `--docs-subdir path/inside/repo`.

### 2a. Input mapping

These are the four locations people usually care about:

| What you want to control | Current input | Notes |
|---|---|---|
| Spec repo | `--spec-repo` | Accepts local git checkout path, `owner/repo`, or GitHub URL |
| Spec starting directory | Not used by the current analyzer | The analyzer reads spec PR metadata and diff from GitHub; it does not walk the spec repo filesystem |
| Test case repo | `--docs-repo` / `--docs-path` | Accepts local path, `owner/repo`, or GitHub URL |
| Test case starting directory | `--docs-subdir` | Repo-relative folder containing the `.adoc` test files |

If you were expecting a spec-side starting directory flag, that is not part of the current design. On the spec side, the PR number already determines the changed files because the app analyzes the GitHub PR diff directly.

### 3. Install dependencies

```bash
pip install \
  anthropic>=0.25 \
  chromadb>=0.4 \
  PyGithub>=1.59 \
  PyYAML>=6.0 \
  tiktoken>=0.5 \
  gitpython>=3.1 \
  sentence-transformers>=2.7
```

The embedding model (`nomic-ai/nomic-embed-text-v1.5`, ~274 MB) downloads automatically from HuggingFace on first run and is cached locally. No OpenAI key is needed.

### 4. Set environment variables

```bash
export ANTHROPIC_API_KEY="sk-ant-..."        # Claude Haiku — free plan works
export SPEC_GITHUB_TOKEN="ghp_spec_..."     # Read spec repo PRs + post PR comments
export DOCS_GITHUB_TOKEN="ghp_docs_..."     # Clone/pull/push the private test case repo
# optional single-token fallback if one token can access both:
export GITHUB_TOKEN="ghp_shared_..."
export DOCS_REPO="https://github.com/your-org/matter-test-cases"
export DOCS_REPO_BRANCH="main"              # optional
export DOCS_REPO_SUBDIR="."                 # optional
export CHROMA_PERSIST_DIR="./chroma_db"
export GAP_REPO_CACHE_DIR="./repo_cache"    # optional temp clone location
```

### 4a. Private repo credentials

Token resolution works like this:

- Spec repo API access and PR comments use `SPEC_GITHUB_TOKEN`, falling back to `GITHUB_TOKEN`
- Test case repo clone and report push use `DOCS_GITHUB_TOKEN`, falling back to `GITHUB_TOKEN`

So:

- one shared token is enough if it can access both private repos
- two separate tokens are supported if the spec and test repos live in different orgs or have different access controls

Example for two different private repos:

```bash
export SPEC_GITHUB_TOKEN="ghp_spec_..."
export DOCS_GITHUB_TOKEN="ghp_docs_..."

python gap_analyzer.py \
  --pr 12681 \
  --spec-repo https://github.com/your-org/private-spec-repo \
  --docs-repo https://github.com/other-org/private-test-repo \
  --docs-subdir .
```

### 5. Optional: prebuild or inspect the vector index

`gap_analyzer.py` now builds or incrementally updates the correct vector index automatically for the selected docs repo, branch, and subfolder. You only need `vector_index.py` if you want to inspect chunking or prebuild from a local checkout.

```bash
python vector_index.py ./matter-test-cases
```

Takes 2–10 minutes depending on how many test case files exist. Progress is logged. Subsequent runs use the cached index.

---

## Running

### Analyse a single PR

```bash
python gap_analyzer.py \
  --pr 12681 \
  --spec-repo your-org/matter-test-spec \
  --docs-repo https://github.com/your-org/matter-test-cases
```

### Analyse a single PR with both repo locations spelled out

```bash
python gap_analyzer.py \
  --pr 12681 \
  --spec-repo https://github.com/your-org/matter-test-spec \
  --docs-repo https://github.com/your-org/matter-test-cases \
  --docs-subdir src/tests
```

### Analyse multiple PRs (one combined report)

```bash
python gap_analyzer.py \
  --prs 12681 12657 12615 12523 \
  --spec-repo your-org/matter-test-spec \
  --docs-repo your-org/matter-test-cases
```

### Dry run with explicit branches and a subfolder

```bash
python gap_analyzer.py \
  --prs 12681 12657 \
  --spec-repo https://github.com/your-org/matter-test-spec \
  --spec-branch release/1.6 \
  --docs-repo https://github.com/your-org/matter-test-cases \
  --docs-branch release/1.6 \
  --docs-subdir src/tests \
  --dry-run
```

### Options

| Flag | Description |
|---|---|
| `--pr N` | Single PR number |
| `--prs N N N` | Multiple PR numbers |
| `--spec-repo` | Spec repo as `owner/repo`, GitHub URL, or local git checkout (required) |
| `--spec-branch BRANCH` | Expected base branch for the spec PRs (optional) |
| Spec starting directory | No CLI flag today | Not needed because the spec side is analyzed from the PR diff, not by walking a spec directory |
| `--docs-repo` / `--docs-path` | Test case repo as local path, `owner/repo`, or GitHub URL |
| `--docs-branch BRANCH` | Branch/ref to read from a remote test case repo |
| `--docs-subdir path/inside/repo` | Repo-relative folder that contains the `.adoc` files |
| `--repo-cache-dir ./repo_cache` | Temp clone directory for remote repos |
| `--config path.yaml` | Config file path (default: `workflow_config.yaml`) |
| `--dry-run` | Write report locally, skip GitHub comment and report commit |

---

## Output

### Full Markdown report

Saved as `GAP_REPORT_PR_{numbers}_{date}.md` locally (always) and committed to the `reports/auto` branch of the test case repo (unless `--dry-run`).

Structure:

```
# Matter / CHIP Spec — Test Plan Gap Analysis

| N Spec Changes | N Clusters Need New TCs | N Clusters Need TC Updates | N Needs Review |
|---|---|---|---|

## PR-by-PR Gap Analysis
| PR # | Cluster | Spec Change | Affected TCs | Action Required |

## Action Summary (priority-sorted)
| Priority | Cluster | Action |
| HIGH     | MediaFileManagement | Author 5 new TCs... |
| MEDIUM   | OccupancySensing    | Promote provisional steps... |
| LOW      | NETIM               | Rename ClientNumber → ClientIndex... |

## No Action Required
- OnOff (#12681): Already covered by TC-MCORE.OO-1.x
```

### GitHub PR comment

A condensed version (stats + action summary + link to full report) is posted on each analysed spec PR. In multi-PR mode each PR gets its own comment showing only its relevant entries.

---

## GitHub Actions

The workflow in `.github/workflows/gap_analysis.yml` fires on two events:

**Automatic** — triggers when a PR closes on any branch. The analyzer uses the PR's actual base repo and base branch.

**Manual** — go to Actions → Test Plan Gap Analysis → Run workflow. Enter PR numbers plus the spec repo, docs repo, optional branch, and optional docs subfolder.

### Required GitHub secrets and variables

Configure these in the repo's Settings → Secrets and variables:

| Name | Type | Value |
|---|---|---|
| `ANTHROPIC_API_KEY` | Secret | Your Anthropic API key |
| `SPEC_GITHUB_TOKEN` | Secret | Optional PAT/App token for private spec repo read/comment access |
| `DOCS_GITHUB_TOKEN` | Secret | Optional PAT/App token for private docs repo clone/push access |
| `GITHUB_TOKEN` | Automatic | Shared fallback if one token can access both repos |
| `DOCS_REPO_NAME` | Variable | Optional default docs repo, e.g. `your-org/matter-test-cases` |
| `DOCS_REPO_BRANCH` | Variable | Optional default docs branch, e.g. `main` |
| `DOCS_REPO_SUBDIR` | Variable | Optional default docs subfolder, e.g. `src/tests` |
| `SPEC_REPO_NAME` | Variable | Optional default spec repo for manual dispatch |

The ChromaDB cache is reused across runs. `gap_analyzer.py` now scopes each collection by docs repo, branch, and subfolder, so different repos do not overwrite each other's embeddings.

If both repos are private and different credentials are required, add both secrets:

- `SPEC_GITHUB_TOKEN` for spec PR fetch/comment access
- `DOCS_GITHUB_TOKEN` for test case repo clone/push access

If one token can access both, you can keep using only `GITHUB_TOKEN`.

---

## Updating the index

`gap_analyzer.py` now keeps the vector index in sync automatically for the selected docs repo, branch, and subfolder. `vector_index.py` remains useful when you want to inspect or manually refresh a local checkout:

**Test cases changed** — run incremental update on just the changed files:
```bash
python vector_index.py ./matter-test-cases \
  --files clusters/on_off/on_off.adoc clusters/fabric_sync/fabric_synchronization.adoc
```

**New test case file added** — same as above, pass the new file path.

**Test case file deleted** — the index is updated automatically; deleted files are removed by file path.

**Full rebuild** (if things seem off):
```bash
python vector_index.py ./matter-test-cases
```

**Inspect how a file was chunked** (useful for debugging retrieval):
```bash
python vector_index.py ./matter-test-cases --inspect fabric_synchronization.adoc
```

---

## Configuration

Edit `workflow_config.yaml` to tune behaviour. Key parameters:

```yaml
retrieval:
  similarity_threshold: 0.72   # Raise if getting irrelevant vector matches
  keyword_min_matches: 2       # Raise if keyword search is too permissive

llm:
  model: "claude-haiku-4-5-20251001"   # Free plan model
  diff_truncation_tokens: 8000         # Raise for very large PRs

cluster_owners:
  "OnOff": ["@lighting-team"]          # Add your team mappings here
  "default": ["@test-plan-maintainers"]
```

See `workflow_config.yaml` for the full list with descriptions.

---

## Interpreting the report

### Priority levels

| Priority | Meaning |
|---|---|
| HIGH | Cert-blocking — new cluster, direct behavioral change, or new device type. Act before the next certification cycle. |
| MEDIUM | Conformance adjustments, attribute additions, feature flag changes. Should be addressed but not immediately blocking. |
| LOW | Naming/identifier changes, provisional flag updates, reference fixes. Low effort, do in batch. |

### Action types

| Type | Meaning |
|---|---|
| `update` | An existing test case needs new or modified steps |
| `create` | A new test case file must be authored from scratch |
| `audit` | Human judgement required — scope of impact is ambiguous |

### ⚠️ Verify flag

Entries marked ⚠️ Verify had low retrieval confidence or failed the automated self-review. The analysis is included but should be confirmed before acting on it. Common causes: new cluster not yet in the test case repo, unusual file structure, or a spec change too subtle for the diff alone to convey.

---

## Troubleshooting

**"No chunks produced" during indexing**  
The test case files may use a non-standard structure. Run `--inspect` on a specific file to see how it's being chunked:
```bash
python vector_index.py ./matter-test-cases --inspect clusters/on_off/on_off.adoc
```
Check that the file has `:picsCode:` defined and uses `====` level headers for individual test cases.

**All entries are `already_covered`**  
The vector search may be matching the wrong sections. Lower `similarity_threshold` in `workflow_config.yaml` or check whether `keyword_min_matches` is filtering out all Tier 1 grep results.

**Decomposer returns empty task list**  
The PR diff may be too large and got truncated to nothing meaningful, or the PR is purely editorial (comments, whitespace). Check the log output. Raise `diff_truncation_tokens` if needed.

**Rate limiting from Claude API**  
The free plan has per-minute rate limits. For large batches of PRs (10+), runs may slow down due to backoff. Consider adding a small `time.sleep(1)` between tasks in `run_batch` if hitting limits frequently, or upgrade to a paid plan.

**Embedding model download fails**  
The first run downloads `nomic-ai/nomic-embed-text-v1.5` (~274 MB) from HuggingFace. If you're in an air-gapped environment, pre-download with:
```bash
python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('nomic-ai/nomic-embed-text-v1.5', trust_remote_code=True)"
```
Then copy the HuggingFace cache to the target environment.
