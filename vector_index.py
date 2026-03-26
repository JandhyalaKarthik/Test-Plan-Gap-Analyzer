"""
Phase 0 — 2b: Vector Index Construction

Handles full rebuilds and incremental updates of the ChromaDB vector index
over .adoc test case files. Deliberately stores no line numbers — those are
always resolved live from the header map at execution time.

Key design decisions reflected here:
  - AsciiDoc attribute substitution (e.g. :picsCode: MCORE.FS → {picsCode})
    is resolved before chunking so TC-IDs and header text are meaningful.
  - raw_header_text (the literal text as it appears in the file) is stored
    alongside resolved_header_text so that header map lookups work correctly.
  - Chunking level is determined per-file based on file type. Test case files
    use level 4 (====), one chunk per test case. Spec files use level 2 (==).
  - Chunks carry no line numbers — those are always resolved live from the
    header map (Phase 0 — 2a) at execution time.

Dependencies:
    pip install chromadb anthropic tiktoken sentence-transformers

Embedding model: nomic-ai/nomic-embed-text-v1.5 (runs fully locally via
sentence-transformers — no API key or cost). The model uses task-prefix
notation: documents are prefixed with "search_document:" at index time,
queries are prefixed with "search_query:" at search time, as required by
the nomic model for asymmetric retrieval quality.

LLM: claude-haiku-4-5-20251001 — available on the Claude free plan.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional

import anthropic
import chromadb
import tiktoken
from sentence_transformers import SentenceTransformer

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

EMBEDDING_MODEL       = "nomic-ai/nomic-embed-text-v1.5"
# nomic-embed-text-v1.5 requires task prefixes for asymmetric retrieval:
#   documents → "search_document: <text>"
#   queries   → "search_query: <text>"
EMBED_DOC_PREFIX      = "search_document: "
EMBED_QUERY_PREFIX    = "search_query: "
TAG_MODEL             = "claude-haiku-4-5-20251001"  # free plan + fast for short tag lists
CHROMA_COLLECTION     = "matter_test_cases"
ADOC_HEADER_PATTERN   = re.compile(r"^(=+)\s+(.+)$", re.MULTILINE)
ADOC_ATTR_DEF_PATTERN = re.compile(r"^:(\w[\w-]*):\s*(.*)$", re.MULTILINE)
# Matches resolved TC-IDs like TC-MCORE.FS-1.1 or TC-CC-1.3
TC_ID_PATTERN         = re.compile(r"TC-[A-Z0-9]+(?:\.[A-Z0-9]+)*-[0-9]+(?:\.[0-9]+)?")
MIN_CHUNK_TOKENS      = 50
MAX_CHUNK_TOKENS      = 1_500
EMBED_BATCH_SIZE      = 64    # sentence-transformers processes locally; keep moderate
TAG_BATCH_SIZE        = 10    # chunks per LLM tagging call

# ---------------------------------------------------------------------------
# File exclusion — non-test-case .adoc files that should not be indexed
# ---------------------------------------------------------------------------

# Stem patterns (case-insensitive) that identify non-test-case files.
# Matched against Path.stem (filename without extension).
EXCLUDED_STEMS = {
    "license", "licence", "notice", "notices",
    "readme", "changelog", "changes", "history",
    "summary", "index", "toc", "cover",
    "copyright", "disclaimer", "contributing",
    "authors", "credits",
}

# Content signals that strongly indicate a non-test-case file.
# If a file matches any of these AND has no test-case signals, it is excluded.
_CONTENT_EXCLUDE_PATTERNS = [
    re.compile(r"copyright\s*\([Cc]\)", re.IGNORECASE),       # copyright notice
    re.compile(r"all rights reserved", re.IGNORECASE),         # license boilerplate
    re.compile(r"^:doctype:\s*book", re.MULTILINE),            # book-style summary doc
    re.compile(r"THIS DOCUMENT.*PROVIDED.*AS.IS", re.IGNORECASE),
]

# Content signal that confirms a file IS a test case (overrides exclusion).
_CONTENT_INCLUDE_PATTERN = re.compile(
    r"^:picsCode:|===== Test Procedure|===== Purpose|Test Case List|PICS Definition",
    re.MULTILINE,
)


def should_index_file(file_path: str, raw_content: str) -> tuple[bool, str]:
    """
    Decides whether a .adoc file should be indexed.

    Returns (should_index, reason_if_excluded).

    Rules applied in order:
    1. If the filename stem matches a known non-test-case pattern → exclude.
    2. If the content has any test-case signal → include (overrides content exclusion).
    3. If the content has multiple non-test-case signals → exclude.
    4. Otherwise → include (err on the side of indexing).
    """
    stem = Path(file_path).stem.lower()

    # Rule 1 — filename-based exclusion
    if stem in EXCLUDED_STEMS:
        return False, f"filename '{stem}' matches exclusion list"

    # Rule 2 — content confirms it is a test case → always include
    if _CONTENT_INCLUDE_PATTERN.search(raw_content):
        return True, ""

    # Rule 3 — multiple non-test-case content signals → exclude
    exclusion_hits = [p.pattern for p in _CONTENT_EXCLUDE_PATTERNS if p.search(raw_content)]
    if len(exclusion_hits) >= 2:
        return False, f"content signals: {exclusion_hits}"

    # Rule 4 — single signal is ambiguous; index conservatively
    return True, ""


# ---------------------------------------------------------------------------
# File type detection
# ---------------------------------------------------------------------------

class FileType(Enum):
    TEST_CASE = "test_case"   # Has :picsCode:, ==== test case sections
    SPEC      = "spec"        # Specification prose, include:: directives
    UNKNOWN   = "unknown"     # Cannot determine — chunk conservatively


def detect_file_type(raw_content: str) -> FileType:
    """
    Inspects raw .adoc content to decide how it should be chunked.

    Signals for TEST_CASE:
      - Has a :picsCode: attribute definition (strongest signal)
      - Contains '===== Test Procedure' or '===== Purpose' sub-sections
      - Contains 'Test Case List' or 'PICS Definition' headings

    Signals for SPEC:
      - Has include:: directives but no :picsCode:
      - Has [[ref_...]] anchor tags (spec cross-reference style)
      - No ==== level headers at all

    Falls back to UNKNOWN if signals are mixed or absent.
    """
    has_pics_code      = bool(re.search(r"^:picsCode:", raw_content, re.MULTILINE))
    has_test_procedure = bool(re.search(r"^={4,5}\s+(?:Test Procedure|Purpose|Preconditions)", raw_content, re.MULTILINE))
    has_test_case_list = bool(re.search(r"Test Case List|PICS Definition", raw_content))
    has_level4_headers = bool(re.search(r"^====\s+", raw_content, re.MULTILINE))
    has_include        = bool(re.search(r"^include::", raw_content, re.MULTILINE))
    has_spec_anchors   = bool(re.search(r"\[\[ref_", raw_content))

    if has_pics_code or (has_test_procedure and has_test_case_list):
        return FileType.TEST_CASE

    if (has_include or has_spec_anchors) and not has_level4_headers:
        return FileType.SPEC

    if has_level4_headers:
        return FileType.TEST_CASE

    return FileType.UNKNOWN


def chunk_level_for(file_type: FileType) -> int:
    """
    Returns the header level (number of = signs) at which chunk boundaries are placed.

    TEST_CASE → 4  (==== is one complete test case, e.g. [TC-MCORE.FS-1.1] FS Setup)
    SPEC      → 2  (== is a major spec section, e.g. == Certificate Common Conventions)
    UNKNOWN   → 3  (conservative middle ground)
    """
    return {
        FileType.TEST_CASE: 4,
        FileType.SPEC:      2,
        FileType.UNKNOWN:   3,
    }[file_type]


# ---------------------------------------------------------------------------
# AsciiDoc attribute resolution
# ---------------------------------------------------------------------------

def resolve_adoc_attributes(raw_content: str) -> tuple[str, dict[str, str]]:
    """
    Extracts all :attr: value definitions from a file and substitutes
    {attr} references throughout the content.

    AsciiDoc attribute definitions look like:
        :picsCode: MCORE.FS
        :sectnums:            <- boolean flag, no value — collected with empty string

    Substitution references look like:
        TC-{picsCode}-1.1  ->  TC-MCORE.FS-1.1

    Only single-pass substitution is performed (no recursive expansion).

    Returns:
        resolved_content: content with all {attr} references substituted
        attr_dict: {attribute_name: value} for inspection and per-string application
    """
    attr_dict: dict[str, str] = {}
    for m in ADOC_ATTR_DEF_PATTERN.finditer(raw_content):
        attr_dict[m.group(1)] = m.group(2).strip()

    resolved = raw_content
    for key, value in attr_dict.items():
        resolved = resolved.replace(f"{{{key}}}", value)

    return resolved, attr_dict


def _apply_attrs(text: str, attr_dict: dict[str, str]) -> str:
    """Applies a pre-parsed attribute dict to a single string."""
    for key, value in attr_dict.items():
        text = text.replace(f"{{{key}}}", value)
    return text


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Chunk:
    """
    A single indexable section from an .adoc file.

    Two header text fields are stored:
      raw_header_text      — the literal text as it appears in the file,
                             including any unresolved {attr} references.
                             Used as the key for header map lookups at
                             execution time, because the header map is also
                             built from the raw file.
      resolved_header_text — attribute-substituted version. Used for TC-ID
                             extraction, display, and embedding quality.

    No line_start / line_end — see workflow doc Section 2b.
    Line numbers are always resolved live from the header map at execution time.
    """
    chunk_id:             str            # "{filename}::{raw_header_text}" — stable across edits
    file_path:            str            # Relative path inside the docs repo
    raw_header_text:      str            # Literal header text (for header map join)
    resolved_header_text: str            # Attribute-substituted header text (for search/display)
    header_level:         int            # Number of '=' signs
    file_type:            str            # "test_case" | "spec" | "unknown"
    tc_id:                Optional[str]  # e.g. "TC-MCORE.FS-1.1", from resolved header
    pics_code:            Optional[str]  # e.g. "MCORE.FS" from :picsCode: attr
    cluster_tags:         list[str]      # LLM-extracted topic tags (populated after chunking)
    raw_text:             str            # Full resolved section text for embedding

    def to_chroma_document(self) -> str:
        """Text passed to the embedding model."""
        return self.raw_text

    def to_chroma_metadata(self) -> dict:
        """
        Metadata stored alongside the embedding in ChromaDB.
        All values must be str | int | float | bool — no None, no lists.
        """
        return {
            "file_path":            self.file_path,
            "raw_header_text":      self.raw_header_text,
            "resolved_header_text": self.resolved_header_text,
            "header_level":         self.header_level,
            "file_type":            self.file_type,
            "tc_id":                self.tc_id or "",
            "pics_code":            self.pics_code or "",
            # Stored as JSON string — ChromaDB doesn't support list metadata natively
            "cluster_tags":         json.dumps(self.cluster_tags),
        }


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------

def _count_tokens(text: str, enc: tiktoken.Encoding) -> int:
    return len(enc.encode(text))


def _split_on_paragraphs(text: str, max_tokens: int, enc: tiktoken.Encoding) -> list[str]:
    """
    Splits text on double-newline paragraph boundaries until every piece is
    under max_tokens. Used when a section exceeds MAX_CHUNK_TOKENS.
    """
    paragraphs = re.split(r"\n\n+", text)
    pieces: list[str] = []
    current_parts: list[str] = []
    current_tokens = 0

    for para in paragraphs:
        para_tokens = _count_tokens(para, enc)
        if current_tokens + para_tokens > max_tokens and current_parts:
            pieces.append("\n\n".join(current_parts))
            current_parts = [para]
            current_tokens = para_tokens
        else:
            current_parts.append(para)
            current_tokens += para_tokens

    if current_parts:
        pieces.append("\n\n".join(current_parts))

    return pieces


def chunk_adoc_file(raw_content: str, file_path: str) -> list[Chunk]:
    """
    Splits a single .adoc file into Chunk objects.

    Step 1 — Detect file type and choose chunk level.
        Test case files (has :picsCode:, ==== headers) -> chunk at level 4.
        Spec files (include:: directives, [[ref_...]] anchors) -> chunk at level 2.
        Unknown -> chunk at level 3.

    Step 2 — Resolve AsciiDoc attributes.
        Extracts :attr: definitions and substitutes {attr} references.
        Turns TC-{picsCode}-1.1 into TC-MCORE.FS-1.1 so TC-IDs are meaningful.

    Step 3 — Parse header positions from the RAW content.
        Raw positions are used for slicing so raw_header_text stays aligned
        with the actual file. The resolved header text is derived per-header.

    Step 4 — Build chunks at the target level only.
        Headers below the target level (e.g. ===== sub-sections within a test
        case like Purpose, Preconditions, Test Procedure, Notes) are treated as
        part of the chunk body — NOT as chunk boundaries. This keeps one complete
        test case as one coherent semantic unit in the index.

    Step 5 — Handle oversized sections.
        Sections above MAX_CHUNK_TOKENS are split on paragraph boundaries.
        Each sub-chunk gets a content-hash suffix on its chunk_id so it can be
        individually deleted and reinserted during incremental updates.
    """
    enc = tiktoken.get_encoding("cl100k_base")
    filename = Path(file_path).name

    # Gate: skip non-test-case files (licensing, summaries, READMEs, etc.)
    should_index, skip_reason = should_index_file(file_path, raw_content)
    if not should_index:
        logger.debug("Skipping %s — %s", file_path, skip_reason)
        return []

    # Step 1 — file type and chunk level
    file_type   = detect_file_type(raw_content)
    chunk_level = chunk_level_for(file_type)
    logger.debug("%s -> file_type=%s, chunk_level=%d", file_path, file_type.value, chunk_level)

    # Step 2 — attribute resolution
    _, attr_dict = resolve_adoc_attributes(raw_content)
    pics_code = attr_dict.get("picsCode") or None

    # Step 3 — parse ALL header positions from raw content.
    # We deliberately parse raw (not resolved) content so character positions
    # are accurate for slicing raw_content below.
    raw_all_headers: list[tuple[int, int, str]] = [
        (m.start(), len(m.group(1)), m.group(2).strip())
        for m in ADOC_HEADER_PATTERN.finditer(raw_content)
    ]

    if not raw_all_headers:
        logger.debug("No headers found in %s — skipping.", file_path)
        return []

    # Step 4 — build chunks only for headers at exactly chunk_level
    chunks: list[Chunk] = []

    for i, (start, level, raw_header_text) in enumerate(raw_all_headers):
        if level != chunk_level:
            continue

        # Section body runs from this header to the next header at the same
        # or higher level (fewer = signs). Sub-headers (level > chunk_level)
        # are absorbed into the body — this is the key fix for test case files,
        # where ===== Purpose / Preconditions / Test Procedure are part of the
        # same test case as its ==== header, not separate semantic units.
        end = len(raw_content)
        for next_start, next_level, _ in raw_all_headers[i + 1:]:
            if next_level <= chunk_level:
                end = next_start
                break

        # Slice from raw then resolve attributes so the embedded text is clean
        raw_section  = raw_content[start:end].rstrip()
        section_text = _apply_attrs(raw_section, attr_dict)

        if _count_tokens(section_text, enc) < MIN_CHUNK_TOKENS:
            logger.debug(
                "Skipping stub section '%s' in %s (under %d tokens).",
                raw_header_text, file_path, MIN_CHUNK_TOKENS,
            )
            continue

        resolved_header_text = _apply_attrs(raw_header_text, attr_dict)
        tc_match = TC_ID_PATTERN.search(resolved_header_text)
        tc_id    = tc_match.group(0) if tc_match else None

        # chunk_id is keyed on raw_header_text — stable even if :picsCode: changes,
        # because raw_header_text still contains the unresolved {picsCode} literal.
        chunk_id = f"{filename}::{raw_header_text}"

        if _count_tokens(section_text, enc) <= MAX_CHUNK_TOKENS:
            chunks.append(Chunk(
                chunk_id             = chunk_id,
                file_path            = file_path,
                raw_header_text      = raw_header_text,
                resolved_header_text = resolved_header_text,
                header_level         = level,
                file_type            = file_type.value,
                tc_id                = tc_id,
                pics_code            = pics_code,
                cluster_tags         = [],   # Populated by extract_cluster_tags()
                raw_text             = section_text,
            ))
        else:
            # Step 5 — oversized: split on paragraph boundaries
            pieces = _split_on_paragraphs(section_text, MAX_CHUNK_TOKENS, enc)
            logger.debug(
                "Section '%s' in %s exceeded max tokens — split into %d pieces.",
                raw_header_text, file_path, len(pieces),
            )
            for piece in pieces:
                piece_hash = hashlib.sha1(piece.encode()).hexdigest()[:8]
                chunks.append(Chunk(
                    chunk_id             = f"{chunk_id}::{piece_hash}",
                    file_path            = file_path,
                    raw_header_text      = raw_header_text,
                    resolved_header_text = resolved_header_text,
                    header_level         = level,
                    file_type            = file_type.value,
                    tc_id                = tc_id,
                    pics_code            = pics_code,
                    cluster_tags         = [],
                    raw_text             = piece,
                ))

    if not chunks:
        logger.debug(
            "No level-%d headers found in %s (file_type=%s) — no chunks produced.",
            chunk_level, file_path, file_type.value,
        )

    return chunks


# ---------------------------------------------------------------------------
# Cluster tag extraction (LLM)
# ---------------------------------------------------------------------------

def extract_cluster_tags(chunks: list[Chunk], anthropic_client: anthropic.Anthropic) -> None:
    """
    Enriches each Chunk's cluster_tags in-place via batched LLM calls.

    Each call sends TAG_BATCH_SIZE chunks and receives a JSON array of tag lists
    in the same order. Tags are short snake_case strings stored as metadata for
    ChromaDB filtered search.

    Failures are soft: a failed batch leaves cluster_tags as [], so retrieval
    still works, just without tag-based filtering for those chunks.
    """
    for batch_start in range(0, len(chunks), TAG_BATCH_SIZE):
        batch = chunks[batch_start : batch_start + TAG_BATCH_SIZE]

        sections_text = "\n\n".join(
            f"[{idx}] Header: {c.resolved_header_text}\n"
            f"PICS Code: {c.pics_code or 'N/A'}\n"
            f"Content (truncated):\n{c.raw_text[:400]}"
            for idx, c in enumerate(batch)
        )

        prompt = f"""You are tagging Matter protocol test case documentation sections.

