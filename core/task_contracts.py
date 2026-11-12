"""Immutable, versioned inputs for the new collection core; no I/O or credentials."""
from __future__ import annotations

import hashlib
import json
import re
import string
from dataclasses import asdict, dataclass, field
from pathlib import PureWindowsPath
from urllib.parse import urlsplit


class ContractError(ValueError):
    """An input cannot be safely persisted or used by the collection core."""


def _text(value: object, name: str, maximum: int = 256, *, empty: bool = False) -> str:
    if not isinstance(value, str) or len(value) > maximum or (not empty and not value):
        raise ContractError(f"Invalid {name}")
    if any(ord(char) < 32 for char in value):
        raise ContractError(f"Control character in {name}")
    return value


def _integer(value: object, name: str, minimum: int = 0, maximum: int = 1_000_000_000) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ContractError(f"Invalid {name}")
    return value


def canonical_json(value: object) -> str:
    """Strict JSON with bounded size and a ban on credential-bearing fields."""
    def inspect(item, depth=0):
        if depth > 20:
            raise ContractError("JSON nesting limit exceeded")
        if item is None or type(item) in (str, int, bool):
            return
        if isinstance(item, dict):
            for key, child in item.items():
                if not isinstance(key, str):
                    raise ContractError("JSON keys must be strings")
                normalized = key.lower().replace("-", "_")
                if normalized in {"cookie", "cookies", "authorization", "token", "access_token",
                                  "refresh_token", "password", "api_key", "credentials"}:
                    raise ContractError("Credentials must stay outside task payloads")
                inspect(child, depth + 1)
        elif isinstance(item, (list, tuple)):
            for child in item:
                inspect(child, depth + 1)
        else:
            raise ContractError("Unsupported JSON value")
    inspect(value)
    result = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    try:
        encoded_size = len(result.encode("utf-8"))
    except UnicodeError as exc:
        raise ContractError("Invalid Unicode in task JSON") from exc
    if encoded_size > 8 * 1024 * 1024:
        raise ContractError("Task payload exceeds 8 MiB")
    return result


def normalize_json(value: str, *, object_only: bool = False) -> str:
    if not isinstance(value, str):
        raise ContractError("Expected a JSON string")
    try:
        if len(value.encode("utf-8")) > 8 * 1024 * 1024:
            raise ContractError("Task payload exceeds 8 MiB")
    except UnicodeError as exc:
        raise ContractError("Invalid Unicode in task JSON") from exc
    try:
        def pairs(items):
            result = {}
            for key, child in items:
                if key in result:
                    raise ContractError("Duplicate JSON key")
                result[key] = child
            return result
        decoded = json.loads(value, object_pairs_hook=pairs)
    except (ValueError, TypeError, RecursionError) as exc:
        raise ContractError("Invalid JSON payload") from exc
    if object_only and not isinstance(decoded, dict):
        raise ContractError("Expected a JSON object")
    return canonical_json(decoded)


