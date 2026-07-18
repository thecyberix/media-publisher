"""BM25 retrieval over exported EN↔BG subtitle cue pairs and metadata pairs."""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

DEFAULT_PAIRS_PATH = Path("data/corpus/subtitle_pairs.jsonl")
DEFAULT_INDEX_PATH = Path("data/corpus/bm25_index.json")
DEFAULT_HOLDOUT_PATH = Path("data/corpus/holdout_titles.json")
DEFAULT_METADATA_PAIRS_PATH = Path("data/corpus/metadata_pairs.jsonl")
DEFAULT_METADATA_TITLE_INDEX_PATH = Path("data/corpus/bm25_metadata_title_index.json")
DEFAULT_METADATA_DESCRIPTION_INDEX_PATH = Path(
    "data/corpus/bm25_metadata_description_index.json"
)

MetadataKind = Literal["title", "description"]

TOKEN_RE = re.compile(r"[a-z0-9]+", re.IGNORECASE)
INDEX_VERSION = 1
DEFAULT_K1 = 1.5
DEFAULT_B = 0.75


@dataclass(frozen=True)
class CorpusDoc:
    en: str
    bg: str
    video_title: str
    record_id: str | None = None
    cue_index: int | None = None
    start: str | None = None
    end: str | None = None


@dataclass(frozen=True)
class CorpusHit:
    en: str
    bg: str
    video_title: str
    score: float
    record_id: str | None = None
    cue_index: int | None = None
    start: str | None = None
    end: str | None = None


def tokenize(text: str) -> list[str]:
    return [match.group(0).lower() for match in TOKEN_RE.finditer(text or "")]


def load_holdout_titles(holdout_path: Path = DEFAULT_HOLDOUT_PATH) -> set[str]:
    if not holdout_path.exists():
        return set()
    payload = json.loads(holdout_path.read_text(encoding="utf-8"))
    videos = payload.get("videos") if isinstance(payload, dict) else None
    if not isinstance(videos, list):
        return set()
    titles: set[str] = set()
    for item in videos:
        if not isinstance(item, dict):
            continue
        title = item.get("title")
        if isinstance(title, str) and title.strip():
            titles.add(title.strip())
    return titles


def load_corpus_pairs(
    pairs_path: Path = DEFAULT_PAIRS_PATH,
    *,
    holdout_path: Path = DEFAULT_HOLDOUT_PATH,
    exclude_holdout: bool = True,
) -> list[CorpusDoc]:
    exclude = load_holdout_titles(holdout_path) if exclude_holdout else set()
    if not pairs_path.exists():
        raise FileNotFoundError(f"Corpus pairs not found: {pairs_path}")

    docs: list[CorpusDoc] = []
    for line in pairs_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        en = payload.get("en")
        bg = payload.get("bg")
        title = payload.get("video_title")
        if not isinstance(en, str) or not isinstance(bg, str):
            continue
        en = en.strip()
        bg = bg.strip()
        if not en or not bg:
            continue
        video_title = title.strip() if isinstance(title, str) else ""
        if video_title and video_title in exclude:
            continue
        cue_index = payload.get("cue_index")
        docs.append(
            CorpusDoc(
                en=en,
                bg=bg,
                video_title=video_title,
                record_id=payload.get("record_id")
                if isinstance(payload.get("record_id"), str)
                else None,
                cue_index=int(cue_index) if isinstance(cue_index, int) else None,
                start=payload.get("start") if isinstance(payload.get("start"), str) else None,
                end=payload.get("end") if isinstance(payload.get("end"), str) else None,
            )
        )
    return docs


