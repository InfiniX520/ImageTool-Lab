"""Stable collection identity excludes command IDs and per-batch resource limits."""
import hashlib
from dataclasses import asdict
from core.task_contracts import CollectionRequest, canonical_json


def scope_fingerprint(request: CollectionRequest) -> str:
    if not isinstance(request, CollectionRequest):
        raise TypeError('CollectionRequest is required')
    fields = asdict(request)
    fields.pop('request_id')
    fields.pop('budget')
    return hashlib.sha256(canonical_json(fields).encode('utf-8')).hexdigest()
