"""Hybrid BM25 + FAISS text search — recall + semantic precision.

BM25 casts a wide net (top-20 by keyword relevance).  FAISS re-ranks the
candidates by semantic similarity to the query.  This gives better results
than either alone:

- BM25 never misses exact keyword matches (FAISS can drift semantically).
- FAISS catches synonyms and paraphrases that BM25 misses.
- Both are optional: the index degrades gracefully to BM25-only if no
  embedding model is available.

API contract unchanged: ``search(query, k)`` returns
``list[tuple[Document, float]]`` matching FAISS ``similarity_search_with_score``.
"""

from __future__ import annotations

import logging
import math
import pickle
import re

from collections import defaultdict
from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter
from pathlib import Path

logger = logging.getLogger(__name__)


# ── Chunking ───────────────────────────────────────────────────────────────────


def chunk_markdown_by_boundaries(content: str) -> list[dict]:
    """Split markdown content into chunks based on page, slide, sheet, or heading sentinels."""
    chunks: list[dict] = []
    current_page: int | None = None
    current_heading: str | None = None
    current_locator: str | None = None
    current_lines: list[str] = []

    page_re = re.compile(r"<!--\s*page:\s*(\d+)\s*-->")
    slide_re = re.compile(r"<!--\s*slide:\s*(\d+)\s*-->")
    sheet_re = re.compile(r"<!--\s*sheet:\s*([^;]+?)(?:\s*;\s*row:\s*(\d+))?\s*-->")
    heading_re = re.compile(r"<!--\s*heading:\s*(\d+)\s*-->")

    for line in content.splitlines():
        page_match = page_re.search(line)
        slide_match = slide_re.search(line)
        sheet_match = sheet_re.search(line)
        heading_match = heading_re.search(line)

        if page_match or slide_match or sheet_match or heading_match:
            if current_lines:
                chunk_text = "\n".join(current_lines).strip()
                if chunk_text:
                    chunks.append(
                        {
                            "text": chunk_text,
                            "page": current_page,
                            "locator": current_locator,
                            "heading": current_heading,
                        }
                    )
                current_lines = []

            if page_match:
                current_page = int(page_match.group(1))
                current_locator = f"Page {current_page}"
                current_heading = None
            elif slide_match:
                current_page = int(slide_match.group(1))
                current_locator = f"Slide {current_page}"
                current_heading = None
            elif sheet_match:
                sheet_name = sheet_match.group(1).strip()
                row = sheet_match.group(2)
                current_locator = (
                    f"Sheet: {sheet_name}, row {row}" if row else f"Sheet: {sheet_name}"
                )
                current_page = None
                current_heading = None
            elif heading_match:
                current_heading = f"Heading level {heading_match.group(1)}"

        current_lines.append(line)

    if current_lines:
        chunk_text = "\n".join(current_lines).strip()
        if chunk_text:
            chunks.append(
                {
                    "text": chunk_text,
                    "page": current_page,
                    "locator": current_locator,
                    "heading": current_heading,
                }
            )

    return chunks


def get_document_chunks(content: str) -> list[dict]:
    """Split markdown content structurally, then sub-split using text splitter."""
    raw_chunks = chunk_markdown_by_boundaries(content)
    splitter = RecursiveCharacterTextSplitter(chunk_size=1500, chunk_overlap=150)
    final_chunks: list[dict] = []

    for chunk in raw_chunks:
        text = chunk["text"]
        if len(text) <= 2000:
            final_chunks.append(chunk)
        else:
            for sub_text in splitter.split_text(text):
                final_chunks.append(
                    {
                        "text": sub_text,
                        "page": chunk["page"],
                        "locator": chunk["locator"],
                        "heading": chunk["heading"],
                    }
                )

    return final_chunks


# ── Stemming ───────────────────────────────────────────────────────────────────

_STEM_RULES: list[tuple[str, str | None]] = [
    ("ational", "ate"),
    ("tional", "tion"),
    ("enci", "ence"),
    ("anci", "ance"),
    ("izer", "ize"),
    ("abli", "able"),
    ("alli", "al"),
    ("entli", "ent"),
    ("eli", "e"),
    ("ousli", "ous"),
    ("ization", "ize"),
    ("ation", "ate"),
    ("ator", "ate"),
    ("alism", "al"),
    ("iveness", "ive"),
    ("fulness", "ful"),
    ("ousness", "ous"),
    ("aliti", "al"),
    ("iviti", "ive"),
    ("biliti", "ble"),
    ("logi", "log"),
    ("icate", "ic"),
    ("ies", "y"),  # Plurals: companies → company
    ("ful", None),
    ("ness", None),
    ("ive", None),
    ("able", None),
    ("ible", None),
    ("ment", None),
    ("ant", None),
    ("ent", None),
    ("ism", None),
    ("ate", None),
    ("iti", None),
    ("ous", None),
    ("al", None),
    ("er", None),
    ("ic", None),
    ("ing", None),
    ("ed", None),
    ("es", None),
    ("s", None),
    ("ly", None),
]