class Bm25Index:
    """In-memory BM25Okapi over English cue text."""

    def __init__(
        self,
        docs: list[CorpusDoc],
        *,
        k1: float = DEFAULT_K1,
        b: float = DEFAULT_B,
        doc_lens: list[int] | None = None,
        avgdl: float | None = None,
        idf: dict[str, float] | None = None,
        postings: dict[str, list[list[int]]] | None = None,
    ) -> None:
        self.docs = docs
        self.k1 = k1
        self.b = b
        if (
            doc_lens is not None
            and avgdl is not None
            and idf is not None
            and postings is not None
        ):
            self.doc_lens = doc_lens
            self.avgdl = avgdl
            self.idf = idf
            self.postings = postings
            return

        self.doc_lens = []
        df: dict[str, int] = {}
        postings_build: dict[str, list[list[int]]] = {}
        for doc_id, doc in enumerate(docs):
            tokens = tokenize(doc.en)
            self.doc_lens.append(len(tokens))
            tf: dict[str, int] = {}
            for token in tokens:
                tf[token] = tf.get(token, 0) + 1
            for term, count in tf.items():
                df[term] = df.get(term, 0) + 1
                postings_build.setdefault(term, []).append([doc_id, count])

        n_docs = len(docs) or 1
        self.avgdl = (sum(self.doc_lens) / n_docs) if docs else 0.0
        self.idf = {
            term: math.log(1.0 + (n_docs - freq + 0.5) / (freq + 0.5))
            for term, freq in df.items()
        }
        self.postings = postings_build

    def retrieve(self, query_en: str, k: int = 8) -> list[CorpusHit]:
        if k <= 0 or not self.docs:
            return []
        query_tokens = tokenize(query_en)
        if not query_tokens:
            return []

        scores: dict[int, float] = {}
        for term in query_tokens:
            idf = self.idf.get(term)
            if idf is None:
                continue
            for doc_id, tf in self.postings.get(term, []):
                dl = self.doc_lens[doc_id] or 1
                denom = tf + self.k1 * (1.0 - self.b + self.b * dl / (self.avgdl or 1.0))
                scores[doc_id] = scores.get(doc_id, 0.0) + idf * (tf * (self.k1 + 1.0) / denom)

        ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)[:k]
        hits: list[CorpusHit] = []
        for doc_id, score in ranked:
            doc = self.docs[doc_id]
            hits.append(
                CorpusHit(
                    en=doc.en,
                    bg=doc.bg,
                    video_title=doc.video_title,
                    score=score,
                    record_id=doc.record_id,
                    cue_index=doc.cue_index,
                    start=doc.start,
                    end=doc.end,
                )
            )
        return hits

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": INDEX_VERSION,
            "k1": self.k1,
            "b": self.b,
            "avgdl": self.avgdl,
            "doc_lens": self.doc_lens,
            "idf": self.idf,
            "postings": self.postings,
            "docs": [
                {
                    "en": doc.en,
                    "bg": doc.bg,
                    "video_title": doc.video_title,
                    "record_id": doc.record_id,
                    "cue_index": doc.cue_index,
                    "start": doc.start,
                    "end": doc.end,
                }
                for doc in self.docs
            ],
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> Bm25Index:
        if int(payload.get("version", 0)) != INDEX_VERSION:
            raise ValueError(f"Unsupported BM25 index version: {payload.get('version')!r}")
        docs = [
            CorpusDoc(
                en=str(item["en"]),
                bg=str(item["bg"]),
                video_title=str(item.get("video_title") or ""),
                record_id=item.get("record_id")
                if isinstance(item.get("record_id"), str)
                else None,
                cue_index=item.get("cue_index")
                if isinstance(item.get("cue_index"), int)
                else None,
                start=item.get("start") if isinstance(item.get("start"), str) else None,
                end=item.get("end") if isinstance(item.get("end"), str) else None,
            )
            for item in payload.get("docs") or []
            if isinstance(item, dict)
        ]
        return cls(
            docs,
            k1=float(payload.get("k1", DEFAULT_K1)),
            b=float(payload.get("b", DEFAULT_B)),
            doc_lens=[int(x) for x in payload.get("doc_lens") or []],
            avgdl=float(payload.get("avgdl") or 0.0),
            idf={str(k): float(v) for k, v in (payload.get("idf") or {}).items()},
            postings={
                str(term): [[int(doc_id), int(tf)] for doc_id, tf in rows]
                for term, rows in (payload.get("postings") or {}).items()
            },
        )


