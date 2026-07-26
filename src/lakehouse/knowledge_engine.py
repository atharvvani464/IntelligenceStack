"""Local vector index standing in for Databricks Mosaic AI Vector Search.

This module is the sandbox counterpart to `src/cognitive/vector_engine.py`
(which provisions a real serverless Mosaic AI index in a workspace), exactly as
`src/lakehouse/local_engine.py` is the sandbox counterpart to the Unity Catalog
functions in `src/governance/uc_bootstrap.py`.

It builds a genuine vector index over a small corpus of governed enterprise
documents and answers similarity queries entirely locally and offline:

  * Each document is chunked by section heading.
  * A TF-IDF model embeds every chunk into a vector.
  * The vectors are stored in a DuckDB table and ranked with the engine's
    native `list_cosine_similarity` -- the same "similarity search in the
    lakehouse" pattern Mosaic AI Vector Search provides.

The seam to production is honest and narrow: swapping the TF-IDF embedding for a
dense neural embedding (e.g. `databricks-bge-large-en`) changes how the vectors
are produced, not how they are stored or queried. The `search()` contract is
identical either way.
"""

import logging
import re

import duckdb
from sklearn.feature_extraction.text import ENGLISH_STOP_WORDS, TfidfVectorizer

from src.settings import CATALOG, KNOWLEDGE_DIR, SCHEMA

logger = logging.getLogger("KnowledgeEngine")

# The fully-qualified name of the governed index, mirroring vector_engine.py.
KNOWLEDGE_INDEX = f"{CATALOG}.{SCHEMA}.knowledge_base_vector_index"

# A retrieval hit below this cosine similarity is treated as no match, so an
# off-topic question returns "no governed knowledge" rather than a weak guess.
MIN_SCORE = 0.05

# Abstention guard. Cosine score alone does not distinguish "genuinely covered"
# from "coincidentally shares one common word": asking about a *vacation policy*
# scores higher against this corpus than a legitimate question about a flagged
# customer, purely because the word "policy" is present. So a hit additionally
# requires that most of the question's meaningful terms actually appear in the
# governed corpus. Below this ratio the engine abstains rather than citing a
# weak lexical match -- a spurious citation is worse than an honest "no".
MIN_TERM_COVERAGE = 0.6

# Entity identifiers (e.g. CUST_404) are served by the structured analytics
# function, not by retrieval, so they are excluded from the coverage
# calculation -- their absence from the document corpus is expected.
_ENTITY_TOKEN = re.compile(r"^(?:cust_?\d+|\d+)$", re.IGNORECASE)

# Human-readable source names for citation. Files not listed fall back to a
# title-cased form of the stem.
_SOURCE_TITLES = {
    "sre_runbook": "SRE Runbook",
    "incident_playbook": "Incident Playbook",
    "customer_tiering_handbook": "Customer Tiering Handbook",
    "data_governance_policy": "Data Governance Policy",
}


def _source_title(stem: str) -> str:
    return _SOURCE_TITLES.get(stem, stem.replace("_", " ").title())


_TOKEN = re.compile(r"[a-z_][a-z0-9_]*")


def _stem(token: str) -> str:
    """Collapse common English inflections to a shared root.

    Without this, a question about "payloads" fails to match a document that
    says "payload", which then drags the term-coverage ratio below the
    abstention threshold and loses a legitimate answer. Deliberately
    conservative -- a crude suffix strip, not a full Porter stemmer.
    """
    for suffix, replacement in (("ies", "y"), ("ing", ""), ("ed", ""), ("es", ""), ("s", "")):
        if token.endswith(suffix) and len(token) - len(suffix) >= 4:
            return token[: len(token) - len(suffix)] + replacement
    return token


def _analyze(text: str) -> list[str]:
    """Tokenise, drop stop words, and stem. Used at index *and* query time."""
    return [
        _stem(t)
        for t in _TOKEN.findall(text.lower())
        if len(t) > 1 and t not in ENGLISH_STOP_WORDS
    ]


def _chunk_markdown(text: str) -> list[tuple[str, str]]:
    """Split a document into (section_title, passage) pairs by `##` headings.

    Content before the first `##` (typically the `# Title`) is dropped; the
    heading is prepended to its passage so the section topic is part of what
    gets embedded.
    """
    chunks: list[tuple[str, str]] = []
    current_title: str | None = None
    buffer: list[str] = []

    def flush():
        if current_title and buffer:
            body = " ".join(line.strip() for line in buffer if line.strip())
            if body:
                chunks.append((current_title, f"{current_title}. {body}"))

    for line in text.splitlines():
        heading = re.match(r"^##\s+(.*)", line)
        if heading:
            flush()
            current_title = heading.group(1).strip()
            buffer = []
        elif line.startswith("# "):
            continue  # document title, not a section
        else:
            buffer.append(line)
    flush()
    return chunks