_MIN_STEM_LEN = 3


def stem_word(word: str) -> str:
    """Lightweight English stemmer — multi-pass suffix stripping with de-doubling."""
    if len(word) <= _MIN_STEM_LEN:
        return word
    stem = word
    for _pass_idx in range(2):
        matched = False
        for suffix, replacement in _STEM_RULES:
            if stem.endswith(suffix) and len(stem) - len(suffix) >= _MIN_STEM_LEN:
                stem = stem[: -len(suffix)]
                if replacement is not None:
                    stem += replacement
                if len(stem) >= 3 and stem[-1] == stem[-2] and stem[-1] not in "aeiou":
                    stem = stem[:-1]
                matched = True
                break
        if not matched:
            break
    return stem


# ── Tokenization ───────────────────────────────────────────────────────────────

_TOKEN_RE = re.compile(r"[a-z0-9]+", re.IGNORECASE)
_NUMERIC_RE = re.compile(r"^[0-9]+$")


def _tokenize(text: str, *, stem: bool = True, bigrams: bool = True) -> list[str]:
    """Lowercase, tokenize, optionally stem and add bigrams.  Numeric tokens preserved."""
    raw = _TOKEN_RE.findall(text.lower())
    tokens: list[str] = []
    for t in raw:
        if len(t) < 2 and not _NUMERIC_RE.match(t):
            continue
        if stem and not _NUMERIC_RE.match(t):
            tokens.append(stem_word(t))
        else:
            tokens.append(t)
    if bigrams and len(tokens) >= 2:
        tokens.extend(f"{tokens[i]}_{tokens[i + 1]}" for i in range(len(tokens) - 1))
    return tokens


# ── BM25 Search Index ──────────────────────────────────────────────────────────

_BM25_K1 = 1.5
_BM25_B = 0.75
_PRF_TOP_N = 3
_PRF_TERMS = 10
_PRF_WEIGHT = 0.3


