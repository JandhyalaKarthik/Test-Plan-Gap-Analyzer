"""
Matter Test Spec — Gap Analysis Orchestrator

Reads the spec PR diff, decomposes it into analysis tasks, retrieves relevant
existing test case content, reasons about coverage gaps, and produces a
structured gap report delivered as a Markdown file and a GitHub PR comment.

No .adoc files are modified. The workflow is purely analytical.

Usage:
    python gap_analyzer.py --pr 12681 --spec-repo org/matter-test-spec --docs-repo https://github.com/your-org/matter-test-cases
    python gap_analyzer.py --pr 12681 --spec-repo org/matter-test-spec --docs-repo https://github.com/your-org/matter-test-cases --docs-branch release/1.6 --docs-subdir src/tests --dry-run

Dependencies:
    pip install openai chromadb tiktoken PyGithub PyYAML gitpython sentence-transformers tqdm

LLM: configured via workflow_config.yaml (or GAP_LLM_MODEL override).
    The same configured model is used for decomposition, gap reasoning,
    self-review, and vector tag extraction.

Embeddings: nomic-ai/nomic-embed-text-v1.5 via sentence-transformers.
    Runs fully locally — no OpenAI key or cost required.
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import hashlib
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from openai import OpenAI, RateLimitError as LLMRateLimitError, APIError as LLMAPIError
import yaml
from github import Auth, Github, GithubException
from tqdm.auto import tqdm

# vector_index.py must be on the path (lives alongside this file)
from vector_index import VectorIndex, should_index_file

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

HEADER_MAP_PATH  = Path("index/header_map.json")

PRIORITY_ORDER   = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}

VALID_TASK_TYPES    = {"update", "create", "audit", "no_action"}
VALID_PRIORITIES    = {"HIGH", "MEDIUM", "LOW"}
VALID_VERDICTS      = {"already_covered", "partial_gap", "full_gap", "new_tc_needed"}
VALID_TERM_STATES   = {"gap_identified", "already_covered", "no_existing_coverage", "analysis_failed"}
GITHUB_REPO_RE      = re.compile(
    r"^(?:https?://github\.com/|git@github\.com:)?(?P<owner>[^/\s]+)/(?P<repo>[^/\s]+?)(?:\.git)?/?$"
)
WORD_RE             = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{2,}")
STOPWORDS           = {
    "the", "and", "for", "with", "from", "that", "this", "into", "onto", "when",
    "where", "what", "which", "while", "have", "has", "had", "were", "been", "being",
    "are", "was", "not", "but", "all", "any", "per", "its", "their", "your", "our",
    "after", "before", "under", "over", "through", "change", "changes", "update",
    "updated", "fix", "fixed", "draft", "local", "spec", "specification", "protocol",
    "matter", "pull", "request", "cluster", "attribute", "test", "plan", "plans",
}


# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------

def load_config(path: str = "workflow_config.yaml") -> dict:
    """Loads workflow_config.yaml, returning sensible defaults if file missing."""
    defaults = {
        "retrieval":  {"similarity_threshold": 0.72, "max_vector_candidates": 5, "keyword_min_matches": 2},
        "analysis":   {"self_review_sample_size": 3, "self_review_min_entries": 8,
                       "high_priority_ratio_warning": 0.80, "decomposer_max_retries": 2},
        "llm":        {"model": "meta-llama/llama-3.3-70b-instruct:free", "decomposer_max_tokens": 4096,
                       "reasoner_max_tokens": 2048, "diff_truncation_tokens": 8000},
        "report":     {"report_filename_prefix": "GAP_REPORT_PR", "reports_branch": "reports/auto",
                       "post_github_comment": True, "commit_full_report": True},
        "cluster_owners": {"default": ["@test-plan-maintainers"]},
    }
    try:
        with open(path) as f:
            user_cfg = yaml.safe_load(f) or {}
        if "area_owners" in user_cfg and "cluster_owners" not in user_cfg:
            user_cfg["cluster_owners"] = user_cfg["area_owners"]
        # Deep merge: user values override defaults
        for section, values in user_cfg.items():
            if section in defaults and isinstance(defaults[section], dict):
                defaults[section].update(values)
            else:
                defaults[section] = values
    except FileNotFoundError:
        logger.warning("workflow_config.yaml not found — using defaults.")

    env_model = os.environ.get("GAP_LLM_MODEL", "").strip()
    if env_model:
        defaults["llm"]["model"] = env_model
    return defaults


def resolve_openrouter_api_key(explicit_key: Optional[str] = None) -> str:
    """Resolves the OpenRouter API key from CLI arg or environment."""
    token = (explicit_key or "").strip() or os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not token:
        raise RuntimeError(
            "Missing OpenRouter API key. Pass --openrouter-api-key or set OPENROUTER_API_KEY."
        )
    return token


def progress_bar(iterable, *, total: Optional[int] = None, desc: str, unit: str):
    """Shared tqdm configuration for long-running orchestration phases."""
    return tqdm(
        iterable,
        total=total,
        desc=desc,
        unit=unit,
        dynamic_ncols=True,
    )


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class Task:
    task_id:             str
    type:                str               # update | create | audit | no_action
    cluster_area:        str
    tc_id:               Optional[str]     # retained for display/audit only — not used for retrieval
    target_description:  str
    search_hints:        list[str]
    spec_change_summary: str
    action_required:     str
    priority:            str               # HIGH | MEDIUM | LOW
    confidence:          str               # high | medium | low
    review_flag:         bool = False


@dataclass
class Candidate:
    file_path:            str
    raw_header_text:      str
    resolved_header_text: str
    tc_id:                Optional[str]
    similarity_score:     float
    raw_text:             str


@dataclass
class RetrievalResult:
    task_id:         str
    retrieval_tier:  int               # 1=keyword, 2=vector, 3=none
    retrieval_method: str
    coverage_status: str               # existing_coverage_found | no_existing_coverage | uncertain_coverage
    candidates:      list[Candidate] = field(default_factory=list)


@dataclass
class GapEntry:
    gap_id:           str
    task_id:          str
    source_pr:        str
    cluster_area:     str
    spec_change:      str
    affected_tcs:     list[str]
    coverage_verdict: str              # already_covered|partial_gap|full_gap|new_tc_needed
    gap_details:      str
    action_type:      str              # update | create | audit | verify
    action_required:  str
    priority:         str              # HIGH | MEDIUM | LOW
    source_file:      Optional[str]
    source_section:   Optional[str]
    match_confidence: str
    review_flag:      bool
    terminal_state:   str              # gap_identified|already_covered|no_existing_coverage|analysis_failed
    self_review_notes: Optional[str] = None


@dataclass
class PreparedRepo:
    raw_input: str
    repo_name: Optional[str]
    repo_root: Path
    content_root: Path
    branch: str
    subdir: str
    is_git_repo: bool
    temp_dir: Optional[tempfile.TemporaryDirectory] = None

    def cleanup(self) -> None:
        if self.temp_dir is not None:
            self.temp_dir.cleanup()
            self.temp_dir = None


def normalize_repo_subdir(subdir: Optional[str]) -> str:
    """Normalizes a repo-relative subdirectory and rejects path escapes."""
    raw = (subdir or ".").strip()
    if not raw or raw == ".":
        return "."

    candidate = Path(raw)
    if candidate.is_absolute():
        raise ValueError(f"Subdirectory must be repo-relative, got absolute path '{subdir}'.")

    normalized = candidate.as_posix().strip("/")
    if normalized in {"", "."}:
        return "."
    if normalized == ".." or normalized.startswith("../") or "/../" in normalized:
        raise ValueError(f"Subdirectory '{subdir}' escapes the repository root.")
    return normalized


def parse_github_repo_name(value: str) -> str:
    """
    Accepts:
      - owner/repo
      - https://github.com/owner/repo
      - git@github.com:owner/repo.git
    """
    match = GITHUB_REPO_RE.match(value.strip())
    if not match:
        raise ValueError(
            f"Could not parse GitHub repository from '{value}'. Use owner/repo or a GitHub URL."
        )
    repo = match.group("repo")
    if repo.endswith(".git"):
        repo = repo[:-4]
    return f"{match.group('owner')}/{repo}"


def is_git_repo(path: Path) -> bool:
    result = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--is-inside-work-tree"],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def infer_repo_name_from_local_checkout(path: Path) -> Optional[str]:
    """Best-effort owner/repo inference from a local git checkout's origin URL."""
    if not is_git_repo(path):
        return None

    result = subprocess.run(
        ["git", "-C", str(path), "config", "--get", "remote.origin.url"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None

    origin = result.stdout.strip()
    if not origin:
        return None

    try:
        return parse_github_repo_name(origin)
    except ValueError:
        return None


def detect_checked_out_branch(path: Path) -> str:
    """Returns the checked-out branch name when possible, else 'HEAD'."""
    if not is_git_repo(path):
        return "filesystem"

    result = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True,
        text=True,
    )
    branch = result.stdout.strip()
    return branch or "HEAD"


def get_spec_github_token() -> str:
    """Token used for spec-repo GitHub API access and PR comments."""
    token = (
        os.environ.get("SPEC_GITHUB_TOKEN", "").strip()
        or os.environ.get("GITHUB_TOKEN", "").strip()
    )
    if not token:
        raise RuntimeError("Missing GitHub token. Set SPEC_GITHUB_TOKEN or GITHUB_TOKEN.")
    return token


def get_docs_github_token(required: bool = False) -> str:
    """Token used for docs-repo clone and push operations."""
    token = (
        os.environ.get("DOCS_GITHUB_TOKEN", "").strip()
        or os.environ.get("GITHUB_TOKEN", "").strip()
    )
    if required and not token:
        raise RuntimeError("Missing docs GitHub token. Set DOCS_GITHUB_TOKEN or GITHUB_TOKEN.")
    return token


def make_authed_clone_url(repo_name: str, token: Optional[str] = None) -> str:
    """
    Builds an HTTPS clone URL.
    If a token is present, use it so private repos can be cloned or pushed.
    """
    token = (token or "").strip()
    if token:
        return f"https://x-access-token:{quote(token, safe='')}@github.com/{repo_name}.git"
    return f"https://github.com/{repo_name}.git"


def resolve_repo_content_root(repo_root: Path, subdir: str) -> Path:
    """Validates and returns the repo content root used for .adoc discovery."""
    content_root = repo_root if subdir == "." else (repo_root / subdir)
    resolved_root = repo_root.resolve()
    resolved_content = content_root.resolve()
    try:
        resolved_content.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError(f"Subdirectory '{subdir}' escapes the repository root.") from exc
    if not resolved_content.exists():
        raise FileNotFoundError(
            f"Requested subdirectory '{subdir}' does not exist under {repo_root}."
        )
    if not resolved_content.is_dir():
        raise NotADirectoryError(
            f"Requested subdirectory '{subdir}' is not a directory under {repo_root}."
        )
    return resolved_content


def prepare_repo(
    repo_input: str,
    branch: Optional[str],
    subdir: Optional[str],
    clone_root: str,
    label: str,
) -> PreparedRepo:
    """
    Resolves a repo source into a local filesystem tree.

    repo_input may be:
      - a local path
      - owner/repo
      - a GitHub HTTPS/SSH URL
    """
    raw = repo_input.strip()
    if not raw:
        raise ValueError(f"{label} repo input is empty.")

    normalized_subdir = normalize_repo_subdir(subdir)
    candidate_path = Path(raw).expanduser()

    if candidate_path.exists():
        repo_root = candidate_path.resolve()
        repo_name = infer_repo_name_from_local_checkout(repo_root)
        repo_is_git = is_git_repo(repo_root)

        if branch and repo_is_git:
            cache_root = Path(clone_root).expanduser()
            cache_root.mkdir(parents=True, exist_ok=True)
            temp_dir = tempfile.TemporaryDirectory(prefix=f"{label}_repo_", dir=cache_root)
            clone_root_path = Path(temp_dir.name)
            try:
                subprocess.run(
                    ["git", "clone", "--single-branch", "--branch", branch, str(repo_root), str(clone_root_path)],
                    check=True,
                    capture_output=True,
                    text=True,
                )
            except subprocess.CalledProcessError as exc:
                temp_dir.cleanup()
                stderr = (exc.stderr or "").strip() or "unknown git clone error"
                raise RuntimeError(
                    f"Failed to clone local {label} repo '{repo_root}' at branch '{branch}': {stderr}"
                ) from exc

            content_root = resolve_repo_content_root(clone_root_path, normalized_subdir)
            return PreparedRepo(
                raw_input=raw,
                repo_name=repo_name,
                repo_root=clone_root_path,
                content_root=content_root,
                branch=branch,
                subdir=normalized_subdir,
                is_git_repo=True,
                temp_dir=temp_dir,
            )

        content_root = resolve_repo_content_root(repo_root, normalized_subdir)
        effective_branch = branch or detect_checked_out_branch(repo_root)
        return PreparedRepo(
            raw_input=raw,
            repo_name=repo_name,
            repo_root=repo_root,
            content_root=content_root,
            branch=effective_branch,
            subdir=normalized_subdir,
            is_git_repo=repo_is_git,
        )

    repo_name = parse_github_repo_name(raw)
    cache_root = Path(clone_root).expanduser()
    cache_root.mkdir(parents=True, exist_ok=True)
    temp_dir = tempfile.TemporaryDirectory(prefix=f"{label}_repo_", dir=cache_root)
    temp_root = Path(temp_dir.name)
    repo_root = temp_root / "repo"
    docs_token = get_docs_github_token(required=False) if label == "docs" else ""

    clone_cmd_prefix = ["git", "clone", "--depth", "1", "--single-branch"]
    if branch:
        clone_cmd_prefix.extend(["--branch", branch])

    clone_urls: list[str] = []
    if docs_token:
        clone_urls.append(make_authed_clone_url(repo_name, docs_token))
    clone_urls.append(make_authed_clone_url(repo_name))

    clone_errors: list[str] = []
    for clone_url in dict.fromkeys(clone_urls):
        shutil.rmtree(repo_root, ignore_errors=True)
        clone_cmd = [*clone_cmd_prefix, clone_url, str(repo_root)]
        try:
            subprocess.run(clone_cmd, check=True, capture_output=True, text=True)
            break
        except subprocess.CalledProcessError as exc:
            stderr = (exc.stderr or "").strip() or "unknown git clone error"
            clone_errors.append(stderr)
    else:
        temp_dir.cleanup()
        error_detail = clone_errors[-1] if clone_errors else "unknown git clone error"
        raise RuntimeError(
            f"Failed to clone {label} repo '{repo_name}'"
            + (f" at branch '{branch}'" if branch else "")
            + f": {error_detail}"
        )

    content_root = resolve_repo_content_root(repo_root, normalized_subdir)
    effective_branch = branch or detect_checked_out_branch(repo_root)
    return PreparedRepo(
        raw_input=raw,
        repo_name=repo_name,
        repo_root=repo_root,
        content_root=content_root,
        branch=effective_branch,
        subdir=normalized_subdir,
        is_git_repo=True,
        temp_dir=temp_dir,
    )


def normalize_spec_repo_name(spec_repo_input: str) -> str:
    """Accepts a local git checkout, owner/repo, or GitHub URL for the spec repo."""
    candidate_path = Path(spec_repo_input).expanduser()
    if candidate_path.exists():
        repo_name = infer_repo_name_from_local_checkout(candidate_path.resolve())
        if repo_name:
            return repo_name
        raise ValueError(
            f"Local spec repo '{spec_repo_input}' has no GitHub origin remote; pass owner/repo or a GitHub URL."
        )
    return parse_github_repo_name(spec_repo_input)


def repo_scope_id(prepared_repo: PreparedRepo) -> str:
    """Stable scope key for per-repo/per-branch/per-subdir cached state."""
    seed = "::".join(
        [
            prepared_repo.repo_name or prepared_repo.repo_root.resolve().as_posix(),
            prepared_repo.branch,
            prepared_repo.subdir,
        ]
    )
    return hashlib.sha1(seed.encode("utf-8")).hexdigest()[:12]


def collect_adoc_files(root: Path, file_ext: str = ".adoc") -> dict[str, str]:
    """Reads all .adoc files under root keyed by repo-relative path."""
    adoc_files: dict[str, str] = {}
    for path in root.rglob(f"*{file_ext}"):
        adoc_files[str(path.relative_to(root))] = path.read_text(
            encoding="utf-8",
            errors="replace",
        )
    return adoc_files


def sync_vector_index(
    index: VectorIndex,
    docs_root: Path,
    persist_directory: str,
    scope_id: str,
) -> None:
    """
    Keeps the vector index in sync with the selected repo/branch/subdir by
    tracking per-file content hashes.
    """
    adoc_files = collect_adoc_files(docs_root)
    if not adoc_files:
        raise RuntimeError(f"No .adoc files found under {docs_root}.")

    manifest_dir = Path(persist_directory) / "manifests"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = manifest_dir / f"{scope_id}.json"

    current_manifest = {
        rel_path: hashlib.sha1(content.encode("utf-8")).hexdigest()
        for rel_path, content in adoc_files.items()
    }

    previous_manifest: dict[str, str] = {}
    if manifest_path.exists():
        try:
            previous_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            logger.warning("Ignoring malformed index manifest at %s.", manifest_path)

    if index.count() == 0 or not previous_manifest:
        logger.info("Building vector index from scratch for %s...", docs_root)
        index.build_full_index(adoc_files)
    else:
        changed_files = {
            rel_path: adoc_files[rel_path]
            for rel_path, digest in current_manifest.items()
            if previous_manifest.get(rel_path) != digest
        }
        deleted_files = sorted(set(previous_manifest) - set(current_manifest))

        if changed_files:
            logger.info("Updating vector index for %d changed file(s)...", len(changed_files))
            index.update_files(changed_files)
        if deleted_files:
            logger.info("Removing %d deleted file(s) from vector index...", len(deleted_files))
            for rel_path in deleted_files:
                index.remove_file(rel_path)
        if not changed_files and not deleted_files:
            logger.info("Vector index already up to date for %s.", docs_root)

    manifest_path.write_text(json.dumps(current_manifest, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Phase 0 — Header map (lightweight, runs every time)
# ---------------------------------------------------------------------------

def build_header_map(docs_repo_local_path: str) -> dict:
    """
    Walks the local .adoc test case repo clone and builds a header map used
    by Tier 1 (keyword) retrieval to locate the best section within a matched file.

    Returns {"headers": [...]} where each entry has file, raw_header_text,
    header_text (resolved), header_level, line_number, and cluster_hint.
    """
    from vector_index import resolve_adoc_attributes
    ADOC_HEADER_RE = re.compile(r"^(=+)\s+(.+)$", re.MULTILINE)

    headers: list[dict] = []
    root = Path(docs_repo_local_path)
    adoc_paths = sorted(root.rglob("*.adoc"))

    for adoc_path in progress_bar(
        adoc_paths,
        total=len(adoc_paths),
        desc="Building header map",
        unit="file",
    ):
        rel = str(adoc_path.relative_to(root))
        try:
            raw = adoc_path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            logger.warning("Could not read %s: %s", rel, e)
            continue

        _, attr_dict = resolve_adoc_attributes(raw)

        # Skip non-test-case files (licensing, summaries, etc.)
        should_index, skip_reason = should_index_file(rel, raw)
        if not should_index:
            logger.debug("Header map: skipping %s — %s", rel, skip_reason)
            continue

        cluster_hint = adoc_path.stem

        for m in ADOC_HEADER_RE.finditer(raw):
            raw_header  = m.group(2).strip()
            level       = len(m.group(1))
            line_number = raw[:m.start()].count("\n") + 1

            resolved_header = raw_header
            for k, v in attr_dict.items():
                resolved_header = resolved_header.replace(f"{{{k}}}", v)

            headers.append({
                "file":            rel,
                "raw_header_text": raw_header,
                "header_text":     resolved_header,
                "header_level":    level,
                "line_number":     line_number,
                "cluster_hint":    cluster_hint,
            })

    logger.info("Header map: %d headers across %d files.",
                len(headers), len({h["file"] for h in headers}))
    return {"headers": headers}


# ---------------------------------------------------------------------------
# Phase 1 — Trigger & pre-flight
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Phase 2 — Task decomposition
# ---------------------------------------------------------------------------

def fetch_pr_packet(
    gh: Github,
    spec_repo_name: str,
    pr_number: int,
    diff_token_limit: int,
    expected_base_branch: Optional[str] = None,
) -> dict:
    """
    Fetches PR metadata and diff from GitHub. Truncates the diff to
    diff_token_limit tokens (approximate, by character count) to stay
    within the decomposer's context window.
    """
    repo = gh.get_repo(spec_repo_name)
    pr   = repo.get_pull(pr_number)

    if expected_base_branch and pr.base.ref != expected_base_branch:
        raise ValueError(
            f"PR #{pr_number} targets base branch '{pr.base.ref}', not '{expected_base_branch}'."
        )

    commit_messages = [c.commit.message.split("\n")[0] for c in pr.get_commits()]

    diff_parts: list[str] = []
    char_budget = diff_token_limit * 4   # rough chars-per-token estimate

    for f in pr.get_files():
        patch = getattr(f, "patch", "") or ""
        header = f"--- {f.filename} ({f.status}) ---\n"
        block  = header + patch + "\n"
        if len("\n".join(diff_parts)) + len(block) > char_budget:
            diff_parts.append(f"[diff truncated — {f.filename} and subsequent files omitted]")
            break
        diff_parts.append(block)

    return {
        "pr_number":       pr.number,
        "pr_title":        pr.title,
        "pr_description":  pr.body or "",
        "commit_messages": commit_messages,
        "diff":            "\n".join(diff_parts),
        "html_url":        pr.html_url,
        "base_branch":     pr.base.ref,
    }


def extract_changed_files(diff: str) -> list[str]:
    """Parses synthetic diff headers emitted by fetch_pr_packet()."""
    files: list[str] = []
    for line in diff.splitlines():
        if line.startswith("--- ") and " (" in line:
            files.append(line[4:].split(" (", 1)[0].strip())
    return files


def summarize_text(text: str, limit: int = 220) -> str:
    clean = re.sub(r"\s+", " ", text or "").strip()
    if len(clean) <= limit:
        return clean
    return clean[: limit - 3].rstrip() + "..."


def humanize_identifier(value: str) -> str:
    text = re.sub(r"[_\-]+", " ", value).strip()
    if not text:
        return "Unknown"
    return " ".join(part.capitalize() for part in text.split())


def derive_cluster_area(packet: dict, changed_files: list[str]) -> str:
    """Best-effort area name for heuristic fallback when the LLM is unavailable."""
    if changed_files:
        first = Path(changed_files[0])
        meaningful_dirs = [
            part for part in first.parts[:-1]
            if part.lower() not in {"src", "docs", "doc", "spec", "specs", "clusters", "cluster"}
        ]
        if meaningful_dirs:
            return humanize_identifier(meaningful_dirs[-1])
        if first.stem:
            return humanize_identifier(first.stem)

    title = packet.get("pr_title", "")
    for match in re.finditer(r"\b([A-Z][A-Za-z0-9]+)\b", title):
        token = match.group(1)
        if token.lower() not in STOPWORDS:
            return token
    return "Unknown"


def extract_search_hints(packet: dict, changed_files: list[str], cluster_area: str) -> list[str]:
    """Pulls a few search terms from the title, description, and changed filenames."""
    candidates: list[str] = []

    if cluster_area and cluster_area != "Unknown":
        candidates.extend(re.findall(r"[A-Za-z0-9]+", cluster_area))

    for path in changed_files[:3]:
        p = Path(path)
        for token in (p.stem, p.parent.name):
            if token:
                candidates.extend(re.split(r"[_\-.]+", token))

    source_text = " ".join([
        packet.get("pr_title", ""),
        packet.get("pr_description", ""),
        " ".join(packet.get("commit_messages", [])),
    ])
    candidates.extend(WORD_RE.findall(source_text))

    hints: list[str] = []
    seen: set[str] = set()
    for raw in candidates:
        token = raw.strip().lower()
        if len(token) < 3 or token in STOPWORDS or token.isdigit():
            continue
        if token not in seen:
            seen.add(token)
            hints.append(token)
        if len(hints) >= 5:
            break

    return hints or ["spec_change"]


def heuristic_decompose_pr(packet: dict) -> list[Task]:
    """
    Conservative decomposition fallback used when the LLM is unavailable.

    Produces a small number of low-confidence tasks so the rest of the pipeline
    can still retrieve potentially relevant test cases and generate a report.
    """
    changed_files = extract_changed_files(packet.get("diff", ""))
    title = packet.get("pr_title", "")
    description = packet.get("pr_description", "")
    cluster_area = derive_cluster_area(packet, changed_files)
    search_hints = extract_search_hints(packet, changed_files, cluster_area)

    editorial_markers = ("anchor", "xref", "link", "format", "spelling", "typo")
    create_markers = ("new", "add", "introduce", "initial draft", "first draft")
    update_markers = ("update", "errata", "fix", "correct", "conformance", "attestation")
    combined_text = f"{title} {description}".lower()

    if any(marker in combined_text for marker in editorial_markers):
        task_type = "no_action"
        priority = "LOW"
    elif any(marker in combined_text for marker in create_markers):
        task_type = "create"
        priority = "MEDIUM"
    elif any(marker in combined_text for marker in update_markers):
        task_type = "update"
        priority = "MEDIUM"
    else:
        task_type = "audit"
        priority = "MEDIUM"

    if task_type == "no_action":
        logger.info(
            "Heuristic fallback classified PR #%s as no_action.",
            packet.get("pr_number"),
        )
        return []

    if re.search(r"\b(attestation|commission|security|authentication|credential)\b", combined_text):
        priority = "HIGH"

    summary_parts = [summarize_text(title)]
    if description.strip():
        summary_parts.append(summarize_text(description))
    if changed_files:
        summary_parts.append(f"Changed files: {', '.join(changed_files[:3])}")

    action_required = (
        "Heuristic fallback used because the LLM provider was unavailable. "
        "Manually review relevant test plans in the selected docs subtree and update or add coverage as needed."
    )
    if task_type == "create":
        action_required = (
            "Heuristic fallback suggests new or expanded coverage may be needed. "
            "Review the selected docs subtree and author or extend test cases for this behavior."
        )
    elif task_type == "update":
        action_required = (
            "Heuristic fallback suggests existing coverage may need updates. "
            "Review the selected docs subtree and revise matching test steps, PICS, or expected outcomes."
        )

    return [Task(
        task_id="heuristic_task_001",
        type=task_type,
        cluster_area=cluster_area,
        tc_id=None,
        target_description=f"Review {cluster_area} coverage for PR #{packet['pr_number']}.",
        search_hints=search_hints,
        spec_change_summary=" | ".join(part for part in summary_parts if part),
        action_required=action_required,
        priority=priority,
        confidence="low",
        review_flag=True,
    )]


def heuristic_reason_about_gap(task: Task, retrieval: RetrievalResult) -> dict:
    """
    Conservative reasoning fallback when the LLM is unavailable.

    Never claims "already_covered". It only points to likely relevant existing
    coverage or to missing coverage that needs human confirmation.
    """
    if retrieval.candidates:
        best = retrieval.candidates[0]
        search_space = " ".join([
            best.file_path,
            best.resolved_header_text,
            best.raw_text[:1200],
        ]).lower()
        overlaps = [hint for hint in task.search_hints if hint.lower() in search_space]
        overlap_summary = ", ".join(overlaps[:3]) if overlaps else "semantic retrieval only"

        return {
            "coverage_verdict": "partial_gap",
            "match_confidence": "low",
            "gap_details": (
                "Heuristic fallback was used because the LLM provider was unavailable. "
                f"Potentially relevant existing coverage was found in {best.file_path} "
                f"under '{best.resolved_header_text}' (matched: {overlap_summary}). "
                "Manual review is required before treating this as fully covered."
            ),
            "affected_tcs": [best.tc_id] if best.tc_id else [],
            "action_type": "update" if task.type in {"update", "create"} else "audit",
            "action_required": (
                f"Review {best.file_path}"
                + (f" ({best.tc_id})" if best.tc_id else "")
                + " and update or extend it to reflect the spec PR."
            ),
            "priority": task.priority,
        }

    coverage_verdict = "new_tc_needed" if task.type == "create" else "full_gap"
    action_type = "create" if task.type == "create" else "audit"
    return {
        "coverage_verdict": coverage_verdict,
        "match_confidence": "low",
        "gap_details": (
            "Heuristic fallback was used because the LLM provider was unavailable. "
            "No relevant indexed coverage was found in the selected docs subtree."
        ),
        "affected_tcs": [],
        "action_type": action_type,
        "action_required": (
            "Review a broader docs subtree or author new coverage if this spec change is test-impacting."
        ),
        "priority": task.priority,
    }



# ---------------------------------------------------------------------------
# Phase 2 — Task decomposition
# ---------------------------------------------------------------------------

DECOMPOSER_PROMPT = """\
You are a task decomposition agent for a Matter protocol test specification gap analysis system.

Given a PR diff and description from a spec repository, decompose the changes into a discrete,
flat list of documentation analysis tasks. Each task represents one thing that might need to
change in the test case (.adoc) repository.

Each task must be one of:
- "update": An existing test case likely needs to be updated to reflect this spec change.
- "create": This spec change introduces new behavior with no existing test case coverage.
- "audit": The change may affect existing test cases but requires human judgement to determine scope.
- "no_action": The change is internal (refactor, CI, tooling) with no test case impact.

For each task, extract:
- type: "update" | "create" | "audit" | "no_action"
- cluster_area: The cluster or feature area affected (e.g. "OnOff", "NetworkCommissioning")
- tc_id: The test case ID if explicitly mentioned (e.g. "TC-OO-1.1"), else null
- target_description: Human-readable description of what test case is affected or needs creating
- search_hints: 2-5 behavioral keywords or cluster names to locate relevant .adoc sections
  (do NOT use spec section numbers like "12.5.8.4" — those do not match test case IDs)
- spec_change_summary: What changed in the spec
- action_required: Precise description of what the test plan author must do in the .adoc repo
- priority: "HIGH" | "MEDIUM" | "LOW"
  HIGH = cert-blocking, direct behavioral change, new cluster/device type
  MEDIUM = conformance adjustments, attribute additions, feature flag changes
  LOW = naming/identifier changes, provisional flag updates, reference fixes
- confidence: "high" | "medium" | "low" — your confidence this is a real test case impact

Return ONLY a JSON array. No preamble. No markdown fences. No trailing text.

PR Title: {pr_title}
PR Description: {pr_description}
Commit Messages: {commit_messages}

Diff:
{diff}
"""


def call_llm_json(
    client: OpenAI,
    prompt: str,
    model: str,
    max_tokens: int,
    retries: int = 2,
) -> list | dict:
    """
    Calls the LLM via OpenRouter and parses the response as JSON.
    Strips markdown fences defensively. Retries on parse failure.
    """
    last_error = None
    for attempt in range(retries + 1):
        try:
            resp = client.chat.completions.create(
                model      = model,
                max_tokens = max_tokens,
                messages   = [{"role": "user", "content": prompt}],
            )
            raw  = resp.choices[0].message.content.strip()
            # Strip ```json ... ``` fences if present
            raw  = re.sub(r"^```(?:json)?\s*", "", raw)
            raw  = re.sub(r"\s*```$", "", raw)
            return json.loads(raw)
        except (json.JSONDecodeError, IndexError) as e:
            last_error = e
            logger.warning("JSON parse failed (attempt %d/%d): %s", attempt + 1, retries + 1, e)
            if attempt < retries:
                time.sleep(2 ** attempt)
        except LLMRateLimitError:
            last_error = RuntimeError("Rate limited by configured LLM provider.")
            wait = 2 ** (attempt + 2)
            logger.warning("Rate limited — waiting %ds.", wait)
            time.sleep(wait)
        except LLMAPIError as e:
            last_error = e
            logger.warning("API error (attempt %d/%d): %s", attempt + 1, retries + 1, e)
            if attempt < retries:
                time.sleep(2 ** attempt)
    raise RuntimeError(f"LLM call failed after {retries + 1} attempts: {last_error}")


TASK_REQUIRED_FIELDS = {
    "type", "cluster_area", "target_description", "search_hints",
    "spec_change_summary", "action_required", "priority", "confidence",
}
AREA_FIELD_ALIASES = ("cluster_area", "feature_area", "component_area", "domain_area")


def decompose_pr(packet: dict, config: dict,
                 client: OpenAI) -> list[Task]:
    """
    Phase 2: calls the decomposer LLM and returns a validated list of Tasks.
    """
    cfg    = config["llm"]
    prompt = DECOMPOSER_PROMPT.format(
        pr_title       = packet["pr_title"],
        pr_description = packet["pr_description"][:2000],
        commit_messages= " | ".join(packet["commit_messages"]),
        diff           = packet["diff"],
    )

    try:
        raw_tasks: list[dict] = call_llm_json(
            client, prompt, cfg["model"], cfg["decomposer_max_tokens"],
            retries=config["analysis"]["decomposer_max_retries"],
        )
    except Exception as exc:
        logger.warning(
            "LLM decomposition unavailable for PR #%s — using heuristic fallback: %s",
            packet.get("pr_number"),
            exc,
        )
        return heuristic_decompose_pr(packet)

    tasks: list[Task] = []
    no_action_log: list[dict] = []

    for i, raw in enumerate(raw_tasks):
        task_id = raw.get("task_id") or f"task_{i+1:03d}"

        # Schema validation — require all mandatory fields
        missing = TASK_REQUIRED_FIELDS - set(raw.keys())
        if not any(str(raw.get(field, "")).strip() for field in AREA_FIELD_ALIASES):
            missing.add("cluster_area")
        if missing:
            logger.warning("Task %s missing fields %s — skipping.", task_id, missing)
            continue

        task_type = raw["type"]
        if task_type not in VALID_TASK_TYPES:
            logger.warning("Task %s has unknown type '%s' — skipping.", task_id, task_type)
            continue

        # Filter no_action — log only
        if task_type == "no_action":
            area_value = next((raw.get(field) for field in AREA_FIELD_ALIASES if raw.get(field)), None)
            no_action_log.append({"task_id": task_id, "cluster_area": area_value,
                                   "reason": raw.get("spec_change_summary", "")})
            continue

        priority = raw.get("priority", "MEDIUM").upper()
        if priority not in VALID_PRIORITIES:
            priority = "MEDIUM"

        confidence = raw.get("confidence", "medium").lower()
        review_flag = confidence == "low"

        # Priority cross-check: create task with LOW priority is suspicious
        if task_type == "create" and priority == "LOW":
            logger.warning(
                "Task %s is type=create with priority=LOW — flagging for verify.", task_id
            )
            review_flag = True

        area_value = next((raw.get(field) for field in AREA_FIELD_ALIASES if raw.get(field)), None)
        task = Task(
            task_id            = task_id,
            type               = task_type,
            cluster_area       = area_value or "Unknown",
            tc_id              = raw.get("tc_id"),
            target_description = raw["target_description"],
            search_hints       = raw.get("search_hints", []),
            spec_change_summary= raw["spec_change_summary"],
            action_required    = raw["action_required"],
            priority           = priority,
            confidence         = confidence,
            review_flag        = review_flag,
        )

        tasks.append(task)

    logger.info(
        "Decomposition: %d actionable tasks, %d no_action filtered.",
        len(tasks), len(no_action_log),
    )
    return tasks


# ---------------------------------------------------------------------------
# Phase 3a — Retrieval
# ---------------------------------------------------------------------------

def grep_adoc_repo(docs_repo_path: str, search_hints: list[str],
                   min_matches: int, header_map: dict) -> list[Candidate]:
    """
    Tier 2: grep the local .adoc repo for search_hint terms.
    Scores each matching file by how many hints it contains, returns top-3.
    """
    file_scores: dict[str, int] = {}
    file_headers: dict[str, list[dict]] = {}

    # Resolve to absolute path so Path.relative_to() works regardless of how
    # docs_repo_path was passed (e.g. "./matter-test-cases" vs absolute)
    abs_repo = Path(docs_repo_path).resolve()

    # Build a quick file -> [header entries] lookup from the map
    for h in header_map.get("headers", []):
        file_headers.setdefault(h["file"], []).append(h)

    for hint in search_hints:
        if not hint.strip():
            continue
        try:
            result = subprocess.run(
                ["grep", "-ril", "--include=*.adoc", hint, str(abs_repo)],
                capture_output=True, text=True, timeout=10,
            )
            for line in result.stdout.strip().splitlines():
                try:
                    rel = str(Path(line).resolve().relative_to(abs_repo))
                    # Skip non-test-case files even if they match a keyword
                    abs_file = abs_repo / rel
                    if abs_file.exists():
                        content = abs_file.read_text(encoding="utf-8", errors="replace")
                        ok, _ = should_index_file(rel, content)
                        if not ok:
                            continue
                    file_scores[rel] = file_scores.get(rel, 0) + 1
                except ValueError:
                    pass   # path not under repo root — skip
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass   # grep unavailable or timed out — skip this hint

    # Keep files that matched at least min_matches hints
    qualified = [(f, s) for f, s in file_scores.items() if s >= min_matches]
    qualified.sort(key=lambda x: -x[1])

    candidates: list[Candidate] = []
    for file_path, _ in qualified[:3]:
        # Pick the most relevant header in this file — prefer ==== level TC headers
        file_hdrs  = file_headers.get(file_path, [])
        tc_hdrs    = [h for h in file_hdrs if h.get("tc_id")]
        level4_hdrs= [h for h in file_hdrs if h.get("header_level") == 4]
        best       = tc_hdrs[0] if tc_hdrs else (level4_hdrs[0] if level4_hdrs else (file_hdrs[0] if file_hdrs else None))
        if not best:
            continue

        try:
            full_text = (abs_repo / file_path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        # Extract the relevant section starting at the best header's line,
        # rather than passing the whole file from byte 0 (which starts at
        # the copyright block and file metadata — not the test steps).
        start_line = best.get("line_number", 1) - 1   # 0-indexed
        lines      = full_text.splitlines()
        section_lines = lines[start_line:start_line + 120]   # ~120 lines covers a full TC
        section_text  = "\n".join(section_lines)

        candidates.append(Candidate(
            file_path            = file_path,
            raw_header_text      = best["raw_header_text"],
            resolved_header_text = best["header_text"],
            tc_id                = best.get("tc_id"),
            similarity_score     = 0.0,   # grep hits don't carry a similarity score
            raw_text             = section_text[:3000],
        ))

    return candidates


def retrieve_for_task(
    task: Task,
    header_map: dict,
    vector_index: VectorIndex,
    docs_repo_path: str,
    config: dict,
) -> RetrievalResult:
    """
    Phase 3a: three-tier retrieval cascade.

    Tier 1 — Keyword/grep: searches the .adoc test case files for cluster
              names and behavioral keywords from search_hints. Fast and
              precise for well-named clusters.
    Tier 2 — Vector semantic search: embeds the task description and finds
              the closest test case chunks by meaning. Catches cases where
              cluster naming differs between spec and test case repo.
    Tier 3 — No match: either no TC exists (new cluster) or the search
              hints were too generic to find anything reliable.

    Note: there is no exact TC-ID tier. Spec PR IDs and test case IDs use
    different numbering schemes and never match directly.
    """
    threshold   = config["retrieval"]["similarity_threshold"]
    max_results = config["retrieval"]["max_vector_candidates"]
    min_keyword = config["retrieval"]["keyword_min_matches"]

    # --- Tier 1: Keyword / grep ---
    grep_candidates = grep_adoc_repo(docs_repo_path, task.search_hints, min_keyword, header_map)
    if grep_candidates:
        return RetrievalResult(
            task_id         = task.task_id,
            retrieval_tier  = 1,
            retrieval_method= "keyword",
            coverage_status = "existing_coverage_found",
            candidates      = grep_candidates,
        )

    # --- Tier 2: Vector semantic search ---
    query    = f"{task.target_description}. {task.spec_change_summary}"
    tag_hint = None
    for hint in task.search_hints:
        candidate_tag = hint.lower().replace(" ", "_").replace("-", "_")
        if len(candidate_tag) > 3:
            tag_hint = candidate_tag
            break

    try:
        vec_results = vector_index.search(
            query              = query,
            n_results          = max_results,
            cluster_tag_filter = tag_hint,
        )
    except Exception as e:
        logger.warning("Vector search failed for task %s: %s", task.task_id, e)
        vec_results = []

    strong = [r for r in vec_results if r["similarity_score"] >= threshold]

    if strong:
        candidates = [
            Candidate(
                file_path            = r["file_path"],
                raw_header_text      = r["raw_header_text"],
                resolved_header_text = r["resolved_header_text"],
                tc_id                = r.get("tc_id"),
                similarity_score     = r["similarity_score"],
                raw_text             = r["raw_text"],
            )
            for r in strong
        ]
        return RetrievalResult(
            task_id         = task.task_id,
            retrieval_tier  = 2,
            retrieval_method= "vector",
            coverage_status = "existing_coverage_found",
            candidates      = candidates,
        )

    # --- Tier 3: No match ---
    coverage_status = (
        "no_existing_coverage" if task.type == "create"
        else "uncertain_coverage"
    )
    if coverage_status == "uncertain_coverage":
        task.review_flag = True
    return RetrievalResult(
        task_id         = task.task_id,
        retrieval_tier  = 3,
        retrieval_method= "none",
        coverage_status = coverage_status,
        candidates      = [],
    )


# ---------------------------------------------------------------------------
# Phase 3b — Contextual reasoning
# ---------------------------------------------------------------------------

GAP_ANALYST_PROMPT = """\
You are a test plan gap analyst for the Matter protocol certification test suite.

A spec PR has made a change. Your job is to:
1. Determine whether the existing test case(s) below already cover the spec change.
2. If not, produce a precise description of what the test plan author needs to do.
3. Assign a priority and action type.

--- SPEC CHANGE ---
Cluster/Area: {cluster_area}
PR Summary: {spec_change_summary}
Required Action (from initial analysis): {action_required}

--- EXISTING TEST CASE CONTENT (from {file_path}) ---
Section: {resolved_header_text}

{raw_text}

--- INSTRUCTIONS ---
Analyse whether the existing test case already covers the spec change.
Be specific: name actual test steps, PICS conditions, table columns, or attribute names.

Respond ONLY in JSON with this exact structure (no preamble, no fences):
{{
  "coverage_verdict": "already_covered | partial_gap | full_gap | new_tc_needed",
  "match_confidence": "high | medium | low",
  "gap_details": "Precise description of what is missing or needs updating. Name specific steps/fields.",
  "affected_tcs": ["TC-XX-1.1"],
  "action_type": "update | create | audit | verify",
  "action_required": "Exact actionable instruction for the test plan author.",
  "priority": "HIGH | MEDIUM | LOW"
}}
"""

GAP_ANALYST_PROMPT_NO_COVERAGE = """\
You are a test plan gap analyst for the Matter protocol certification test suite.

A spec PR has introduced a new cluster or behavior. No existing test cases were found
in the test case repository for this cluster/area.

--- SPEC CHANGE ---
Cluster/Area: {cluster_area}
PR Summary: {spec_change_summary}
Required Action (from initial analysis): {action_required}

Based on the spec change described, produce a gap entry for the report.
Suggest a TC numbering scheme (TC-{{PICS_CODE}}-1.x starting at 1.1) if the cluster is new.

Respond ONLY in JSON with this exact structure (no preamble, no fences):
{{
  "coverage_verdict": "new_tc_needed",
  "match_confidence": "high",
  "gap_details": "No test cases exist for this cluster/behavior in the test case repository.",
  "affected_tcs": [],
  "action_type": "create",
  "action_required": "Precise list of test cases to author, with suggested TC IDs and scope.",
  "priority": "HIGH | MEDIUM | LOW"
}}
"""


def reason_about_gap(
    task: Task,
    retrieval: RetrievalResult,
    config: dict,
    client: OpenAI,
) -> dict:
    """
    Phase 3b: calls the configured LLM to analyse the coverage gap.
    Returns the parsed JSON reasoning output.

    Handles three retrieval outcomes:
      - no_existing_coverage: confirmed no TCs exist — use the no-coverage prompt
      - uncertain_coverage:   update task but nothing found — treat same as no coverage,
                              but flag it so the report marks it for human verification
      - existing_coverage_found: normal path — analyse the retrieved section
    """
    cfg = config["llm"]

    if retrieval.coverage_status in ("no_existing_coverage", "uncertain_coverage"):
        # For uncertain_coverage (update task, no match) we use the same "no existing
        # TC" prompt but the entry will carry review_flag=True from the task itself
        # (set in retrieve_for_task Tier 4) to signal lower confidence.
        prompt = GAP_ANALYST_PROMPT_NO_COVERAGE.format(
            cluster_area       = task.cluster_area,
            spec_change_summary= task.spec_change_summary,
            action_required    = task.action_required,
        )
    else:
        # Use the best candidate (first = highest score or exact match)
        best = retrieval.candidates[0]
        prompt = GAP_ANALYST_PROMPT.format(
            cluster_area         = task.cluster_area,
            spec_change_summary  = task.spec_change_summary,
            action_required      = task.action_required,
            file_path            = best.file_path,
            resolved_header_text = best.resolved_header_text,
            raw_text             = best.raw_text[:2000],
        )

    try:
        return call_llm_json(client, prompt, cfg["model"], cfg["reasoner_max_tokens"])
    except Exception as exc:
        logger.warning(
            "LLM gap reasoning unavailable for task %s — using heuristic fallback: %s",
            task.task_id,
            exc,
        )
        return heuristic_reason_about_gap(task, retrieval)


# ---------------------------------------------------------------------------
# Phase 4 — Gap entry assembly
# ---------------------------------------------------------------------------

def assemble_gap_entry(
    gap_id: str,
    task: Task,
    retrieval: RetrievalResult,
    reasoning: dict,
    pr_number: int,
) -> GapEntry:
    """
    Phase 4a: combines task, retrieval, and reasoning into a GapEntry.
    """
    verdict = reasoning.get("coverage_verdict", "full_gap")
    if verdict not in VALID_VERDICTS:
        verdict = "full_gap"

    match_confidence = reasoning.get("match_confidence", task.confidence)

    # Derive terminal state
    if retrieval.coverage_status == "no_existing_coverage":
        terminal_state = "no_existing_coverage"
    elif verdict == "already_covered":
        terminal_state = "already_covered"
    else:
        terminal_state = "gap_identified"

    # Inherit review_flag from task; also set if reasoning confidence is low
    review_flag = task.review_flag or (match_confidence == "low")

    source_file = retrieval.candidates[0].file_path if retrieval.candidates else None
    source_section = retrieval.candidates[0].resolved_header_text if retrieval.candidates else None

    return GapEntry(
        gap_id          = gap_id,
        task_id         = task.task_id,
        source_pr       = f"#{pr_number}",
        cluster_area    = task.cluster_area,
        spec_change     = task.spec_change_summary,
        affected_tcs    = reasoning.get("affected_tcs", []),
        coverage_verdict= verdict,
        gap_details     = reasoning.get("gap_details", ""),
        action_type     = reasoning.get("action_type", task.type),
        action_required = reasoning.get("action_required", task.action_required),
        priority        = reasoning.get("priority", task.priority),
        source_file     = source_file,
        source_section  = source_section,
        match_confidence= match_confidence,
        review_flag     = review_flag,
        terminal_state  = terminal_state,
    )


# ---------------------------------------------------------------------------
# Phase 5 — Report assembly & validation
# ---------------------------------------------------------------------------

def validate_completeness(tasks: list[Task], gap_entries: dict[str, GapEntry]) -> list[str]:
    """5a: every task must have a gap entry with a valid terminal state."""
    errors: list[str] = []
    for task in tasks:
        if task.task_id not in gap_entries:
            errors.append(f"Task {task.task_id} has no gap entry.")
        else:
            ts = gap_entries[task.task_id].terminal_state
            if ts not in VALID_TERM_STATES:
                errors.append(f"Task {task.task_id} has invalid terminal_state '{ts}'.")
    return errors


def check_priority_distribution(gap_entries: dict[str, GapEntry]) -> list[str]:
    """5b: sanity check the priority mix."""
    warnings: list[str] = []
    actionable = [e for e in gap_entries.values() if e.terminal_state != "already_covered"]
    if not actionable:
        return warnings

    high_count = sum(1 for e in actionable if e.priority == "HIGH")
    ratio      = high_count / len(actionable)

    if ratio > 0.80:
        warnings.append(
            f"Priority warning: {high_count}/{len(actionable)} entries are HIGH "
            f"({ratio:.0%}). Decomposer may be over-prioritising."
        )
    if all(e.terminal_state == "already_covered" for e in gap_entries.values()):
        warnings.append(
            "All entries are already_covered. Retrieval may have matched wrong sections."
        )
    return warnings


SELF_REVIEW_PROMPT = """\
You are a senior test plan reviewer for the Matter protocol certification suite.

Review the following gap analysis entry and verify it is:
1. Accurately describing a real gap (not already covered by the existing TC content shown)
2. Giving actionable, specific instructions (not vague)
3. Correctly prioritised

Gap Entry:
{gap_entry_json}

Existing TC Content Reviewed (excerpt):
{raw_text}

Respond ONLY in JSON (no preamble, no fences):
{{"accurate": true|false, "specific": true|false, "priority_correct": true|false, "notes": "..."}}
"""


def self_review_sample(
    gap_entries: dict[str, GapEntry],
    retrieval_map: dict[str, RetrievalResult],
    config: dict,
    client: OpenAI,
) -> None:
    """5c: LLM self-review on a random sample. Mutates review_flag in place."""
    cfg              = config["analysis"]
    min_entries      = cfg["self_review_min_entries"]
    sample_size      = cfg["self_review_sample_size"]
    llm_cfg          = config["llm"]

    actionable = [
        e for e in gap_entries.values()
        if e.terminal_state == "gap_identified"
    ]
    if len(actionable) < min_entries:
        logger.info("Self-review skipped (%d < %d entries).", len(actionable), min_entries)
        return

    sample = random.sample(actionable, min(sample_size, len(actionable)))
    logger.info("Self-reviewing %d sampled gap entries...", len(sample))

    for entry in progress_bar(
        sample,
        total=len(sample),
        desc="Self-reviewing entries",
        unit="entry",
    ):
        retrieval   = retrieval_map.get(entry.task_id)
        raw_excerpt = ""
        if retrieval and retrieval.candidates:
            raw_excerpt = retrieval.candidates[0].raw_text[:1000]

        prompt = SELF_REVIEW_PROMPT.format(
            gap_entry_json = json.dumps(asdict(entry), indent=2),
            raw_text       = raw_excerpt,
        )
        try:
            result = call_llm_json(client, prompt, llm_cfg["model"], 512, retries=1)
            failed_checks = [k for k in ("accurate", "specific", "priority_correct")
                             if not result.get(k, True)]
            if failed_checks:
                entry.review_flag       = True
                entry.self_review_notes = (
                    f"Self-review flagged: {', '.join(failed_checks)}. "
                    f"{result.get('notes', '')}"
                )
                logger.info("Self-review flagged entry %s: %s", entry.gap_id, failed_checks)
        except Exception as e:
            logger.warning("Self-review failed for %s: %s", entry.gap_id, e)


def compute_run_summary(
    gap_entries: dict[str, GapEntry],
    tasks: list[Task],
    pr_number: int,
) -> dict:
    """5d: compute statistics for the report header."""
    entries = list(gap_entries.values())
    # For new-cluster entries, the reasoner returns affected_tcs=[] because no TCs exist yet.
    # Count the entries themselves as the lower-bound TC count in that case.
    new_tc_count = 0
    for e in entries:
        if e.terminal_state in ("gap_identified", "no_existing_coverage") and e.action_type == "create":
            # Use len(affected_tcs) if the reasoner suggested specific TC IDs;
            # fall back to 1 per entry so the count is never 0 for real new-TC work.
            new_tc_count += max(len(e.affected_tcs), 1)
    # Count clusters needing updates (unique cluster_areas with non-covered actionable entries)
    update_clusters = {
        e.cluster_area for e in entries
        if e.terminal_state == "gap_identified" and e.action_type != "create"
    }
    return {
        "pr_number":                  pr_number,
        "total_spec_changes":         len(tasks),
        "gaps_identified":            sum(1 for e in entries if e.terminal_state == "gap_identified"),
        "already_covered":            sum(1 for e in entries if e.terminal_state == "already_covered"),
        "no_existing_coverage":       sum(1 for e in entries if e.terminal_state == "no_existing_coverage"),
        "analysis_failed":            sum(1 for e in entries if e.terminal_state == "analysis_failed"),
        "new_tcs_needed":             new_tc_count,
        "clusters_needing_updates":   len(update_clusters),
        "high_priority_count":        sum(1 for e in entries if e.priority == "HIGH"),
        "medium_priority_count":      sum(1 for e in entries if e.priority == "MEDIUM"),
        "low_priority_count":         sum(1 for e in entries if e.priority == "LOW"),
        "entries_flagged_for_verify": sum(1 for e in entries if e.review_flag),
    }


# ---------------------------------------------------------------------------
# Phase 6 — Report rendering & delivery
# ---------------------------------------------------------------------------

def _action_cell(entry: GapEntry) -> str:
    """Formats the Action Required column for the PR-by-PR table."""
    cell = entry.action_required
    if entry.review_flag:
        note = entry.self_review_notes or "Low-confidence analysis — verify before acting."
        cell = f"⚠️ **Verify** — {note}<br>{cell}"
    return cell


def recommendation_label(entry: GapEntry) -> str:
    """Human-readable action category for reports/comments."""
    if entry.terminal_state == "already_covered":
        return "Already Covered"
    if entry.terminal_state == "analysis_failed":
        return "Manual Review Needed"
    if entry.terminal_state == "no_existing_coverage":
        return "Current Test Case Docs Do Not Cover This PR"
    if entry.action_type == "update":
        return "Update Existing Test Case"
    if entry.action_type == "create":
        return "Create New Sub-Test Case" if entry.source_file else "Create New Test Case"
    if entry.action_type == "verify":
        return "Verify Recommendation"
    return "Review Existing Coverage"


def location_hint(entry: GapEntry, summary: dict) -> str:
    """Where the user should look or add coverage."""
    if entry.source_file and entry.source_section:
        return f"`{entry.source_file}` → `{entry.source_section}`"
    if entry.source_file:
        return f"`{entry.source_file}`"
    docs_subdir = summary.get("docs_subdir", ".")
    return f"No matching file found under selected docs subtree `{docs_subdir}`"


def detail_hint(entry: GapEntry) -> str:
    """Detailed gap explanation with a safe fallback."""
    detail = (entry.gap_details or "").strip()
    if detail:
        return detail
    if entry.terminal_state == "no_existing_coverage":
        return "No relevant indexed test-case coverage was found for this PR in the selected docs subtree."
    return "No additional analysis detail was captured."


def render_markdown_report(
    gap_entries: dict[str, GapEntry],
    summary: dict,
    packet: dict,
    partial_warning: Optional[str] = None,
    all_packets: Optional[list[dict]] = None,
) -> str:
    """
    Phase 6a/6b: renders the full Markdown gap report.
    Structure matches the Cert Gap Report format.
    all_packets: if provided (multi-PR mode), the PR-by-PR table groups entries by source PR.
    """
    date_str  = datetime.now(timezone.utc).strftime("%B %d, %Y")
    pr_numbers = summary.get("pr_numbers", [summary["pr_number"]])
    is_batch  = len(pr_numbers) > 1

    if is_batch:
        pr_list = ", ".join(f"#{n}" for n in pr_numbers)
        source_line = f"Spec Changes: PRs {pr_list}  |  {date_str}  |  Prepared by: Automation"
    else:
        pr_link     = f"[PR #{summary['pr_number']}: {packet['pr_title']}]({packet['html_url']})"
        source_line = f"Spec Changes: Source {pr_link}  |  {date_str}  |  Prepared by: Automation"

    lines: list[str] = []
    lines += [
        "# Matter / CHIP Spec — Test Plan Gap Analysis",
        "",
        source_line,
        "",
    ]

    if partial_warning:
        lines += [f"> ⚠️ **Partial analysis:** {partial_warning}", ""]

    new_tc_clusters = summary["no_existing_coverage"]
    update_clusters = summary["clusters_needing_updates"]
    verify_count    = summary["entries_flagged_for_verify"]
    lines += [
        f"| **{summary['total_spec_changes']}** Spec Changes Analyzed "
        f"| **{new_tc_clusters}** Clusters Need New TCs "
        f"({summary['new_tcs_needed']} total) "
        f"| **{update_clusters}** Clusters Need TC Updates "
        f"| **{verify_count}** Needs Review Only |",
        "| --- | --- | --- | --- |",
        "",
        f"**Run completed:** {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}",
        "",
        "---",
        "",
    ]

    # PR-by-PR gap analysis table — group by source_pr in batch mode
    actionable = [
        e for e in gap_entries.values()
        if e.terminal_state in ("gap_identified", "no_existing_coverage", "analysis_failed")
    ]

    lines += [
        "## PR-by-PR Gap Analysis",
        "",
        "| **PR #** | **Cluster / Area** | **Recommendation** | **Where To Update / Add** | **Affected TCs** |",
        "| --- | --- | --- | --- | --- |",
    ]

    # Sort by source_pr then cluster_area for consistent ordering
    for entry in sorted(actionable, key=lambda e: (e.source_pr, e.cluster_area)):
        affected = ", ".join(entry.affected_tcs) if entry.affected_tcs else "None (new)"
        lines.append(
            f"| **{entry.source_pr}** | **{entry.cluster_area}** "
            f"| **{recommendation_label(entry)}** "
            f"| {location_hint(entry, summary)} "
            f"| {affected} "
        )

    # Priority-sorted action summary
    lines += [
        "",
        "---",
        "",
        "## Action Summary",
        "",
        "| **Priority** | **Cluster** | **Action** |",
        "| --- | --- | --- |",
    ]
    sorted_entries = sorted(
        actionable,
        key=lambda e: (PRIORITY_ORDER.get(e.priority, 9), e.cluster_area),
    )
    for entry in sorted_entries:
        verify_tag = " ⚠️" if entry.review_flag else ""
        lines.append(
            f"| **{entry.priority}{verify_tag}** "
            f"| **{entry.cluster_area}** "
            f"| **{recommendation_label(entry)}**<br>{entry.action_required} |"
        )

    lines += [
        "",
        "---",
        "",
        "## Detailed Recommendations",
        "",
    ]
    for entry in sorted_entries:
        lines += [
            f"### {entry.source_pr} — {entry.cluster_area}",
            "",
            f"- Recommendation: **{recommendation_label(entry)}**",
            f"- Priority: **{entry.priority}**" + (" ⚠️ Verify" if entry.review_flag else ""),
            f"- Coverage Verdict: `{entry.coverage_verdict}`",
            f"- Where To Update / Add: {location_hint(entry, summary)}",
            f"- Affected TCs: {', '.join(entry.affected_tcs) if entry.affected_tcs else 'None identified'}",
            f"- Spec Change: {entry.spec_change}",
            f"- Gap Details: {detail_hint(entry)}",
            f"- Action Required: {entry.action_required}",
            "",
        ]

    # Already-covered section
    covered = [e for e in gap_entries.values() if e.terminal_state == "already_covered"]
    if covered:
        lines += [
            "",
            "---",
            "",
            "## No Action Required",
            "",
            "The following spec changes are already covered by existing test cases:",
            "",
        ]
        for entry in covered:
            lines.append(f"- **{entry.cluster_area}** ({entry.source_pr}): {entry.spec_change}")

    return "\n".join(lines)


def render_github_comment(
    gap_entries: dict[str, GapEntry],
    summary: dict,
    packet: dict,
    report_url: Optional[str],
    local_report_path: Optional[str] = None,
    filter_pr: Optional[int] = None,
) -> str:
    """
    Phase 6c: condensed comment body for a spec PR.
    filter_pr: if set, only include entries whose source_pr matches this PR number.
               Used in batch mode so each PR gets its own relevant comment.
    """
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    pr_tag   = f"#{filter_pr}" if filter_pr else None

    actionable = [
        e for e in gap_entries.values()
        if e.terminal_state in ("gap_identified", "no_existing_coverage", "analysis_failed")
        and (pr_tag is None or e.source_pr == pr_tag)
    ]
    sorted_entries = sorted(
        actionable,
        key=lambda e: (PRIORITY_ORDER.get(e.priority, 9), e.cluster_area),
    )

    rows = "\n".join(
        f"| **{e.priority}{'⚠️' if e.review_flag else ''}** | **{e.cluster_area}** | **{recommendation_label(e)}** | {location_hint(e, summary)} |"
        for e in sorted_entries
    )

    detail_rows = "\n".join(
        f"- **{e.cluster_area}** — {recommendation_label(e)}<br>"
        f"Where: {location_hint(e, summary)}<br>"
        f"Details: {detail_hint(e)}<br>"
        f"Action: {e.action_required}"
        for e in sorted_entries
    )

    # Counts scoped to this PR if filtering
    this_verify  = sum(1 for e in actionable if e.review_flag)
    this_covered = sum(1 for e in gap_entries.values()
                       if e.terminal_state == "already_covered"
                       and (pr_tag is None or e.source_pr == pr_tag))
    this_new_tcs = sum(max(len(e.affected_tcs), 1) for e in actionable if e.action_type == "create")
    this_updates = len({e.cluster_area for e in actionable if e.action_type != "create"})

    if report_url:
        report_link = f"[Full Report]({report_url})"
    elif local_report_path:
        report_link = f"`{local_report_path}`"
    else:
        report_link = "_Report not committed_"

    return (
        f"## 🔍 Test Plan Gap Analysis — Auto-Generated\n\n"
        f"**Source PR:** #{packet['pr_number']} — {packet['pr_title']}  \n"
        f"**Run completed:** {date_str}  \n"
        f"**Full report:** {report_link}\n\n"
        f"---\n\n"
        f"| Spec Changes | New TCs Needed | TC Updates | Needs Review |\n"
        f"| --- | --- | --- | --- |\n"
        f"| {len(actionable)} "
        f"| {this_new_tcs} "
        f"| {this_updates} "
        f"| {this_verify} |\n\n"
        f"### Action Summary\n\n"
        f"| Priority | Cluster | Recommendation | Where |\n"
        f"| --- | --- | --- | --- |\n"
        f"{rows}\n\n"
        f"### Details\n\n"
        f"{detail_rows}\n\n"
        f"---\n"
        f"*{this_verify} entries marked ⚠️ Verify — confirm before acting.*  \n"
        f"*{this_covered} spec changes already covered — no action needed.*"
    )


def commit_report_to_branch(
    docs_repo_local_path: str,
    report_md: str,
    filename: str,
    branch: str,
    pr_number: int,
    push_target: Optional[str] = None,
) -> Optional[str]:
    """
    Commits the report Markdown to the reports branch of the local docs repo clone.
    Returns the relative path committed, or None on failure.
    """
    repo_path = Path(docs_repo_local_path)
    if not is_git_repo(repo_path):
        logger.warning("Skipping report commit because %s is not a git checkout.", repo_path)
        return None

    reports_dir = repo_path / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    report_path = reports_dir / filename
    relative_report_path = report_path.relative_to(repo_path)

    try:
        # Ensure we're on the reports branch (create if necessary)
        subprocess.run(
            ["git", "-C", str(repo_path), "checkout", "-B", branch],
            check=True, capture_output=True, text=True,
        )
        report_path.write_text(report_md, encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(repo_path), "add", str(relative_report_path)],
            check=True, capture_output=True, text=True,
        )
        commit_result = subprocess.run(
            ["git", "-C", str(repo_path), "commit", "-m",
             f"docs(gap-report): PR #{pr_number} gap analysis report"],
            capture_output=True, text=True,
        )
        if commit_result.returncode != 0:
            combined = f"{commit_result.stdout}\n{commit_result.stderr}".lower()
            if "nothing to commit" not in combined:
                commit_result.check_returncode()
            logger.info("Report contents unchanged on branch '%s'; skipping commit.", branch)
        else:
            push_destination = push_target or "origin"
            subprocess.run(
                ["git", "-C", str(repo_path), "push", push_destination, f"HEAD:refs/heads/{branch}"],
                check=True, capture_output=True, text=True,
            )
        logger.info("Report committed to branch '%s': reports/%s", branch, filename)
        return str(relative_report_path)
    except subprocess.CalledProcessError as e:
        logger.warning("Failed to commit report: %s", e.stderr)
        return None


# ---------------------------------------------------------------------------
# Batch entry point — analyse a list of PRs, produce one combined report
# ---------------------------------------------------------------------------

def run_batch(
    spec_repo_input: str,
    pr_numbers: list[int],
    docs_repo_input: str,
    config: dict,
    dry_run: bool = False,
    spec_base_branch: Optional[str] = None,
    docs_branch: Optional[str] = None,
    docs_subdir: str = ".",
    repo_cache_dir: str = "./repo_cache",
    openrouter_api_key: Optional[str] = None,
) -> dict:
    """
    Runs the full gap analysis pipeline across multiple PRs and produces a
    single combined report covering all of them.

    Each PR is fetched independently. Tasks are decomposed per-PR and tagged
    with their source PR number. The combined gap entry list is assembled,
    validated, and rendered into one unified report.

    Returns a combined summary dict with per-PR breakdowns.
    """
    spec_token = get_spec_github_token()
    docs_token = get_docs_github_token(required=False)
    llm_model = config["llm"]["model"]
    gh = Github(auth=Auth.Token(spec_token))
    client = OpenAI(
        api_key     = resolve_openrouter_api_key(openrouter_api_key),
        base_url    = "https://openrouter.ai/api/v1",
        max_retries = 0,
    )
    spec_repo_name = normalize_spec_repo_name(spec_repo_input)
    docs_repo = prepare_repo(
        repo_input=docs_repo_input,
        branch=docs_branch,
        subdir=docs_subdir,
        clone_root=repo_cache_dir,
        label="docs",
    )

    try:
        persist_dir = os.environ.get("CHROMA_PERSIST_DIR", "./chroma_db")
        scope_id = repo_scope_id(docs_repo)
        index = VectorIndex(
            persist_directory=persist_dir,
            llm_client=client,
            llm_model=llm_model,
            collection_name=f"matter_test_cases_{scope_id}",
        )

        logger.info(
            "Using docs repo source '%s' at branch '%s' (subdir '%s').",
            docs_repo.repo_name or docs_repo.repo_root,
            docs_repo.branch,
            docs_repo.subdir,
        )
        logger.info("Using configured LLM model '%s' for all LLM-backed analysis.", llm_model)
        sync_vector_index(index, docs_repo.content_root, persist_dir, scope_id)

        # Build header map once — shared across all PRs
        logger.info("=== Phase 0: Building header map ===")
        header_map = build_header_map(str(docs_repo.content_root))
        HEADER_MAP_PATH.parent.mkdir(parents=True, exist_ok=True)
        HEADER_MAP_PATH.write_text(json.dumps({"headers": header_map["headers"]}, indent=2))

        # ---------- per-PR fetch + decompose ----------
        all_tasks: list[Task] = []
        all_packets: list[dict] = []
        skipped_prs: list[int] = []

        for pr_number in progress_bar(
            pr_numbers,
            total=len(pr_numbers),
            desc="Fetching and decomposing PRs",
            unit="pr",
        ):
            logger.info("=== Fetching PR #%d ===", pr_number)
            try:
                packet = fetch_pr_packet(
                    gh,
                    spec_repo_name,
                    pr_number,
                    config["llm"]["diff_truncation_tokens"],
                    expected_base_branch=spec_base_branch,
                )
            except Exception as e:
                logger.error("Failed to fetch PR #%d: %s — skipping.", pr_number, e)
                skipped_prs.append(pr_number)
                continue

            logger.info("=== Phase 2: Decomposing PR #%d ===", pr_number)
            try:
                tasks = decompose_pr(packet, config, client)
            except Exception as e:
                logger.error("Decomposition failed for PR #%d: %s — skipping.", pr_number, e)
                skipped_prs.append(pr_number)
                continue

            # Prefix task IDs with the PR number to avoid collisions across PRs
            for t in tasks:
                t.task_id = f"pr{pr_number}_{t.task_id}"

            all_tasks.extend(tasks)
            all_packets.append(packet)
            logger.info("PR #%d: %d actionable tasks.", pr_number, len(tasks))

        if not all_tasks:
            logger.info("No actionable tasks across all PRs — nothing to report.")
            return {"status": "no_action", "pr_numbers": pr_numbers, "skipped": skipped_prs}

        # ---------- Phase 3+4 loop (shared across all PRs) ----------
        logger.info("=== Phase 3-4: Agentic loop (%d total tasks across %d PRs) ===",
                    len(all_tasks), len(all_packets))

        gap_entries: dict[str, GapEntry] = {}
        retrieval_map: dict[str, RetrievalResult] = {}
        partial_warning: Optional[str] = None

        for i, task in enumerate(
            progress_bar(
                all_tasks,
                total=len(all_tasks),
                desc="Analyzing tasks",
                unit="task",
            ),
            1,
        ):
            # Extract the original PR number from the prefixed task_id
            pr_num_for_task = int(task.task_id.split("_")[0].lstrip("pr"))
            logger.info("[%d/%d] %s — %s (%s)", i, len(all_tasks),
                        task.task_id, task.cluster_area, task.type)
            try:
                retrieval = retrieve_for_task(
                    task,
                    header_map,
                    index,
                    str(docs_repo.content_root),
                    config,
                )
                retrieval_map[task.task_id] = retrieval
                reasoning = reason_about_gap(task, retrieval, config, client)
                gap_id = f"gap_{i:03d}"
                entry = assemble_gap_entry(gap_id, task, retrieval, reasoning, pr_num_for_task)
                gap_entries[task.task_id] = entry
                logger.info("  -> %s | %s | %s", entry.terminal_state,
                            entry.coverage_verdict, entry.priority)
            except Exception as e:
                logger.error("Task %s failed: %s", task.task_id, e, exc_info=True)
                gap_entries[task.task_id] = GapEntry(
                    gap_id=f"gap_{i:03d}", task_id=task.task_id,
                    source_pr=f"#{pr_num_for_task}", cluster_area=task.cluster_area,
                    spec_change=task.spec_change_summary, affected_tcs=[],
                    coverage_verdict="full_gap", gap_details=f"Analysis failed: {e}",
                    action_type="audit",
                    action_required="Manual review required — automated analysis failed.",
                    priority=task.priority, source_file=None, source_section=None, match_confidence="low",
                    review_flag=True, terminal_state="analysis_failed",
                )

        # ---------- Phase 5 validation ----------
        logger.info("=== Phase 5: Validation ===")
        completeness_errors = validate_completeness(all_tasks, gap_entries)
        if completeness_errors:
            partial_warning = (
                f"{len(completeness_errors)} tasks incomplete: "
                + "; ".join(completeness_errors[:3])
            )
            logger.error("Completeness errors: %s", completeness_errors)

        for w in check_priority_distribution(gap_entries):
            logger.warning(w)

        self_review_sample(gap_entries, retrieval_map, config, client)

        # Build a combined summary using all PR numbers
        summary = compute_run_summary(gap_entries, all_tasks, pr_numbers[0])
        summary["pr_numbers"] = pr_numbers
        summary["prs_analyzed"] = len(all_packets)
        summary["skipped_prs"] = skipped_prs
        summary["spec_repo"] = spec_repo_name
        summary["spec_base_branch"] = spec_base_branch
        summary["docs_repo"] = docs_repo.repo_name or str(docs_repo.repo_root)
        summary["docs_branch"] = docs_repo.branch
        summary["docs_subdir"] = docs_repo.subdir

        # ---------- Phase 6 delivery ----------
        logger.info("=== Phase 6: Report delivery ===")

        # For multi-PR, render_markdown_report needs a synthetic packet
        combined_packet = {
            "pr_number": pr_numbers[0],
            "pr_title": f"Multiple PRs: {', '.join(f'#{n}' for n in pr_numbers[:5])}{'...' if len(pr_numbers) > 5 else ''}",
            "html_url": f"https://github.com/{spec_repo_name}/pulls",
        }
        report_md = render_markdown_report(
            gap_entries, summary, combined_packet, partial_warning,
            all_packets=all_packets,
        )

        date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
        pr_slug = "_".join(str(n) for n in pr_numbers[:4])
        if len(pr_numbers) > 4:
            pr_slug += f"_and_{len(pr_numbers)-4}_more"
        prefix = config["report"]["report_filename_prefix"]
        filename = f"{prefix}_{pr_slug}_{date_str}.md"
        branch = config["report"]["reports_branch"]

        report_url: Optional[str] = None
        local_report_path: Optional[str] = None
        if not dry_run and config["report"]["commit_full_report"]:
            push_target = None
            if docs_repo.repo_name and docs_token:
                push_target = make_authed_clone_url(docs_repo.repo_name, docs_token)
            committed_path = commit_report_to_branch(
                str(docs_repo.repo_root), report_md, filename, branch,
                pr_numbers[0],
                push_target=push_target,
            )
            if committed_path and docs_repo.repo_name:
                report_url = (
                    f"https://github.com/{docs_repo.repo_name}/blob/{branch}/{committed_path}"
                )
            else:
                out_path = Path(filename).resolve()
                out_path.write_text(report_md, encoding="utf-8")
                local_report_path = str(out_path)
                logger.info("Report commit failed; report written locally to %s", out_path)
        else:
            out_path = Path(filename)
            out_path.write_text(report_md, encoding="utf-8")
            local_report_path = str(out_path.resolve())
            logger.info("Dry-run: report written locally to %s", out_path)

        # Post a comment on each analyzed PR
        if not dry_run and config["report"]["post_github_comment"]:
            spec_repo_obj = gh.get_repo(spec_repo_name)
            for packet in progress_bar(
                all_packets,
                total=len(all_packets),
                desc="Posting PR comments",
                unit="pr",
            ):
                pr_num = packet["pr_number"]
                comment_body = render_github_comment(
                    gap_entries,
                    summary,
                    packet,
                    report_url,
                    local_report_path=local_report_path,
                    filter_pr=pr_num,
                )
                try:
                    issue = spec_repo_obj.get_issue(pr_num)
                    comment = issue.create_comment(comment_body)
                    logger.info("Posted comment on PR #%d (comment #%d).", pr_num, comment.id)
                except GithubException as e:
                    logger.warning("Failed to post comment on PR #%d: %s", pr_num, e)
        else:
            logger.info("Dry-run: GitHub comments not posted.")

        logger.info(
            "Batch done. %d PRs | %d gaps | %d new TCs | %d to verify | %d already covered.",
            len(all_packets), summary["gaps_identified"], summary["new_tcs_needed"],
            summary["entries_flagged_for_verify"], summary["already_covered"],
        )
        if local_report_path:
            summary["local_report_path"] = local_report_path
        return summary
    finally:
        docs_repo.cleanup()


# ---------------------------------------------------------------------------
# Main orchestrator (single PR)
# ---------------------------------------------------------------------------

def run(
    spec_repo_input: str,
    pr_number: int,
    docs_repo_input: str,
    config: dict,
    dry_run: bool = False,
    spec_base_branch: Optional[str] = None,
    docs_branch: Optional[str] = None,
    docs_subdir: str = ".",
    repo_cache_dir: str = "./repo_cache",
    openrouter_api_key: Optional[str] = None,
) -> dict:
    """
    Single-PR convenience wrapper — delegates to run_batch().
    """
    return run_batch(
        spec_repo_input = spec_repo_input,
        pr_numbers = [pr_number],
        docs_repo_input = docs_repo_input,
        config = config,
        dry_run = dry_run,
        spec_base_branch = spec_base_branch,
        docs_branch = docs_branch,
        docs_subdir = docs_subdir,
        repo_cache_dir = repo_cache_dir,
        openrouter_api_key = openrouter_api_key,
    )


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    logging.basicConfig(
        level   = logging.INFO,
        format  = "%(asctime)s %(levelname)s %(message)s",
        datefmt = "%H:%M:%S",
    )

    parser = argparse.ArgumentParser(
        description="Run Matter test plan gap analysis for one or more merged spec PRs.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Single PR
  python gap_analyzer.py --pr 12681 --spec-repo org/matter-test-spec --docs-repo https://github.com/your-org/matter-test-cases --openrouter-api-key sk-or-v1-...

  # Multiple PRs — one combined report, one comment per PR
  python gap_analyzer.py --prs 12681 12657 12615 --spec-repo org/matter-test-spec --docs-repo org/matter-test-cases

  # Dry-run: remote private test repo + explicit branch and subfolder
  python gap_analyzer.py --prs 12681 12657 --spec-repo https://github.com/your-org/matter-test-spec --spec-branch release/1.6 --docs-repo https://github.com/your-org/matter-test-cases --docs-branch release/1.6 --docs-subdir src/tests --dry-run

Required environment variables:
  CHROMA_PERSIST_DIR  (e.g. "./chroma_db")

No OpenAI key needed — embeddings run locally via sentence-transformers.

Optional:
  OPENROUTER_API_KEY                 (used if --openrouter-api-key is not passed)
  SPEC_GITHUB_TOKEN                 (spec repo read/comment; falls back to GITHUB_TOKEN)
  DOCS_GITHUB_TOKEN                 (docs repo clone/push; falls back to GITHUB_TOKEN)
  GITHUB_TOKEN                      (single-token fallback for both)
  DOCS_REPO / DOCS_REPO_LOCAL_PATH  (local path, owner/repo, or GitHub URL)
  DOCS_REPO_BRANCH                  (branch/ref for a remote docs repo)
  DOCS_REPO_SUBDIR                  (repo-relative folder containing .adoc files)
  SPEC_REPO_BRANCH                  (expected base branch for spec PRs)
  GAP_REPO_CACHE_DIR                (where remote repos are cloned)
        """,
    )

    pr_group = parser.add_mutually_exclusive_group(required=True)
    pr_group.add_argument("--pr",  type=int,       help="Single PR number to analyse")
    pr_group.add_argument("--prs", type=int, nargs="+", help="Multiple PR numbers to analyse together")

    default_docs_repo = (
        os.environ.get("DOCS_REPO")
        or os.environ.get("DOCS_REPO_URL")
        or os.environ.get("DOCS_REPO_LOCAL_PATH")
        or "./matter-test-cases"
    )

    parser.add_argument(
        "--spec-repo",
        required=True,
        help="Spec repo as owner/repo, GitHub URL, or local git checkout path",
    )
    parser.add_argument(
        "--spec-branch",
        default=os.environ.get("SPEC_REPO_BRANCH"),
        help="Expected base branch for the spec PRs (optional)",
    )
    parser.add_argument(
        "--docs-repo", "--docs-path",
        dest="docs_repo",
        default=default_docs_repo,
        help="Test case repo source: local path, owner/repo, or GitHub URL",
    )
    parser.add_argument(
        "--docs-branch",
        default=os.environ.get("DOCS_REPO_BRANCH"),
        help="Branch/ref to read from the test case repo when cloning a remote repo",
    )
    parser.add_argument(
        "--docs-subdir",
        default=os.environ.get("DOCS_REPO_SUBDIR", "."),
        help="Repo-relative folder containing the test case .adoc files",
    )
    parser.add_argument(
        "--repo-cache-dir",
        default=os.environ.get("GAP_REPO_CACHE_DIR", "./repo_cache"),
        help="Local directory used for temporary remote repo clones",
    )
    parser.add_argument(
        "--openrouter-api-key",
        default=None,
        help="OpenRouter API key. If omitted, OPENROUTER_API_KEY is used.",
    )
    parser.add_argument("--config",      default="workflow_config.yaml")
    parser.add_argument("--dry-run",     action="store_true",
                        help="Skip GitHub comment + report commit; write report locally only")
    args = parser.parse_args()

    pr_numbers = args.prs if args.prs else [args.pr]
    config     = load_config(args.config)

    try:
        result = run_batch(
            spec_repo_input = args.spec_repo,
            pr_numbers = pr_numbers,
            docs_repo_input = args.docs_repo,
            config = config,
            dry_run = args.dry_run,
            spec_base_branch = args.spec_branch,
            docs_branch = args.docs_branch,
            docs_subdir = args.docs_subdir,
            repo_cache_dir = args.repo_cache_dir,
            openrouter_api_key = args.openrouter_api_key,
        )
        print(json.dumps(result, indent=2))
    except Exception as exc:
        logger.error("Fatal error: %s", exc, exc_info=True)
        sys.exit(1)
