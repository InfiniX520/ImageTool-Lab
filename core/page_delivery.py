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

from core.file_presence import Presence, probe_ref
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


@dataclass(frozen=True, slots=True)
class _Discard:
    """Result of one attempt to delete a delivery-owned staged file."""

    deleted: bool
    code: str


def _presence_error(presence, target):
    """Map an unobservable entry to the fixed diagnostic code it deserves."""
    if presence.state is Presence.ROOT_UNAVAILABLE:
        return "root_unavailable"
    return "final_unreadable" if target == "final" else "temp_unreadable"


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
        # Fair rotation for the bounded cancelled-temp retry queue (RFA-03):
        # one cursor for never-attempted rows, one for rows already retried.
        self._cleanup_cursor = 0
        self._cleanup_retry_cursor = 0

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
        if self.resolver.root_state(final_ref) is not RootState.AVAILABLE:
            # The volume that holds the gallery is gone: neither "published"
            # nor "unpublished" is provable, so the row stays recoverable and
            # the round stops instead of retrying a dead root.
            raise _Fault("root_unavailable", block=True)
        self._hook("before_publish", row)
        final_presence = probe_ref(self.resolver, final_ref)
        if final_presence.state is Presence.ABSENT:
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
        elif final_presence.state is Presence.PRESENT:
            # Published wins: a later exclusion only affects future work, and
            # the bytes are confirmed through the normal path below.
            pass
        elif final_presence.state is Presence.UNREADABLE:
            # A reachable root whose final entry cannot be read is not proof of
            # an unpublished delivery.  Keep the staged bytes for a retry.
            raise _Fault("final_unreadable", block=True)
        else:
            # The root itself became unreachable between the gate and the probe.
            raise _Fault("root_unavailable", block=True)
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

        Presence is four-state on purpose: an unreachable root is not proof
        that the final name is absent, and a cancelled row is not proof that a
        staged file was ours.  Only a reachable root with a provably absent
        final name may lead to cancellation (RFA-02), and the staged file is
        verified again after the writer round-trip before it is removed
        (RFA-06).
        """
        # Unknown or malformed authorization is treated as unreadable: keep
        # the staged bytes instead of assuming an exclusion that never existed.
        reason = "intent_unreadable"
        if isinstance(decision, dict) and decision.get("reason") == "intent_excluded":
            reason = "intent_excluded"
        if reason == "intent_unreadable":
            # An unreadable intent is not permission to discard staged bytes:
            # keep prepared/.part and park the delivery for an explicit review.
            self._db(
                "set_delivery_issue",
                row["delivery_id"],
                row["current_batch_id"],
                state="recovery_needed",
                error_code=reason,
            )
            return ("recovery_needed", reason)
        final_presence = probe_ref(self.resolver, PathRef(**evidence["final_ref"]))
        if final_presence.state is Presence.PRESENT:
            # A published name belongs to the user's gallery; a later exclusion
            # only affects future work.
            return self._refuse_cancel(row, "published_before_abort")
        if final_presence.state is not Presence.ABSENT:
            return self._refuse_cancel(row, _presence_error(final_presence, "final"))
        temp_ref = PathRef(**evidence["temp_ref"])
        temp_presence = probe_ref(self.resolver, temp_ref)
        if temp_presence.state is Presence.PRESENT:
            try:
                self._verify_owned(self.resolver.resolve(temp_ref), evidence)
            except (OSError, _Fault, ContractError):
                # Foreign or changed bytes are never deleted; leave the row
                # for an explicit local review.
                return self._refuse_cancel(row, "unowned_staged_temp")
        elif temp_presence.state is not Presence.ABSENT:
            return self._refuse_cancel(row, _presence_error(temp_presence, "temp"))
        current = self._db("get_delivery", row["delivery_id"])
        expected_state = str(current["state"])
        if expected_state == "cancelled":
            return ("cancelled", reason)
        if expected_state not in {"prepared", "recovery_needed"}:
            return self._refuse_cancel(row, "unexpected_unpublished_state")
        try:
            self._db(
                "cancel_unpublished_delivery",
                row["delivery_id"],
                row["current_batch_id"],
                error_code=reason,
                expected_state=expected_state,
                expected_prepared_sha256=str(evidence["sha256"]),
                expected_generation=int(current["generation"]),
            )
        except _StorageFault:
            # The row moved between our read and the CAS (or the writer failed
            # once).  Report the winner instead of guessing a cancellation.
            return self._classify_after_race(row, "cancel_state_changed")
        if temp_presence.state is Presence.PRESENT:
            discard = self._discard_owned_temp(temp_ref, evidence, recheck_after_db=True)
            if not discard.deleted and discard.code != "cleanup_failed":
                # Identity doubt is a durable diagnosis (Sec.2.5).  A transient
                # delete failure is not: the row is already queued for retry,
                # and a durable "failed" fact would only pace it behind fresh
                # work without adding information.
                self._mark_cleanup(row["delivery_id"], evidence, "failed", discard.code)
        else:
            # Reachable root and a provably absent staged file: record the
            # terminal fact instead of retrying a deletion that has nothing
            # left to delete.  This never claims the delivery succeeded.
            self._mark_cleanup(row["delivery_id"], evidence, "cleaned", "already_absent")
        return ("cancelled", reason)

    def _refuse_cancel(self, row, error_code):
        """Keep the row recoverable when the evidence denies cancellation.

        A competing writer may have completed the row between the observation
        and this write.  That refusal is a result, not a storage fault: report
        the winner instead of letting the fault escape the publish path.
        """
        try:
            self._db(
                "set_delivery_issue",
                row["delivery_id"],
                row["current_batch_id"],
                state="recovery_needed",
                error_code=error_code,
            )
        except _StorageFault:
            return self._classify_after_race(row, error_code)
        return ("recovery_needed", error_code)

    def _classify_after_race(self, row, fallback_code):
        """Report the state a competing writer left, without a second write.

        The caller already failed to record an issue, so repeating the same
        write would only repeat the refusal (or the outage).  The row keeps
        whatever durable state the winner gave it.
        """
        try:
            current = self._db("get_delivery", row["delivery_id"])
        except _StorageFault:
            return ("recovery_needed", fallback_code)
        state = str(current["state"])
        if state == "completed":
            return ("completed", "")
        if state == "cancelled":
            return ("cancelled", fallback_code)
        return ("recovery_needed", fallback_code)

    def _mark_cleanup(self, delivery_id, evidence, outcome, error_code):
        """Record one cleanup attempt bound to the prepared identity.

        A missing fact only means a later sweep retries this delivery; it never
        turns an unverified situation into a reported success.
        """
        try:
            self._db(
                "mark_cleanup_completed",
                delivery_id,
                outcome=outcome,
                error_code=error_code,
                expected_sha256=str(evidence["sha256"]),
                expected_size=int(evidence["size"]),
            )
        except _StorageFault:
            return

    def _discard_owned_temp(self, temp_ref, evidence, *, recheck_after_db=False):
        """Delete exactly the staged file this delivery still owns.

        One implementation for cancellation, retry cleanup and post-confirm
        cleanup.  The file is re-observed and re-verified after any writer
        round-trip, because the path may name a replacement by then (RFA-06).
        Nothing is ever deleted from a name, an extension or a directory scan.
        """
        presence = probe_ref(self.resolver, temp_ref)
        if presence.state is Presence.ABSENT:
            return _Discard(True, "already_absent")
        if presence.state is not Presence.PRESENT:
            return _Discard(False, _presence_error(presence, "temp"))
        temp = self.resolver.resolve(temp_ref)
        try:
            self._verify_owned(temp, evidence)
        except (OSError, _Fault, ContractError):
            # After a writer round-trip the path is expected to name the very
            # object that was verified before the CAS; anything else was
            # replaced in between.
            code = (
                "temp_replaced_after_observation"
                if recheck_after_db
                else "unowned_staged_temp"
            )
            return _Discard(False, code)
        if recheck_after_db:
            again = probe_ref(self.resolver, temp_ref)
            if again.state is Presence.ABSENT:
                return _Discard(True, "already_absent")
            if again.state is not Presence.PRESENT:
                return _Discard(False, _presence_error(again, "temp"))
            try:
                self._verify_owned(temp, evidence)
            except (OSError, _Fault, ContractError):
                return _Discard(False, "temp_replaced_after_observation")
        try:
            temp.unlink()
        except OSError:
            return _Discard(False, "cleanup_failed")
        return _Discard(True, "deleted")

    def cleanup_delivery(self, delivery_id):
        """Explicit local cleanup, safe even after a crash just after confirmation."""
        row = self._db("get_delivery", delivery_id)
        if row["state"] == "cancelled":
            return self.cleanup_cancelled_delivery(delivery_id)
        evidence = row.get("prepared")
        if row["state"] != "completed" or not isinstance(evidence, dict):
            return False
        final_ref = PathRef(**evidence["final_ref"])
        temp_ref = PathRef(**evidence["temp_ref"])
        final = self.resolver.resolve(final_ref)
        temp = self.resolver.resolve(temp_ref)
        if temp.parent != final.parent or temp == final or temp.suffix != ".part":
            return False
        self._verify_owned(final, evidence)
        return self._discard_owned_temp(temp_ref, evidence).deleted

    def cleanup_cancelled_delivery(self, delivery_id):
        """Retry deleting a cancelled delivery's own staged temp.

        The final name is never touched, no directory is scanned, and the file
        is removed only after its identity, size and digest still match the
        prepared evidence.  A root that cannot be observed is not "already
        absent": the delivery stays in the retry queue (RFA-02).
        """
        row = self._db("get_delivery", delivery_id)
        if row.get("state") != "cancelled":
            return False
        evidence = row.get("prepared")
        if not isinstance(evidence, dict):
            return False
        final_ref = PathRef(**evidence["final_ref"])
        temp_ref = PathRef(**evidence["temp_ref"])
        final_presence = probe_ref(self.resolver, final_ref)
        if final_presence.state is Presence.PRESENT:
            # A published name belongs to the user's gallery; cleanup never
            # removes it, even for a cancelled row.
            return False
        if final_presence.state is not Presence.ABSENT:
            self._mark_cleanup(
                delivery_id, evidence, "failed", _presence_error(final_presence, "final")
            )
            return False
        temp_presence = probe_ref(self.resolver, temp_ref)
        if temp_presence.state is Presence.ABSENT:
            self._mark_cleanup(delivery_id, evidence, "cleaned", "already_absent")
            return True
        if temp_presence.state is not Presence.PRESENT:
            self._mark_cleanup(
                delivery_id, evidence, "failed", _presence_error(temp_presence, "temp")
            )
            return False
        final = self.resolver.resolve(final_ref)
        temp = self.resolver.resolve(temp_ref)
        if final == temp or temp.parent != final.parent or temp.suffix != ".part":
            return False
        discard = self._discard_owned_temp(temp_ref, evidence)
        if not discard.deleted:
            self._mark_cleanup(delivery_id, evidence, "failed", discard.code)
            return False
        self._mark_cleanup(delivery_id, evidence, "cleaned", "")
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
            delivery_id = row["delivery_id"]
            evidence = row.get("prepared")
            try:
                if not isinstance(evidence, dict):
                    raise _Fault("prepared_evidence_missing", block=True)
                published = self._publish(row, evidence)
                if isinstance(published, tuple):
                    results.append((delivery_id, published[0]))
                else:
                    results.append((delivery_id, "completed"))
            except _StorageFault:
                # A typed library failure is a local condition, not a reason to
                # abort startup; the row stays recoverable.
                results.append((delivery_id, "recovery_needed"))
            except (_Fault, OSError, ContractError):
                results.append((delivery_id, self._settle_local_failure(row)))
        self._sweep_cleanup(limit)
        return tuple(results)

    def _settle_local_failure(self, row):
        """Classify a local recovery failure without letting a race escape.

        A competing recovery may have completed or cancelled the row while this
        call was working.  Report the winner's state instead of rewriting it,
        and never let the compensation's own refusal abort the loop (RFA-07).
        Only an unreadable ledger propagates out of here.
        """
        delivery_id = row["delivery_id"]
        batch_id = row["current_batch_id"]
        current = self._db("get_delivery", delivery_id)
        for _ in range(2):
            state = str(current["state"])
            if state in {"completed", "cancelled", "recovery_needed"}:
                return state
            try:
                self._db(
                    "set_delivery_issue",
                    delivery_id,
                    batch_id,
                    state="recovery_needed",
                    error_code="local_recovery_conflict",
                    expected_state=state,
                )
                return "recovery_needed"
            except _StorageFault:
                # Either the row moved (a legal CAS refusal) or the writer is
                # unavailable.  Re-reading separates the two cases.
                current = self._db("get_delivery", delivery_id)
        state = str(current["state"])
        if state in {"completed", "cancelled", "recovery_needed"}:
            return state
        # The row did not move and the compensation could not be written.  It
        # is still recoverable exactly as it was, so report that instead of
        # aborting the whole startup pass with a false success.
        return "recovery_needed"

    def _sweep_cleanup(self, limit):
        """Bounded retry of cancelled-by-intent temps, with fair cursors.

        Each tier keeps its own continuation, so the never-attempted rows and
        the already-retried rows advance independently; reaching the tail of
        both wraps once so a single startup pass still cleans the beginning.
        """
        rows = ()
        try:
            wrapped = False
            while True:
                stats: dict[str, int] = {}
                rows = self._db(
                    "list_cleanup_candidates",
                    limit=limit,
                    after_rowid=self._cleanup_cursor,
                    after_retry_rowid=self._cleanup_retry_cursor,
                    stats=stats,
                )
                self._cleanup_cursor = max(
                    self._cleanup_cursor, int(stats.get("next_rowid", 0))
                )
                self._cleanup_retry_cursor = int(
                    stats.get("next_retry_rowid", self._cleanup_retry_cursor)
                )
                if rows or wrapped or not (
                    self._cleanup_cursor or self._cleanup_retry_cursor
                ):
                    break
                # Reached the tail of both tiers: wrap once so one startup pass
                # still cleans the beginning.
                self._cleanup_cursor = 0
                self._cleanup_retry_cursor = 0
                wrapped = True
            for fact in rows:
                try:
                    self.cleanup_cancelled_delivery(fact["delivery_id"])
                except (_Fault, OSError, ContractError, _StorageFault):
                    continue
        except _StorageFault:
            return