class BM25SearchIndex:
    """Enhanced BM25 with stemming, bigrams, and pseudo-relevance feedback."""

    def __init__(self, *, prf: bool = True) -> None:
        self._documents: list[Document] = []
        self._doc_tfs: list[dict[str, int]] = []
        self._doc_lens: list[int] = []
        self._idf: dict[str, float] = {}
        self._avgdl: float = 0.0
        self._prf_enabled = prf

    @property
    def documents(self) -> list[Document]:
        return self._documents

    def add_documents(self, documents: list[Document]) -> None:
        total_tokens = sum(self._doc_lens)
        for doc in documents:
            self._documents.append(doc)
            tokens = _tokenize(doc.page_content)
            tf: dict[str, int] = {}
            for t in tokens:
                tf[t] = tf.get(t, 0) + 1
            self._doc_tfs.append(tf)
            self._doc_lens.append(len(tokens))
            total_tokens += len(tokens)
        doc_count = len(self._documents)
        self._avgdl = total_tokens / doc_count if doc_count else 0.0
        df: dict[str, int] = defaultdict(int)
        for tf_map in self._doc_tfs:
            for term in tf_map:
                df[term] += 1
        self._idf = {
            term: math.log(1.0 + (doc_count - freq + 0.5) / (freq + 0.5))
            for term, freq in df.items()
        }

    def search(
            self, query: str, k: int = 5, *, use_prf: bool | None = None
    ) -> list[tuple[Document, float]]:
        if use_prf is None:
            use_prf = self._prf_enabled
        query_terms = _tokenize(query)
        if not query_terms or not self._documents:
            return []
        results = self._bm25_score_all(query_terms, k=max(k, 10))
        if not results and len(query_terms) > 1:
            for term in query_terms:
                if len(term) < 3:
                    continue
                for doc, score in self._bm25_score_all([term], k=k):
                    results.append((doc, score))
            seen: set[int] = set()
            deduped: list[tuple[Document, float]] = []
            for doc, score in results:
                if id(doc) not in seen:
                    seen.add(id(doc))
                    deduped.append((doc, score))
            deduped.sort(key=lambda x: x[1], reverse=True)
            results = deduped[:k]
        if not use_prf or not results or len(self._documents) <= _PRF_TOP_N:
            return results[:k]
        expansion_terms = self._extract_prf_terms(
            [doc for doc, _ in results[:_PRF_TOP_N]],
            exclude_terms=set(query_terms),
            top_n=_PRF_TERMS,
        )
        if not expansion_terms:
            return results[:k]
        expanded = list(query_terms)
        for term in expansion_terms:
            expanded.extend([term] * max(1, int(len(query_terms) * _PRF_WEIGHT)))
        prf_results = self._bm25_score_all(expanded, k=k)
        seen_ids = {id(d) for d, _ in results}
        merged = list(results)
        for doc, score in prf_results:
            if id(doc) not in seen_ids:
                merged.append((doc, score * 0.9))
                seen_ids.add(id(doc))
        merged.sort(key=lambda x: x[1], reverse=True)
        return merged[:k]

    def _bm25_score_all(
            self, query_terms: list[str], k: int
    ) -> list[tuple[Document, float]]:
        qtf: dict[str, float] = {}
        for term in query_terms:
            qtf[term] = qtf.get(term, 0.0) + self._idf.get(term, 0.0)
        scores: list[tuple[int, float]] = []
        for doc_idx, tf_map in enumerate(self._doc_tfs):
            score = 0.0
            doc_len = self._doc_lens[doc_idx]
            doc_len_norm = (
                (1.0 - _BM25_B + _BM25_B * (doc_len / self._avgdl))
                if self._avgdl > 0
                else 1.0
            )
            for term, q_weight in qtf.items():
                tf = tf_map.get(term, 0)
                if tf == 0:
                    continue
                score += q_weight * (
                        tf * (_BM25_K1 + 1.0) / (tf + _BM25_K1 * doc_len_norm)
                )
            if score > 0.0:
                scores.append((doc_idx, score))
        scores.sort(key=lambda x: x[1], reverse=True)
        return [(self._documents[idx], s) for idx, s in scores[:k]]

    def _extract_prf_terms(
            self, top_docs: list[Document], exclude_terms: set[str], top_n: int
    ) -> list[str]:
        total_docs = len(self._documents)
        prf_tf: dict[str, float] = defaultdict(float)
        for doc in top_docs:
            tokens = _tokenize(doc.page_content)
            seen: set[str] = set()
            for t in tokens:
                if t not in seen:
                    prf_tf[t] += 1.0
                    seen.add(t)
        scored: list[tuple[str, float]] = []
        for term, tf_in_prf in prf_tf.items():
            if term in exclude_terms or len(term) < 3:
                continue
            df = sum(1 for tf_map in self._doc_tfs if term in tf_map)
            if df == 0:
                continue
            scored.append((term, tf_in_prf * math.log(1.0 + total_docs / df)))
        scored.sort(key=lambda x: x[1], reverse=True)
        return [term for term, _ in scored[:top_n]]

    def save(self, directory: Path) -> None:
        """Persist to ``directory/index.pkl``."""
        directory.mkdir(parents=True, exist_ok=True)
        with (directory / "index.pkl").open("wb") as f:
            pickle.dump(self, f, protocol=pickle.HIGHEST_PROTOCOL)

    @classmethod
    def load(cls, directory: Path) -> BM25SearchIndex | None:
        """Load from ``directory/index.pkl``."""
        path = directory / "index.pkl"
        if not path.exists():
            return None
        try:
            with path.open("rb") as f:
                obj = pickle.load(f)
            return obj if isinstance(obj, BM25SearchIndex) else None
        except Exception:
            return None

    def __len__(self) -> int:
        return len(self._documents)


# ── Hybrid Search Index (BM25 recall → FAISS re-rank) ─────────────────────────

# BM25 retrieves this many candidates for FAISS to re-rank.
_HYBRID_BM25_K = 20