class KnowledgeEngine:
    """Reads the governed corpus and serves similarity search over it."""

    def __init__(self, knowledge_dir=KNOWLEDGE_DIR):
        self.knowledge_dir = knowledge_dir
        self._con = duckdb.connect(database=":memory:")
        self._vectorizer: TfidfVectorizer | None = None
        self._built = False

    def build(self) -> "KnowledgeEngine":
        """Chunk, embed, and materialise the vector index from the corpus."""
        docs = sorted(self.knowledge_dir.glob("*.md")) if self.knowledge_dir.exists() else []
        if not docs:
            raise FileNotFoundError(
                f"No knowledge documents found in {self.knowledge_dir}. "
                "The governed corpus is required for retrieval."
            )

        records: list[tuple[str, str, str]] = []  # (source, title, text_chunk)
        for path in docs:
            source = _source_title(path.stem)
            for title, passage in _chunk_markdown(path.read_text()):
                records.append((source, title, passage))

        if not records:
            raise ValueError("Knowledge corpus produced no chunks; check document formatting.")

        # Fit the embedding model on the whole corpus, then embed every chunk.
        # sublinear_tf dampens repeated terms; english stop words drop noise.
        self._vectorizer = TfidfVectorizer(
            analyzer=_analyze, max_features=512, sublinear_tf=True
        )
        matrix = self._vectorizer.fit_transform([r[2] for r in records])
        embeddings = matrix.toarray().astype(float).tolist()

        self._con.execute(
            """
            CREATE OR REPLACE TABLE knowledge_base_vector_index (
                id        INTEGER,
                source    VARCHAR,
                title     VARCHAR,
                text_chunk VARCHAR,
                embedding FLOAT[]
            )
            """
        )
        self._con.executemany(
            "INSERT INTO knowledge_base_vector_index VALUES (?, ?, ?, ?, ?)",
            [
                (i, src, title, chunk, emb)
                for i, ((src, title, chunk), emb) in enumerate(zip(records, embeddings))
            ],
        )

        self._built = True
        logger.info(
            "Vector index %s built: %d chunks from %d documents.",
            KNOWLEDGE_INDEX,
            len(records),
            len(docs),
        )
        return self

    def _ensure_built(self) -> None:
        if not self._built:
            self.build()

    def _has_corpus_coverage(self, query: str) -> bool:
        """True when enough of the question's meaningful terms exist in the corpus.

        Guards against citing a passage that merely shares one common word with
        an otherwise off-topic question. See `MIN_TERM_COVERAGE`.
        """
        assert self._vectorizer is not None
        analyzer = self._vectorizer.build_analyzer()
        vocabulary = self._vectorizer.vocabulary_

        terms = [t for t in analyzer(query) if not _ENTITY_TOKEN.match(t)]
        if not terms:
            # Nothing but identifiers/stop words -- no retrievable intent.
            return False

        known = sum(1 for t in terms if t in vocabulary)
        return (known / len(terms)) >= MIN_TERM_COVERAGE

    def search(self, query: str, k: int = 3) -> list[dict]:
        """Return the top-k most similar governed passages to `query`.

        The query is embedded with the same TF-IDF model, then ranked in-engine
        with `list_cosine_similarity`. The query vector is passed as a **bound
        parameter** to the ranking statement -- it can never alter the query's
        structure, only supply a value to compare against.
        """
        self._ensure_built()
        assert self._vectorizer is not None

        if not self._has_corpus_coverage(query):
            return []

        query_vec = self._vectorizer.transform([query]).toarray()[0]
        # An all-zero vector means the query shares no vocabulary with the
        # corpus -- there is nothing meaningful to compare, so it is a miss.
        if float((query_vec * query_vec).sum()) == 0.0:
            return []

        rows = self._con.execute(
            """
            SELECT source, title, text_chunk,
                   list_cosine_similarity(embedding, ?::FLOAT[]) AS score
            FROM knowledge_base_vector_index
            WHERE score IS NOT NULL
            ORDER BY score DESC
            LIMIT ?
            """,
            [query_vec.astype(float).tolist(), k],
        ).fetchall()

        results = []
        for source, title, chunk, score in rows:
            if score is None or score < MIN_SCORE:
                continue
            snippet = chunk if len(chunk) <= 320 else chunk[:317].rsplit(" ", 1)[0] + "…"
            results.append(
                {
                    "source": source,
                    "title": title,
                    "snippet": snippet,
                    "score": round(float(score), 4),
                }
            )
        return results

    def index_summary(self) -> list[dict]:
        """Per-document chunk counts, for the control plane's knowledge view."""
        self._ensure_built()
        rows = self._con.execute(
            "SELECT source, COUNT(*) FROM knowledge_base_vector_index GROUP BY source ORDER BY source"
        ).fetchall()
        return [{"source": r[0], "chunks": r[1]} for r in rows]