For each numbered section below, emit 1-5 short snake_case tags describing the
cluster name, feature area, or protocol domain the section covers.
Use the PICS Code as a strong signal when present.

Good tag examples: fabric_sync, color_control, zigbee, occupancy_sensing,
thread, ble, wifi, door_lock, level_control, on_off, window_covering,
thermostat, device_attestation, operational_credentials, commissioner_control.

Return ONLY a JSON array of arrays, one inner array per section, in the same order.
No preamble, no markdown fences, no extra keys.
Example for 3 sections: [["fabric_sync","commissioner_control"],["color_control","lighting"],["on_off"]]

Sections:
{sections_text}"""

        try:
            response = anthropic_client.messages.create(
                model    = TAG_MODEL,
                max_tokens = 512,
                messages = [{"role": "user", "content": prompt}],
            )
            raw = response.content[0].text.strip()
            tag_lists: list[list[str]] = json.loads(raw)

            if len(tag_lists) != len(batch):
                logger.warning(
                    "Tag extraction returned %d results for %d chunks — skipping batch.",
                    len(tag_lists), len(batch),
                )
                continue

            for chunk, tags in zip(batch, tag_lists):
                chunk.cluster_tags = [str(t).lower() for t in tags if isinstance(t, str)]

        except (json.JSONDecodeError, IndexError, anthropic.APIError) as exc:
            logger.warning("Tag extraction failed for batch at index %d: %s", batch_start, exc)
            # cluster_tags remains [] — retrieval still works without tag filtering


# ---------------------------------------------------------------------------
# Embedding model — loaded once as a module-level singleton
# ---------------------------------------------------------------------------

def _load_embedding_model() -> SentenceTransformer:
    """
    Loads nomic-embed-text-v1.5 via sentence-transformers.

    trust_remote_code=True is required by the nomic model. The model is
    ~274 MB and downloads once to the HuggingFace cache on first run.
    Subsequent runs load from cache — no internet connection needed.
    """
    logger.info("Loading embedding model '%s'...", EMBEDDING_MODEL)
    model = SentenceTransformer(EMBEDDING_MODEL, trust_remote_code=True)
    logger.info("Embedding model loaded (dim=%d).", model.get_sentence_embedding_dimension())
    return model


# Lazy singleton — instantiated on first call to embed_chunks() or embed_query()
_embedding_model: Optional[SentenceTransformer] = None


def _get_embedding_model() -> SentenceTransformer:
    global _embedding_model
    if _embedding_model is None:
        _embedding_model = _load_embedding_model()
    return _embedding_model


# ---------------------------------------------------------------------------
# Embedding
# ---------------------------------------------------------------------------

def embed_chunks(chunks: list[Chunk]) -> list[list[float]]:
    """
    Generates embeddings for all chunks using nomic-embed-text-v1.5.

    Documents are prefixed with "search_document: " as required by the nomic
    model's asymmetric retrieval design. Processing is fully local — no API
    calls, no rate limits, no cost.

    Returns embedding vectors in the same order as the input chunks.
    """
    model = _get_embedding_model()
    embeddings: list[list[float]] = []

    for batch_start in range(0, len(chunks), EMBED_BATCH_SIZE):
        batch = chunks[batch_start : batch_start + EMBED_BATCH_SIZE]
        # Prepend the document task prefix required by nomic-embed-text-v1.5
        texts = [EMBED_DOC_PREFIX + c.to_chroma_document() for c in batch]

        try:
            vecs = model.encode(
                texts,
                batch_size       = EMBED_BATCH_SIZE,
                show_progress_bar= False,
                normalize_embeddings = True,   # cosine similarity needs unit vectors
            )
            embeddings.extend(vecs.tolist())
            logger.debug(
                "Embedded batch %d-%d (%d chunks).",
                batch_start, batch_start + len(batch) - 1, len(batch),
            )
        except Exception as exc:
            # sentence-transformers runs locally so transient errors are rare;
            # re-raise immediately rather than retrying on a corrupted model state
            raise RuntimeError(
                f"Embedding failed for batch starting at index {batch_start}: {exc}"
            ) from exc

    return embeddings


def embed_query(query: str) -> list[float]:
    """
    Embeds a single search query using the nomic query task prefix.
    Called at search time by VectorIndex.search().
    """
    model = _get_embedding_model()
    vec   = model.encode(
        [EMBED_QUERY_PREFIX + query],
        normalize_embeddings = True,
    )
    return vec[0].tolist()


# ---------------------------------------------------------------------------
# Vector Index
# ---------------------------------------------------------------------------

class VectorIndex:
    """
    Thin wrapper around a ChromaDB collection that handles full rebuilds,
    incremental file-level updates, and semantic search.

    Chunk boundaries, attribute resolution, and file type detection are all
    handled by chunk_adoc_file() — this class only concerns itself with
    storage, retrieval, and lifecycle management.
    """

    def __init__(
        self,
        persist_directory: str,
        anthropic_client: anthropic.Anthropic,
        collection_name: str = CHROMA_COLLECTION,
    ):
        self._chroma     = chromadb.PersistentClient(path=persist_directory)
        self._collection = self._chroma.get_or_create_collection(
            name     = collection_name,
            metadata = {"hnsw:space": "cosine"},
        )
        self._anthropic = anthropic_client
        # Trigger model load at construction time so the first search isn't slow
        _get_embedding_model()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def build_full_index(self, adoc_files: dict[str, str]) -> None:
        """
        Full rebuild from scratch. Drops all existing chunks and re-indexes
        every file. Use on initial setup or after major repo restructuring.

        :param adoc_files: {relative_file_path: raw_file_content}
        """
        logger.info("Starting full index rebuild over %d files...", len(adoc_files))

        existing_count = self._collection.count()
        if existing_count > 0:
            logger.info("Clearing %d existing chunks.", existing_count)
            all_ids = self._collection.get(include=[])["ids"]
            if all_ids:
                self._collection.delete(ids=all_ids)

        all_chunks = self._chunk_and_tag_files(adoc_files)

        if not all_chunks:
            logger.warning("No chunks produced — index is empty.")
            return

        self._embed_and_store(all_chunks)
        logger.info("Full rebuild complete. %d chunks indexed.", len(all_chunks))

    def update_files(self, changed_files: dict[str, str]) -> None:
        """
        Incremental update for a set of changed or newly created files.

        For each file:
          1. Delete all existing chunks for that file_path (metadata filter).
          2. Re-chunk, re-tag, re-embed, and reinsert.

        Because chunks carry no line numbers, this is always a complete,
        safe operation — no other files in the index are touched.

        :param changed_files: {relative_file_path: new_raw_file_content}
        """
        logger.info("Incremental update for %d changed file(s).", len(changed_files))

        for file_path in changed_files:
            self._delete_chunks_for_file(file_path)

        new_chunks = self._chunk_and_tag_files(changed_files)
        if new_chunks:
            self._embed_and_store(new_chunks)

        logger.info("Incremental update complete. %d new chunks inserted.", len(new_chunks))

    def remove_file(self, file_path: str) -> None:
        """
        Removes all chunks for a deleted file.
        Call this when a .adoc file is removed from the docs repo.
        """
        deleted = self._delete_chunks_for_file(file_path)
        logger.info("Removed %d chunks for deleted file: %s", deleted, file_path)

    def search(
        self,
        query: str,
        n_results: int = 5,
        cluster_tag_filter: Optional[str] = None,
        file_type_filter: Optional[str] = None,
    ) -> list[dict]:
        """
        Semantic search over the index.

        :param query:              Natural language query. For best results, combine
                                   the task's target_description and change_summary
                                   from the decomposer output.
        :param n_results:          Maximum results to return.
        :param cluster_tag_filter: Restricts results to chunks whose cluster_tags
                                   JSON string contains this value.
                                   e.g. "authentication" or "billing"
        :param file_type_filter:   Restricts results to a specific file type:
                                   "test_case", "spec", or "unknown".
                                   Defaults to "test_case" — spec prose is rarely
                                   what the retrieval loop needs.

        :returns: List of result dicts. Callers use raw_header_text + file_path
                  to look up the section in the header map for live line-number
                  resolution at execution time.
        """
        effective_file_type = file_type_filter if file_type_filter is not None else "test_case"

        # Embed the query with the nomic query task prefix (asymmetric retrieval)
        query_embedding = embed_query(query)

        where_clauses: list[dict] = [{"file_type": {"$eq": effective_file_type}}]
        if cluster_tag_filter:
            where_clauses.append({"cluster_tags": {"$contains": cluster_tag_filter}})

        where_filter = (
            {"$and": where_clauses} if len(where_clauses) > 1 else where_clauses[0]
        )

        results = self._collection.query(
            query_embeddings = [query_embedding],
            n_results        = n_results,
            include          = ["metadatas", "documents", "distances"],
            where            = where_filter,
        )

        output = []
        for chunk_id, meta, doc, distance in zip(
            results["ids"][0],
            results["metadatas"][0],
            results["documents"][0],
            results["distances"][0],
        ):
            similarity = round(1.0 - (distance / 2.0), 4)
            output.append({
                "chunk_id":             chunk_id,
                "file_path":            meta["file_path"],
                # raw_header_text is used by the execution phase to join against
                # the header map for live line-number resolution
                "raw_header_text":      meta["raw_header_text"],
                "resolved_header_text": meta["resolved_header_text"],
                "header_level":         meta["header_level"],
                "file_type":            meta["file_type"],
                "tc_id":                meta["tc_id"] or None,
                "pics_code":            meta["pics_code"] or None,
                "cluster_tags":         json.loads(meta["cluster_tags"]),
                "raw_text":             doc,
                "similarity_score":     similarity,
            })

        return output

    def count(self) -> int:
        """Returns the total number of chunks currently in the index."""
        return self._collection.count()

    def inspect_file(self, file_path: str) -> list[dict]:
        """
        Returns all indexed chunks for a specific file.
        Useful for debugging chunking behaviour without running a search.
        """
        result = self._collection.get(
            where   = {"file_path": {"$eq": file_path}},
            include = ["metadatas", "documents"],
        )
        enc = tiktoken.get_encoding("cl100k_base")
        return [
            {
                "chunk_id":             cid,
                "raw_header_text":      meta["raw_header_text"],
                "resolved_header_text": meta["resolved_header_text"],
                "file_type":            meta["file_type"],
                "tc_id":                meta["tc_id"] or None,
                "pics_code":            meta["pics_code"] or None,
                "cluster_tags":         json.loads(meta["cluster_tags"]),
                "token_count":          _count_tokens(doc, enc),
            }
            for cid, meta, doc in zip(result["ids"], result["metadatas"], result["documents"])
        ]

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _chunk_and_tag_files(self, adoc_files: dict[str, str]) -> list[Chunk]:
        """Chunks all files then enriches all resulting chunks with cluster tags."""
        all_chunks: list[Chunk] = []

        for file_path, raw_content in adoc_files.items():
            file_chunks = chunk_adoc_file(raw_content, file_path)
            logger.debug("  %s -> %d chunk(s)", file_path, len(file_chunks))
            all_chunks.extend(file_chunks)

        if not all_chunks:
            return []

        logger.info("Extracting cluster tags for %d chunks...", len(all_chunks))
        extract_cluster_tags(all_chunks, self._anthropic)
        return all_chunks

    def _embed_and_store(self, chunks: list[Chunk]) -> None:
        """Generates embeddings and upserts chunks into ChromaDB."""
        logger.info("Generating embeddings for %d chunks...", len(chunks))
        embeddings = embed_chunks(chunks)
        self._collection.upsert(
            ids        = [c.chunk_id for c in chunks],
            embeddings = embeddings,
            documents  = [c.to_chroma_document() for c in chunks],
            metadatas  = [c.to_chroma_metadata() for c in chunks],
        )
        logger.info("Stored %d chunks in ChromaDB.", len(chunks))

    def _delete_chunks_for_file(self, file_path: str) -> int:
        """
        Deletes all chunks for a given file_path using a metadata filter.
        Returns count of deleted chunks.
        """
        existing      = self._collection.get(where={"file_path": {"$eq": file_path}}, include=[])
        ids_to_delete = existing["ids"]
        if ids_to_delete:
            self._collection.delete(ids=ids_to_delete)
            logger.debug("Deleted %d chunks for file: %s", len(ids_to_delete), file_path)
        return len(ids_to_delete)


# ---------------------------------------------------------------------------
# Convenience factory
# ---------------------------------------------------------------------------

def build_index_from_env(persist_directory: str) -> VectorIndex:
    """
    Builds a VectorIndex using credentials from environment variables.

    Embeddings are generated locally via sentence-transformers — no API key needed.

    Required env var:
        ANTHROPIC_API_KEY   (for cluster tag extraction via Claude Haiku)
    """
    import os
    return VectorIndex(
        persist_directory = persist_directory,
        anthropic_client  = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"]),
    )


# ---------------------------------------------------------------------------
# CLI — full rebuild or incremental update from a local repo checkout
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    import os

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    parser = argparse.ArgumentParser(
        description="Build or update the Matter test case vector index.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Full rebuild from a local clone
  python vector_index.py /path/to/adoc-repo

  # Inspect how a single file was chunked without writing to the index
  python vector_index.py /path/to/adoc-repo --inspect fabric_synchronization.adoc

  # Incremental update after a docs PR merges
  python vector_index.py /path/to/adoc-repo \\
      --files clusters/fabric_sync/fabric_synchronization.adoc \\
              clusters/lighting/color_control.adoc
        """,
    )
    parser.add_argument("docs_repo_path", help="Local path to the cloned .adoc docs repo.")
    parser.add_argument(
        "--persist-dir",
        default=os.environ.get("CHROMA_PERSIST_DIR", "./chroma_db"),
        help="ChromaDB persistence directory.",
    )
    parser.add_argument("--file-ext", default=".adoc")
    parser.add_argument(
        "--files", nargs="*", default=None,
        help="Incremental mode: relative paths of files to re-index.",
    )
    parser.add_argument(
        "--inspect", metavar="FILE", default=None,
        help="Print chunking result for one file without writing to the index.",
    )
    args = parser.parse_args()

    docs_root = Path(args.docs_repo_path)

    # --inspect: chunk a file and print results, no index writes
    if args.inspect:
        inspect_path = docs_root / args.inspect
        raw          = inspect_path.read_text(encoding="utf-8")
        file_type    = detect_file_type(raw)
        chunks       = chunk_adoc_file(raw, args.inspect)
        enc          = tiktoken.get_encoding("cl100k_base")

        print(f"\nFile:             {args.inspect}")
        print(f"File type:        {file_type.value}")
        print(f"Chunk level:      {chunk_level_for(file_type)} ({'=' * chunk_level_for(file_type)})")
        print(f"Total chunks:     {len(chunks)}\n")
        for c in chunks:
            tokens = _count_tokens(c.raw_text, enc)
            print(f"  [{tokens:4d} tok] {c.chunk_id}")
            print(f"           raw:      {c.raw_header_text}")
            print(f"           resolved: {c.resolved_header_text}")
            print(f"           tc_id:    {c.tc_id}   pics_code: {c.pics_code}\n")
        raise SystemExit(0)

    index = build_index_from_env(args.persist_dir)

    if args.files:
        changed: dict[str, str] = {}
        for rel_path in args.files:
            abs_path = docs_root / rel_path
            if abs_path.exists():
                changed[rel_path] = abs_path.read_text(encoding="utf-8")
            else:
                index.remove_file(rel_path)
        if changed:
            index.update_files(changed)
    else:
        adoc_files: dict[str, str] = {
            str(p.relative_to(docs_root)): p.read_text(encoding="utf-8")
            for p in docs_root.rglob(f"*{args.file_ext}")
        }
        logger.info("Found %d %s files under %s.", len(adoc_files), args.file_ext, docs_root)
        index.build_full_index(adoc_files)

    logger.info("Index now contains %d total chunks.", index.count())