class HybridSearchIndex:
    """BM25 for keyword recall → FAISS for semantic re-ranking.

    Builds both a BM25 index and a FAISS vector store.  At query time BM25
    casts a wide net (top-20) then FAISS re-ranks the candidates to the
    final top-K.  This combines the strengths of both approaches.

    If no embedding model is available at build time the FAISS index is
    skipped and search degrades gracefully to BM25-only.
    """

    def __init__(self, bm25: BM25SearchIndex, faiss_store=None) -> None:
        self.bm25 = bm25
        self._faiss = faiss_store  # FAISS vectorstore or None
        self._faiss_doc_map: dict[int, Document] = {}  # faiss internal id → Document

    @property
    def documents(self) -> list[Document]:
        return self.bm25.documents

    @property
    def has_faiss(self) -> bool:
        return self._faiss is not None

    def search(self, query: str, k: int = 5) -> list[tuple[Document, float]]:
        """BM25 recall → FAISS re-rank → top-k."""
        if not self._faiss:
            return self.bm25.search(query, k=k)

        # 1) BM25: wide-net recall.
        bm25_candidates = self.bm25.search(query, k=min(_HYBRID_BM25_K, len(self.bm25)))
        if not bm25_candidates:
            logger.warning(
                "BM25 returned zero results for query=%r. "
                "FAISS index is available (%s) but HybridSearchIndex.search() "
                "does not auto-fallback — call search_faiss_only() explicitly.",
                query, "yes" if self._faiss else "no",
            )
            return []

        # 2) FAISS: re-rank candidates by semantic similarity.
        # Build a temporary in-memory FAISS index over just the candidates.
        candidate_docs = [doc for doc, _ in bm25_candidates]
        try:
            reranked = self._faiss_re_rank(query, candidate_docs, k=k)
        except Exception:
            logger.debug("FAISS re-rank failed, falling back to BM25", exc_info=True)
            return bm25_candidates[:k]

        return reranked

    def _faiss_re_rank(
            self, query: str, candidates: list[Document], k: int
    ) -> list[tuple[Document, float]]:
        """Re-rank candidates using FAISS semantic similarity."""
        from langchain_community.vectorstores import FAISS

        # Get the same embedding model used to build the full index.
        embedding = _resolve_embedding_model()
        if embedding is None:
            return [(doc, 0.0) for doc in candidates[:k]]

        # Build a lightweight FAISS index over just the candidates.
        tmp_store = FAISS.from_documents(candidates, embedding)
        results = tmp_store.similarity_search_with_score(query, k=k)

        # FAISS returns L2 distance (lower = better).  Invert to a relevance
        # score (higher = better) so the output format is consistent.
        scored: list[tuple[Document, float]] = []
        for doc, distance in results:
            relevance = 1.0 / (1.0 + distance)  # Map [0, ∞) → (0, 1]
            scored.append((doc, relevance))
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored

    def search_faiss_only(self, query: str, k: int = 5) -> list[tuple[Document, float]]:
        """Search the full FAISS index directly (semantic-only, no BM25 gating).

        Use this as a fallback when BM25 keyword search returns zero or few
        results.  The FAISS index was built over ALL documents at index-build
        time, so this captures synonyms and paraphrases that BM25 misses.

        Returns the same ``list[tuple[Document, float]]`` format as
        :meth:`search`, making it a drop-in fallback.
        """
        if not self._faiss:
            return []

        try:
            results = self._faiss.similarity_search_with_score(query, k=k)
        except Exception:
            logger.debug("FAISS direct search failed", exc_info=True)
            return []

        # FAISS returns L2 distance (lower = better).  Invert to relevance
        # score (higher = better) so the output format is consistent.
        scored: list[tuple[Document, float]] = []
        for doc, distance in results:
            relevance = 1.0 / (1.0 + distance)
            scored.append((doc, relevance))
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored

    def save(self, directory: Path) -> None:
        """Persist BM25 (pickle) and FAISS (save_local) to directory."""
        directory.mkdir(parents=True, exist_ok=True)
        # Save BM25 separately.
        bm25_path = directory / "index.pkl"
        with bm25_path.open("wb") as f:
            pickle.dump(self.bm25, f, protocol=pickle.HIGHEST_PROTOCOL)
        # Save FAISS if present.
        if self._faiss is not None:
            try:
                self._faiss.save_local(str(directory / "faiss"))
            except Exception:
                logger.warning(
                    "Failed to save FAISS index; BM25 will be used on load",
                    exc_info=True,
                )

    @classmethod
    def load(cls, directory: Path) -> HybridSearchIndex | None:
        """Load a previously saved hybrid index."""
        bm25_path = directory / "index.pkl"
        bm25 = None
        # --- Load BM25 Index ---
        if not bm25_path.exists():
            return None

        try:
            with bm25_path.open("rb") as f:
                bm25 = pickle.load(f)
            if not isinstance(bm25, BM25SearchIndex):
                # The loaded object is not the expected type, indicating corruption or wrong save format.
                return None
        except (IOError, EOFError, pickle.UnpicklingError) as e:
            logger.warning(f"Failed to load BM25 index from {bm25_path}: {e}")
            # Cannot proceed without a valid BM25 component.
            return None

        faiss_store = None
        # --- Load FAISS Store (Optional) ---
        faiss_dir = directory / "faiss"
        if faiss_dir.exists() and (faiss_dir / "index.faiss").exists():
            try:
                embedding = _resolve_embedding_model()
                if embedding is not None:
                    from langchain_community.vectorstores import FAISS

                    # Use the explicit path string for load_local
                    faiss_store = FAISS.load_local(
                        str(faiss_dir),
                        embedding,
                        allow_dangerous_deserialization=True,
                    )
            except Exception as e:
                logger.warning(
                    f"Failed to load FAISS index from {faiss_dir}: {e}. Using BM25-only."
                )

        # Initialize and return the hybrid index using loaded components.
        return cls(bm25, faiss_store)

    def __len__(self) -> int:
        return len(self.bm25)


