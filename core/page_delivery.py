"""Bounded original-page delivery with owned staging and local crash recovery.

No IO occurs on import/construction. The coordinator explicitly calls this
executor. HTTP retries live here, never in the transport adapter. All persisted
errors are fixed codes; headers and exception text never enter the ledger.
"""
from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import sqlite3
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path, PurePosixPath

import requests

from core.http_client import request_get
from core.image_validation import (ImageNeedsVerification, ImageValidationError,
                                   ImageValidationStopped, inspect_image)
from core.library_paths import RootState
from core.library_repository import LibraryError
from core.task_contracts import ContractError, PageSpec, PathRef, canonical_json
from core.task_coordinator import DeliveryOutcome

_BLOCK = 64 * 1024
_MIME = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
         ".gif": "image/gif", ".webp": "image/webp"}
_ALIASES = {"image/jpg": "image/jpeg", "image/pjpeg": "image/jpeg",
            "image/x-png": "image/png", "image/apng": "image/png"}
_BINARY = {"", "application/octet-stream", "binary/octet-stream", "application/binary"}
_ETAG = re.compile(r'^"[\x21\x23-\x7e\x80-\xff]{0,510}"$')
_RANGE = re.compile(r"^bytes ([0-9]+)-([0-9]+)/([0-9]+)$")


@dataclass(frozen=True, slots=True)
class TransferPolicy:
    max_attempts: int = 3
    max_seconds: float = 300
    connect_timeout: float = 5
    read_timeout: float = 15
    max_file_bytes: int = 1 << 32
    checkpoint_bytes: int = 8 * 1024 * 1024
    checkpoint_seconds: float = 5
    retry_delay: float = 0.25

    def __post_init__(self):
        if type(self.max_attempts) is not int or not 1 <= self.max_attempts <= 3:
            raise ValueError("Each explicit retry cycle permits at most three GETs")
        for value in (self.max_seconds, self.connect_timeout, self.read_timeout,
                      self.checkpoint_seconds):
            if not isinstance(value, (int, float)) or not 0 < value <= 86400:
                raise ValueError("Invalid transfer time limit")
        for value in (self.max_file_bytes, self.checkpoint_bytes):
            if type(value) is not int or value <= 0:
                raise ValueError("Invalid transfer byte limit")
        if not isinstance(self.retry_delay, (int, float)) or not 0 <= self.retry_delay <= 60:
            raise ValueError("Invalid retry delay")


class _Fault(Exception):
    def __init__(self, code, *, retry=False, restart=False, block=False):
        self.code, self.retry, self.restart, self.block = code, retry, restart, block
        super().__init__(code)


class _Interrupted(Exception):
    def __init__(self, code="stopped"):
        self.code = code


class _StorageFault(Exception):
    pass


class _StopView:
    def __init__(self, stop, deadline, clock):
        self.stop, self.deadline, self.clock = stop, deadline, clock

    def is_set(self):
        return self.stop.is_set() or self.clock() >= self.deadline

    def check(self):
        if self.stop.is_set():
            raise _Interrupted()
        if self.clock() >= self.deadline:
            raise _Interrupted("transfer_deadline")


@dataclass(slots=True)
class _Stage:
    path: Path
    ref: PathRef
    stream: object
    file_id: dict
    source_identity: str
    hasher: object
    count: int = 0
    total: int | None = None
    etag: str | None = None
    media: str | None = None
    saved_count: int = 0
    saved_at: float = 0

    def close(self):
        if not self.stream.closed:
            self.stream.close()


@dataclass(slots=True)
class _Transfer:
    stage: _Stage | None = None
    restart: bool = False
    fallback_used: bool = False


def _file_id(info):
    return {"device": info.st_dev, "inode": info.st_ino}


def _header(response, name):
    for key, value in response.headers.items():
        if key.lower() == name.lower():
            if not isinstance(value, str) or len(value) > 4096 or "\r" in value or "\n" in value:
                raise _Fault("invalid_response_header")
            return value.strip()
    return ""


def _strong_etag(value):
    return value if isinstance(value, str) and _ETAG.fullmatch(value) else None


def _length(value, maximum):
    if not value:
        return None
    if not re.fullmatch(r"[0-9]{1,20}", value):
        raise _Fault("invalid_content_length", retry=True, restart=True)
    amount = int(value)
    if not 0 < amount <= maximum:
        raise _Fault("content_length_out_of_bounds")
    return amount