def build_index(
    pairs_path: Path = DEFAULT_PAIRS_PATH,
    *,
    holdout_path: Path = DEFAULT_HOLDOUT_PATH,
    k1: float = DEFAULT_K1,
    b: float = DEFAULT_B,
) -> Bm25Index:
    docs = load_corpus_pairs(pairs_path, holdout_path=holdout_path, exclude_holdout=True)
    return Bm25Index(docs, k1=k1, b=b)


def save_index(index: Bm25Index, path: Path = DEFAULT_INDEX_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(index.to_dict(), ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def load_index(path: Path = DEFAULT_INDEX_PATH) -> Bm25Index:
    if not path.exists():
        raise FileNotFoundError(f"BM25 index not found: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Invalid BM25 index payload in {path}")
    return Bm25Index.from_dict(payload)


def load_or_build_index(
    *,
    index_path: Path = DEFAULT_INDEX_PATH,
    pairs_path: Path = DEFAULT_PAIRS_PATH,
    holdout_path: Path = DEFAULT_HOLDOUT_PATH,
) -> Bm25Index:
    if index_path.exists():
        return load_index(index_path)
    index = build_index(pairs_path, holdout_path=holdout_path)
    save_index(index, index_path)
    return index


def metadata_index_path_for_kind(kind: MetadataKind) -> Path:
    if kind == "title":
        return DEFAULT_METADATA_TITLE_INDEX_PATH
    if kind == "description":
        return DEFAULT_METADATA_DESCRIPTION_INDEX_PATH
    raise ValueError(f"Unsupported metadata kind: {kind!r}")


def load_metadata_corpus_pairs(
    pairs_path: Path = DEFAULT_METADATA_PAIRS_PATH,
    *,
    kind: MetadataKind,
    holdout_path: Path = DEFAULT_HOLDOUT_PATH,
    exclude_holdout: bool = True,
) -> list[CorpusDoc]:
    exclude = load_holdout_titles(holdout_path) if exclude_holdout else set()
    if not pairs_path.exists():
        raise FileNotFoundError(f"Metadata corpus pairs not found: {pairs_path}")

    docs: list[CorpusDoc] = []
    for line in pairs_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        if payload.get("kind") != kind:
            continue
        en = payload.get("en")
        bg = payload.get("bg")
        title = payload.get("video_title")
        if not isinstance(en, str) or not isinstance(bg, str):
            continue
        en = en.strip()
        bg = bg.strip()
        if not en or not bg:
            continue
        video_title = title.strip() if isinstance(title, str) else ""
        if video_title and video_title in exclude:
            continue
        docs.append(
            CorpusDoc(
                en=en,
                bg=bg,
                video_title=video_title,
                record_id=payload.get("record_id")
                if isinstance(payload.get("record_id"), str)
                else None,
            )
        )
    return docs


def build_metadata_index(
    pairs_path: Path = DEFAULT_METADATA_PAIRS_PATH,
    *,
    kind: MetadataKind,
    holdout_path: Path = DEFAULT_HOLDOUT_PATH,
    k1: float = DEFAULT_K1,
    b: float = DEFAULT_B,
) -> Bm25Index:
    docs = load_metadata_corpus_pairs(
        pairs_path,
        kind=kind,
        holdout_path=holdout_path,
        exclude_holdout=True,
    )
    return Bm25Index(docs, k1=k1, b=b)


def load_or_build_metadata_index(
    kind: MetadataKind,
    *,
    index_path: Path | None = None,
    pairs_path: Path = DEFAULT_METADATA_PAIRS_PATH,
    holdout_path: Path = DEFAULT_HOLDOUT_PATH,
) -> Bm25Index:
    resolved_index = index_path or metadata_index_path_for_kind(kind)
    if resolved_index.exists():
        return load_index(resolved_index)
    index = build_metadata_index(pairs_path, kind=kind, holdout_path=holdout_path)
    save_index(index, resolved_index)
    return index