# ── Embedding model resolution ─────────────────────────────────────────────────

_embedding_model_cache: object | None = None
_embedding_failed: bool = False


def _resolve_embedding_model():
    """Resolve the embedding model with single-attempt caching.

    Returns None if no embedding model is available (e.g. no API keys
    configured).  The first failure is cached so we don't retry on every
    query.
    """
    global _embedding_model_cache, _embedding_failed
    if _embedding_model_cache is not None:
        return _embedding_model_cache
    if _embedding_failed:
        return None

    try:
        import sys as _sys
        from pathlib import Path as _Path

        _sys.path.insert(0, str(_Path(__file__).resolve().parent.parent.parent))
        try:
            from model_factory import create_embedding_model

            model = create_embedding_model()
            _embedding_model_cache = model
            return model
        finally:
            _sys.path.pop(0)
    except Exception:
        _embedding_failed = True
        logger.debug(
            "No embedding model available; search will use BM25-only", exc_info=True
        )
        return None


# ── Public API ─────────────────────────────────────────────────────────────────


def build_search_index(
        raw_dir: Path, output_index_dir: Path
) -> HybridSearchIndex | None:
    """Build a hybrid BM25 + FAISS search index from raw markdown documents.

    FAISS is built only if an embedding model is available.  The BM25 index
    is always built and serves as the recall layer (and sole engine if no
    embeddings).
    """
    if not raw_dir.exists():
        return None

    md_files = list(raw_dir.rglob("*.md"))
    if not md_files:
        return None

    documents: list[Document] = []
    for md_file in md_files:
        try:
            content = md_file.read_text(encoding="utf-8")
        except Exception:
            continue
        for chunk in get_document_chunks(content):
            metadata = {
                "source_path": f"/raw/{md_file.name}",
                "page": chunk["page"],
                "locator": chunk["locator"],
                "heading": chunk["heading"],
            }
            metadata = {k: v for k, v in metadata.items() if v is not None}
            documents.append(Document(page_content=chunk["text"], metadata=metadata))

    if not documents:
        return None

    # Always build BM25.
    bm25 = BM25SearchIndex(prf=True)
    bm25.add_documents(documents)

    # Optionally build FAISS on the full corpus for re-ranking.
    faiss_store = None
    try:
        embedding = _resolve_embedding_model()
        if embedding is not None:
            from langchain_community.vectorstores import FAISS

            faiss_store = FAISS.from_documents(documents, embedding)
            logger.info(
                "Built FAISS index for %d documents alongside BM25", len(documents)
            )
    except Exception:
        logger.warning("FAISS index build failed; using BM25-only", exc_info=True)

    index = HybridSearchIndex(bm25, faiss_store)
    index.save(output_index_dir)
    return index


def load_or_build_search_index(
        raw_dir: Path, output_index_dir: Path
) -> HybridSearchIndex | None:
    """Load an existing hybrid search index or build a new one."""
    existing = HybridSearchIndex.load(output_index_dir)
    if existing is not None:
        return existing
    return build_search_index(raw_dir, output_index_dir)


def search_index(
        query: str, index: HybridSearchIndex, k: int = 5
) -> list[tuple[Document, float]]:
    """Search a hybrid index; returns top-k (document, relevance-score) results."""
    return index.search(query, k=k)