@dataclass(frozen=True, slots=True)
class PathRef:
    kind: str = "project"
    root_id: str = "project"
    relative_path: str = "downl_Pixiv"

    def __post_init__(self):
        if self.kind not in {"project", "external"}:
            raise ContractError("Invalid path kind")
        if not isinstance(self.root_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", self.root_id):
            raise ContractError("Invalid path root identity")
        if self.kind == "project" and self.root_id != "project":
            raise ContractError("Project paths use the stable project root identity")
        raw = _text(self.relative_path, "relative path", 2048).replace("\\", "/")
        if PureWindowsPath(raw).anchor or raw.startswith("/"):
            raise ContractError("Expected a relative path")
        if self.kind == "external" and raw == ".":
            # An explicitly mapped external directory can itself be the
            # selected output root. Individual files must still be children.
            object.__setattr__(self, "relative_path", raw)
            return
        pieces = raw.split("/")
        for piece in pieces:
            if piece in {"", ".", ".."} or piece.endswith((".", " ")) or any(c in piece for c in '<>:"|?*'):
                raise ContractError("Unsafe path component")
            if re.fullmatch(r"(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", piece, re.IGNORECASE):
                raise ContractError("Windows device path is forbidden")
        object.__setattr__(self, "relative_path", raw)

    @property
    def target_key(self) -> str:
        return hashlib.sha256(canonical_json([self.kind, self.root_id.casefold(), self.relative_path.casefold()]).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class FilterSpec:
    min_bookmarks: int | None = None
    exclude_ai: bool = False

    def __post_init__(self):
        if self.min_bookmarks is not None:
            _integer(self.min_bookmarks, "minimum bookmarks")
        if type(self.exclude_ai) is not bool:
            raise ContractError("exclude_ai must be boolean")


@dataclass(frozen=True, slots=True)
class BatchBudget:
    max_works: int = 100
    max_requests: int = 80
    max_artists: int = 30
    max_seconds: int | None = None

    def __post_init__(self):
        for name in ("max_works", "max_requests", "max_artists"):
            _integer(getattr(self, name), name, 1, 1_000_000)
        if self.max_seconds is not None:
            _integer(self.max_seconds, "max_seconds", 1, 604800)


@dataclass(frozen=True, slots=True)
class QuerySpec:
    source: str = "follow"
    mode: str = "history"
    parameters_json: str = "{}"
    requested_sort: str = "source_default"
    filters: FilterSpec = field(default_factory=FilterSpec)

    def __post_init__(self):
        if self.source not in {"follow", "artist", "ranking", "search", "recommendations", "intersection"}:
            raise ContractError("Unknown source")
        if self.mode not in ({"history", "new"} if self.source == "follow" else {"query"}):
            raise ContractError("Invalid source mode")
        _text(self.requested_sort, "requested sort", 64)
        if not isinstance(self.filters, FilterSpec):
            raise ContractError("Invalid filters")
        object.__setattr__(self, "parameters_json", normalize_json(self.parameters_json, object_only=True))


@dataclass(frozen=True, slots=True)
class QualitySpec:
    original: bool = True
    all_pages: bool = True
    validation_policy_version: int = 1

    def __post_init__(self):
        if type(self.original) is not bool or type(self.all_pages) is not bool:
            raise ContractError("Quality flags must be boolean")
        if type(self.validation_policy_version) is not int or self.validation_policy_version != 1:
            raise ContractError("Unsupported validation policy")


@dataclass(frozen=True, slots=True)
class OutputSpec:
    root_ref: PathRef = field(default_factory=PathRef)
    folder_by_artist: bool = True
    name_template: str = "{illust_id}_p{page_index}"

    def __post_init__(self):
        if not isinstance(self.root_ref, PathRef) or type(self.folder_by_artist) is not bool:
            raise ContractError("Invalid output specification")
        _text(self.name_template, "name template", 200)
        try:
            for _, name, format_spec, conversion in string.Formatter().parse(self.name_template):
                if name is not None and (name not in {"illust_id", "page_index", "artist_id", "artist_name", "title", "id", "page", "user_id", "user", "date"} or format_spec or conversion):
                    raise ContractError("Unsupported name template field")
            rendered = self.name_template.format(illust_id="1", page_index=0, artist_id="2", artist_name="artist", title="title",
                                                 id="1", page=0, user_id="2", user="artist", date="20261023")
            PathRef(relative_path=rendered + ".jpg")
            if "/" in rendered or "\\" in rendered:
                raise ContractError("Name template must not create directories")
        except (ValueError, KeyError, IndexError) as exc:
            raise ContractError("Invalid name template") from exc


@dataclass(frozen=True, slots=True)
class CollectionRequest:
    """Collection intent, distinct from the legacy HTTP DownloadRequest."""
    request_id: str
    account_ref: str
    query: QuerySpec = field(default_factory=QuerySpec)
    output: OutputSpec = field(default_factory=OutputSpec)
    quality: QualitySpec = field(default_factory=QualitySpec)
    budget: BatchBudget = field(default_factory=BatchBudget)
    dedup_policy: str = "trusted"
    schema_version: int = 1

    def __post_init__(self):
        _text(self.request_id, "request ID", 128)
        _text(self.account_ref, "non-secret account reference", 128)
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ContractError("Unsupported request version")
        if self.dedup_policy not in {"trusted", "redownload"}:
            raise ContractError("Invalid deduplication policy")
        for name, kind in (("query", QuerySpec), ("output", OutputSpec), ("quality", QualitySpec), ("budget", BatchBudget)):
            if not isinstance(getattr(self, name), kind):
                raise ContractError(f"Invalid {name}")

    def to_json(self) -> str:
        return canonical_json(asdict(self))

    @classmethod
    def from_json(cls, text: str) -> CollectionRequest:
        payload = json.loads(normalize_json(text, object_only=True))
        try:
            query = dict(payload.pop("query"))
            query["filters"] = FilterSpec(**query["filters"])
            output = dict(payload.pop("output"))
            output["root_ref"] = PathRef(**output["root_ref"])
            quality = QualitySpec(**payload.pop("quality"))
            budget = BatchBudget(**payload.pop("budget"))
            return cls(**payload, query=QuerySpec(**query), output=OutputSpec(**output),
                       quality=quality, budget=budget)
        except (KeyError, TypeError, ValueError) as exc:
            raise ContractError("Invalid collection request") from exc


@dataclass(frozen=True, slots=True)
class PageSpec:
    page_index: int
    output_ref: PathRef
    url: str
    expected_size: int | None = None

    def __post_init__(self):
        _integer(self.page_index, "page index", 0, 9999)
        if not isinstance(self.output_ref, PathRef):
            raise ContractError("Invalid page output reference")
        if self.output_ref.relative_path == ".":
            raise ContractError("A page must name a file below its output root")
        try:
            parsed = urlsplit(_text(self.url, "original URL", 4096))
            if parsed.port not in {None, 443}:
                raise ContractError("Unsupported source URL port")
        except ValueError as exc:
            raise ContractError("Invalid source URL") from exc
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
            raise ContractError("Invalid source URL")
        if parsed.hostname != "pximg.net" and not parsed.hostname.endswith(".pximg.net"):
            raise ContractError("Original URL must use the Pixiv image host")
        if self.expected_size is not None:
            _integer(self.expected_size, "expected size", 1, 1 << 40)


@dataclass(frozen=True, slots=True)
class CandidateDecision:
    illust_id: str
    decision: str
    artist_id: str = ""
    artist_name: str = ""
    title: str = ""
    page_count: int | None = None
    pages: tuple[PageSpec, ...] = ()
    bookmarks: int | None = None
    ai_flag: bool | None = None
    reason_codes: tuple[str, ...] = ()
    provenance_json: str = "{}"

    def __post_init__(self):
        if not isinstance(self.illust_id, str) or not re.fullmatch(r"[0-9]{1,32}", self.illust_id):
            raise ContractError("Invalid artwork ID")
        if self.decision not in {"download", "filtered", "metadata_unknown", "unavailable", "unsupported", "skipped_existing"}:
            raise ContractError("Invalid candidate decision")
        for name in ("artist_id", "artist_name", "title"):
            _text(getattr(self, name), name, 4096, empty=True)
        if self.page_count is not None:
            _integer(self.page_count, "page count", 1, 10000)
        if self.bookmarks is not None:
            _integer(self.bookmarks, "bookmarks")
        if self.ai_flag is not None and type(self.ai_flag) is not bool:
            raise ContractError("AI flag must be boolean or unknown")
        pages = tuple(self.pages)
        if not all(isinstance(page, PageSpec) for page in pages):
            raise ContractError("Invalid page specification")
        if len({page.page_index for page in pages}) != len(pages):
            raise ContractError("Duplicate required page")
        if self.decision == "download":
            if self.page_count is None or not pages or any(page.page_index >= self.page_count for page in pages):
                raise ContractError("Download decisions need known required pages")
        elif pages:
            raise ContractError("Only download decisions may contain page tasks")
        reasons = tuple(self.reason_codes)
        for reason in reasons:
            _text(reason, "reason code", 128)
        object.__setattr__(self, "pages", pages)
        object.__setattr__(self, "reason_codes", reasons)
        object.__setattr__(self, "provenance_json", normalize_json(self.provenance_json, object_only=True))


@dataclass(frozen=True, slots=True)
class SourcePage:
    source_id: str
    pass_id: str
    entity_id: str
    page_identity: str
    candidates: tuple[CandidateDecision, ...]
    cursor_before_json: str = "null"
    cursor_after_json: str = "null"
    exhausted: bool = False
    actual_sort: str = "source_default"
    coverage_json: str = "{}"

    def __post_init__(self):
        for name in ("source_id", "pass_id", "entity_id", "page_identity", "actual_sort"):
            _text(getattr(self, name), name, 256)
        candidates = tuple(self.candidates)
        if len(candidates) > 2000 or not all(isinstance(c, CandidateDecision) for c in candidates):
            raise ContractError("Invalid or oversized source page")
        if len({c.illust_id for c in candidates}) != len(candidates):
            raise ContractError("Duplicate artwork in a source page")
        if type(self.exhausted) is not bool:
            raise ContractError("Invalid exhaustion flag")
        object.__setattr__(self, "candidates", candidates)
        for name in ("cursor_before_json", "cursor_after_json"):
            object.__setattr__(self, name, normalize_json(getattr(self, name)))
        object.__setattr__(self, "coverage_json", normalize_json(self.coverage_json, object_only=True))
        canonical_json(asdict(self))