def _source_identity(row, page):
    return hashlib.sha256(canonical_json({"illust_id": row["illust_id"],
        "page_index": page.page_index, "url": page.url,
        "original": row["quality"]["original"],
        "validation_policy_version": row["quality"]["validation_policy_version"]}).encode()).hexdigest()


class PageDeliveryExecutor:
    """A thread-safe executor, sharing one cooldown across its page workers."""

    def __init__(self, repository, resolver, *, get=request_get, headers_factory=None,
                 policy=TransferPolicy(), clock=time.monotonic, fault_hook=None,before_request=None):
        self.repository, self.resolver = repository, resolver
        # The root owner must be the same object used to publish files.  The
        # repository cannot infer an output root from its database location.
        bind_roots = getattr(repository, 'bind_path_resolver', None)
        if callable(bind_roots):
            bind_roots(resolver)
        self.get, self.headers_factory, self.policy = get, headers_factory, policy
        self.clock, self.fault_hook = clock, fault_hook
        self.before_request=before_request
        self._cooldown_lock, self._cooldown_until = threading.Lock(), 0.0

    def _db(self, method, *args, **kwargs):
        try:
            return getattr(self.repository, method)(*args, **kwargs)
        except (LibraryError, sqlite3.Error, OSError, TimeoutError) as exc:
            raise _StorageFault() from exc

    def _hook(self, stage, row):
        if self.fault_hook:
            self.fault_hook(stage, row)

    def _checkpoint(self, row, stage):
        stage.stream.flush()
        os.fsync(stage.stream.fileno())
        checkpoint = {"schema_version": 1, "delivery_id": row["delivery_id"],
            "temp_ref": asdict(stage.ref), "file_id": stage.file_id,
            "source_identity": stage.source_identity, "strong_etag": stage.etag,
            "expected_total": stage.total, "verified_bytes": stage.count,
            "prefix_sha256": stage.hasher.hexdigest(), "media_type": stage.media,
            "validation_policy_version": 1}
        self._db("save_transfer_checkpoint", row["delivery_id"], row["current_batch_id"], checkpoint)
        stage.saved_count, stage.saved_at = stage.count, self.clock()

    def _new_stage(self, row, final_ref, source):
        final = self.resolver.resolve(final_ref)
        # A bound root that disappeared (unplugged volume, moved library,
        # deleted project root) is not a directory to recreate.  Only a live
        # root may gain its first output subdirectory.
        if self.resolver.root_state(final_ref) is not RootState.AVAILABLE:
            raise _Fault("root_unavailable", block=True)
        final.parent.mkdir(parents=True, exist_ok=True)
        self.resolver.resolve(final_ref)  # reject a replaced parent/link before creating
        descriptor, raw = tempfile.mkstemp(prefix=f".{row['delivery_id']}.", suffix=".part", dir=final.parent)
        stream = os.fdopen(descriptor, "r+b", buffering=0)
        path = Path(raw)
        try:
            ref = replace(final_ref, relative_path=str(PurePosixPath(final_ref.relative_path).parent / path.name))
            self.resolver.resolve(ref)
            stage = _Stage(path, ref, stream, _file_id(os.fstat(stream.fileno())), source,
                           hashlib.sha256(), saved_at=self.clock())
            if not stage.file_id["inode"]:
                raise _Fault("file_identity_unavailable", block=True)
            self._hook("staging_created", row)
            return stage
        except BaseException:
            stream.close()
            raise  # an unregistered file is retained, never guessed/deleted later

    def _resume_stage(self, row, final_ref, source, stop):
        checkpoint = row.get("checkpoint")
        if not isinstance(checkpoint, dict) or checkpoint.get("schema_version") != 1:
            return None
        try:
            if checkpoint["delivery_id"] != row["delivery_id"] or checkpoint["source_identity"] != source:
                return None
            ref = PathRef(**checkpoint["temp_ref"])
            final = self.resolver.resolve(final_ref)
            path = self.resolver.resolve(ref)
            if path.parent != final.parent or path == final or row["delivery_id"] not in path.name or not path.name.endswith(".part"):
                return None
            count, total = checkpoint["verified_bytes"], checkpoint["expected_total"]
            if type(count) is not int or not 0 <= count <= self.policy.max_file_bytes:
                return None
            if total is not None and (type(total) is not int or not count <= total <= self.policy.max_file_bytes):
                return None
            etag = _strong_etag(checkpoint.get("strong_etag"))
            if not etag and not (count and count == total):
                return None
            stream = path.open("r+b", buffering=0)
        except (OSError, KeyError, TypeError, ValueError, ContractError):
            return None
        try:
            info = os.fstat(stream.fileno())
            if _file_id(info) != checkpoint["file_id"] or not info.st_ino or info.st_size < count:
                stream.close()
                return None
            hasher, remaining = hashlib.sha256(), count
            while remaining:
                stop.check()
                data = stream.read(min(_BLOCK, remaining))
                if not data:
                    stream.close()
                    return None
                hasher.update(data)
                remaining -= len(data)
            if hasher.hexdigest() != checkpoint["prefix_sha256"]:
                stream.close()
                return None
            # Only the registered inode with a rechecked prefix may lose its
            # unconfirmed tail. Other old/altered files are preserved.
            if info.st_size > count:
                stream.truncate(count)
                stream.flush()
                os.fsync(stream.fileno())
            stream.seek(count)
            return _Stage(path, ref, stream, checkpoint["file_id"], source, hasher,
                          count, total, etag, checkpoint.get("media_type"), count, self.clock())
        except BaseException:
            stream.close()
            raise

    def _wait(self, seconds, stop):
        until = self.clock() + seconds
        while self.clock() < until:
            stop.check()
            stop.stop.wait(min(0.1, until - self.clock()))
        stop.check()

    def _wait_cooldown(self, stop):
        while True:
            with self._cooldown_lock:
                delay = max(0, self._cooldown_until - self.clock())
            if not delay:
                return
            self._wait(delay, stop)

    def _rate_limit(self, response):
        value = _header(response, "Retry-After")
        delay = 30.0
        if value.isascii() and value.isdecimal() and len(value) <= 9:
            delay = float(value)
        elif value:
            try:
                date = parsedate_to_datetime(value)
                if date.tzinfo is None:
                    date = date.replace(tzinfo=timezone.utc)
                delay = max(0, (date - datetime.now(timezone.utc)).total_seconds())
            except (TypeError, ValueError, OverflowError):
                pass
        with self._cooldown_lock:
            self._cooldown_until = max(self._cooldown_until, self.clock() + max(1, delay))

    def _response_plan(self, response, stage, ranged):
        status = response.status_code
        if status in {429, 503}:
            self._rate_limit(response)
            raise _Fault("server_cooldown", retry=True)
        if status in {401, 403}:
            raise _Fault("image_access_denied", block=True)
        if status == 404:
            raise _Fault("image_unavailable")
        if status in {408, 500, 502, 504}:
            raise _Fault("http_transient", retry=True)
        if status == 416:
            raise _Fault("range_unsatisfied", retry=True, restart=True)
        if status not in {200, 206}:
            raise _Fault("unexpected_http_status")
        if _header(response, "Content-Encoding").lower() not in {"", "identity"}:
            raise _Fault("unexpected_content_encoding")
        content_type = _header(response, "Content-Type").split(";", 1)[0].strip().lower()
        content_type = _ALIASES.get(content_type, content_type)
        if content_type not in _BINARY and not content_type.startswith("image/"):
            raise _Fault("non_image_content_type")
        length = _length(_header(response, "Content-Length"), self.policy.max_file_bytes)
        if length and _header(response, "Transfer-Encoding"):
            raise _Fault("ambiguous_transfer_length")
        etag = _strong_etag(_header(response, "ETag"))
        range_header = _header(response, "Content-Range")
        if status == 200:
            if range_header:
                raise _Fault("unexpected_content_range", retry=True, restart=True)
            return length, length, content_type, etag, False
        if not ranged or not stage:
            raise _Fault("unsolicited_partial_response", retry=True, restart=True)
        match = _RANGE.fullmatch(range_header)
        if not match or content_type.startswith("multipart/"):
            raise _Fault("invalid_content_range", retry=True, restart=True)
        start, end, total = map(int, match.groups())
        if (start != stage.count or not start <= end < total <= self.policy.max_file_bytes
                or (stage.total is not None and total != stage.total)
                or etag != stage.etag or etag is None):
            raise _Fault("range_version_or_offset_mismatch", retry=True, restart=True)
        segment = end - start + 1
        if length is not None and length != segment:
            raise _Fault("range_length_mismatch", retry=True, restart=True)
        return segment, total, content_type, etag, True

    def _transfer_attempt(self, row, page, final_ref, source, transfer, stop):
        stage = transfer.stage
        ranged = bool(stage and stage.count and stage.etag and not transfer.restart)
        headers = dict(self.headers_factory(page.url)) if self.headers_factory else {}
        # The resolver supplies headers only for this validated CDN URL. We
        # refuse redirects, and overwrite all caller-supplied range/encoding.
        headers = {k:v for k,v in headers.items() if k.lower() not in {"range", "if-range", "accept-encoding"}}
        headers.update({"Accept-Encoding": "identity", "Referer": "https://www.pixiv.net/"})
        if ranged:
            headers.update({"Range": f"bytes={stage.count}-", "If-Range": stage.etag})
        self._wait_cooldown(stop)
        if self.before_request is not None:
            self.before_request(stop)
        stop.check()
        self._db("start_transfer_attempt", row["delivery_id"], row["current_batch_id"], limit=self.policy.max_attempts)
        remaining = max(0.001, stop.deadline - self.clock())
        response = self.get(page.url, headers=headers, stream=True, allow_redirects=False,
                            timeout=(min(self.policy.connect_timeout, remaining), min(self.policy.read_timeout, remaining)))
        try:
            segment, total, media, etag, append = self._response_plan(response, stage, ranged)
            stop.check()
            if not append:
                if stage:
                    stage.close()
                stage = transfer.stage = self._new_stage(row, final_ref, source)
                transfer.restart = False
            stage.total, stage.etag = total, etag
            stage.media = media if media in set(_MIME.values()) else None
            self._checkpoint(row, stage)
            received, prefix = 0, bytearray()
            for data in response.iter_content(chunk_size=_BLOCK):
                stop.check()
                if not data:
                    continue
                if not isinstance(data, bytes) or len(data) > _BLOCK:
                    raise _Fault("transport_chunk_limit")
                if received + len(data) > (segment if segment is not None else self.policy.max_file_bytes):
                    raise _Fault("response_body_too_long", retry=True, restart=True)
                if stage.count + len(data) > self.policy.max_file_bytes:
                    raise _Fault("image_size_limit")
                if not append and len(prefix) < 64:
                    prefix.extend(data[:64-len(prefix)])
                    if bytes(prefix).lstrip().startswith((b"<", b"{", b"[")):
                        raise _Fault("image_error_body")
                view = memoryview(data)
                while view:
                    stop.check()
                    written = stage.stream.write(view)
                    if not written:
                        raise OSError(errno.ENOSPC, "short file write")
                    stage.hasher.update(view[:written])
                    stage.count += written
                    received += written
                    view = view[written:]
                if (stage.count - stage.saved_count >= self.policy.checkpoint_bytes
                        or self.clock() - stage.saved_at >= self.policy.checkpoint_seconds):
                    self._checkpoint(row, stage)
            self._checkpoint(row, stage)
            if segment is not None and received != segment:
                raise _Fault("response_body_truncated", retry=True)
            if stage.total is not None and stage.count != stage.total:
                raise _Fault("partial_range_remaining", retry=True)
            if stage.count == 0:
                raise _Fault("empty_image", retry=True, restart=True)
            return media
        except (requests.RequestException, _Interrupted, _Fault):
            if stage and not stage.stream.closed:
                self._checkpoint(row, stage)
            raise
        finally:
            response.close()

    def _verify_owned(self, path, evidence, *, hash_content=True):
        info = path.stat()
        if _file_id(info) != evidence["file_id"] or not info.st_ino or info.st_size != evidence["size"]:
            raise _Fault("published_file_identity_mismatch", block=True)
        if hash_content:
            with path.open("rb") as stream:
                before = os.fstat(stream.fileno())
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
                after = os.fstat(stream.fileno())
            if (_file_id(before) != evidence["file_id"] or before.st_size != after.st_size
                    or before.st_mtime_ns != after.st_mtime_ns or digest != evidence["sha256"]):
                raise _Fault("published_file_content_mismatch", block=True)

    def _publish(self, row, evidence):
        final_ref, temp_ref = PathRef(**evidence["final_ref"]), PathRef(**evidence["temp_ref"])
        final, temp = self.resolver.resolve(final_ref), self.resolver.resolve(temp_ref)
        if final.parent != temp.parent or final == temp or row["delivery_id"] not in temp.name:
            raise _Fault("invalid_staging_identity", block=True)
        self._hook("before_publish", row)
        if not final.exists():
            # UA-11: the exclusion linearizes here, immediately before the
            # final name exists.  A denied gate leaves the staged temp owned by
            # this delivery and cancels the row; it never removes a published
            # file and never reports success.
            decision = self._db("publish_authorized", row["delivery_id"])
            if not isinstance(decision, dict) or decision.get("allowed") is not True:
                return self._abort_unpublished(row, evidence, decision)
            self._verify_owned(temp, evidence)
            try:
                os.link(temp, final)  # atomic, same-directory, refuses existing names
            except FileExistsError:
                pass  # race: still must prove the final inode belongs to this delivery
        self._hook("after_publish", row)
        self.resolver.resolve(final_ref)
        self._verify_owned(final, evidence, hash_content=False)
        self._hook("before_confirm", row)
        self.resolver.resolve(final_ref)
        self._verify_owned(final, evidence)
        artifact = self._db("confirm_delivery", row["delivery_id"])
        self._hook("after_confirm", row)
        try:
            self._hook("before_cleanup", row)
            self.cleanup_delivery(row["delivery_id"])
            self._hook("after_cleanup", row)
        except (OSError, _Fault, _StorageFault, ContractError):
            pass  # durable success remains success; the owned .part is recoverable
        return artifact

    def _abort_unpublished(self, row, evidence, decision):
        """Cancel a delivery whose final name was never created.

        The staged file is removed only when it is provably this delivery's
        own temp (identity, size and digest all verified).  Any doubt keeps
        the '.part' for local recovery instead of guessing.
        """
        reason = "intent_excluded"
        if isinstance(decision, dict) and decision.get("reason") == "intent_unreadable":
            reason = "intent_unreadable"
        self._db(
            "cancel_unpublished_delivery",
            row["delivery_id"],
            row["current_batch_id"],
            error_code=reason,
        )
        temp = self.resolver.resolve(PathRef(**evidence["temp_ref"]))
        if temp.exists():
            try:
                self._verify_owned(temp, evidence)
                temp.unlink()
            except (OSError, _Fault, ContractError):
                pass  # an unverified leftover stays recoverable
        return ("cancelled", reason)

    def cleanup_delivery(self, delivery_id):
        """Explicit local cleanup, safe even after a crash just after confirmation."""
        row = self._db("get_delivery", delivery_id)
        evidence = row.get("prepared")
        if row["state"] != "completed" or not isinstance(evidence, dict):
            return False
        final = self.resolver.resolve(PathRef(**evidence["final_ref"]))
        temp = self.resolver.resolve(PathRef(**evidence["temp_ref"]))
        if temp.parent != final.parent or temp == final or not temp.exists():
            return False
        self._verify_owned(final, evidence)
        self._verify_owned(temp, evidence)
        temp.unlink()
        return True

    def _finish_issue(self, row, stop, outcome, *, block=False):
        delivery_id, batch = row["delivery_id"], row["current_batch_id"]
        if outcome.state in {"needs_verification", "recovery_needed"}:
            self._db("set_delivery_issue", delivery_id, batch, state=outcome.state, error_code=outcome.error_code)
        else:
            self._db("release_delivery", delivery_id, batch, state=outcome.state, error_code=outcome.error_code)
        if block:
            stop.set()
            self._db("request_block", batch, outcome.error_code)
        return outcome

    def __call__(self, row, stop_event):
        transfer = _Transfer()
        stop = _StopView(stop_event, self.clock() + self.policy.max_seconds, self.clock)
        prepared = False
        try:
            if row["state"] == "completed":
                return DeliveryOutcome("completed")
            stop.check()
            raw_page = row["payload"]["page"]
            final_ref = PathRef(**row["output_ref"])
            page = PageSpec(raw_page["page_index"], final_ref, raw_page["url"], raw_page.get("expected_size"))
            if row["quality"]["original"] and "/img-original/" not in page.url.split("?", 1)[0]:
                raise _Fault("original_url_required")
            if self.resolver.root_state(final_ref) is RootState.MISSING_BOUND:
                # Refuse before the first GET: the volume that holds the
                # library is gone, not a directory to create.
                raise _Fault("root_unavailable", block=True)
            if self.resolver.resolve(final_ref).exists():
                raise _Fault("output_file_conflict", block=True)
            source = _source_identity(row, page)
            transfer.stage = self._resume_stage(row, final_ref, source, stop)
            last_error = "transfer_attempts_exhausted"
            for attempt in range(self.policy.max_attempts + 1):
                stop.check()
                current = self._db("get_delivery", row["delivery_id"])
                stage = transfer.stage
                complete = stage and stage.total is not None and stage.count == stage.total
                if not complete and current["http_attempts"] >= self.policy.max_attempts:
                    break
                try:
                    if complete:
                        media = stage.media or ""
                    else:
                        if stage and (transfer.restart or not stage.etag):
                            stage.close()
                            transfer.stage = None
                        media = self._transfer_attempt(row, page, final_ref, source, transfer, stop)
                    stage = transfer.stage
                    stage.close()
                    self._db("begin_validation", row["delivery_id"], row["current_batch_id"])
                    evidence = inspect_image(stage.path, expected_size=stage.total,
                        expected_media_type=media or None, stop_event=stop, max_file_bytes=self.policy.max_file_bytes)
                    if _MIME.get(self.resolver.resolve(final_ref).suffix.lower()) != evidence.media_type:
                        raise ImageNeedsVerification("image_extension_mismatch")
                    prepared_evidence = {"schema_version": 1, "delivery_id": row["delivery_id"],
                        "temp_ref": asdict(stage.ref), "final_ref": asdict(final_ref), "file_id": stage.file_id,
                        "source_identity": source, "validation_policy_version": 1, **asdict(evidence)}
                    self._verify_owned(stage.path, prepared_evidence, hash_content=False)
                    stop.check()
                    self._hook("before_prepare", row)
                    self._db("prepare_delivery", row["delivery_id"], row["current_batch_id"], prepared_evidence)
                    prepared = True
                    self._hook("after_prepare", row)
                    published = self._publish(row, prepared_evidence)
                    if isinstance(published, tuple):
                        return DeliveryOutcome(*published)
                    return DeliveryOutcome("completed")
                except ImageNeedsVerification as exc:
                    return self._finish_issue(row, stop_event, DeliveryOutcome("needs_verification", exc.code))
                except ImageValidationError as exc:
                    if exc.code == "image_media_type_mismatch":
                        return self._finish_issue(row, stop_event, DeliveryOutcome("needs_verification", exc.code))
                    last_error, transfer.restart = exc.code, True
                    if transfer.stage:
                        transfer.stage.close()
                    transfer.stage = None
                except requests.RequestException:
                    last_error = "image_network_error"
                except _Fault as exc:
                    if not exc.retry or (exc.restart and transfer.fallback_used):
                        raise
                    last_error = exc.code
                    if exc.restart:
                        transfer.restart, transfer.fallback_used = True, True
                        if transfer.stage:
                            transfer.stage.close()
                        transfer.stage = None
                if self._db("get_delivery", row["delivery_id"])["http_attempts"] >= self.policy.max_attempts:
                    break
                self._wait(self.policy.retry_delay, stop)
            return self._finish_issue(row, stop_event, DeliveryOutcome("failed", last_error))
        except (ImageValidationStopped, _Interrupted) as exc:
            code = getattr(exc, "code", "stopped")
            if not stop_event.is_set() and self.clock() >= stop.deadline:
                code = "transfer_deadline"
            return self._finish_issue(row, stop_event, DeliveryOutcome("suspended", code), block=code == "transfer_deadline")
        except _Fault as exc:
            state = "recovery_needed" if prepared else ("suspended" if exc.block else "failed")
            return self._finish_issue(row, stop_event, DeliveryOutcome(state, exc.code), block=exc.block or prepared)
        except (OSError, ContractError) as exc:
            code = "disk_full" if isinstance(exc, OSError) and exc.errno == errno.ENOSPC else "file_access_failed"
            state = "recovery_needed" if prepared else "suspended"
            return self._finish_issue(row, stop_event, DeliveryOutcome(state, code), block=True)
        except _StorageFault:
            # No successful result can escape an uncommitted DB operation.
            # Keep prepared/bytes intact; coordinator will stop on the row.
            stop_event.set()
            return DeliveryOutcome("recovery_needed" if prepared else "blocked", "record_write_failed")
        finally:
            if transfer.stage:
                transfer.stage.close()

    def recover_local(self, *, limit=200):
        """Reconcile a bounded batch of prepared files, with zero HTTP requests."""
        results = []
        for row in self._db("list_recoverable", limit=limit):
            evidence = row.get("prepared")
            try:
                if not isinstance(evidence, dict):
                    raise _Fault("prepared_evidence_missing", block=True)
                published = self._publish(row, evidence)
                if isinstance(published, tuple):
                    results.append((row["delivery_id"], published[0]))
                else:
                    results.append((row["delivery_id"], "completed"))
            except (_Fault, OSError, ContractError):
                self._db("set_delivery_issue", row["delivery_id"], row["current_batch_id"],
                         state="recovery_needed", error_code="local_recovery_conflict")
                results.append((row["delivery_id"], "recovery_needed"))
        return tuple(results)
