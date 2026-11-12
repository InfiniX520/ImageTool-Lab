"""Synthetic HTTP/filesystem/SQLite fixture helpers, never production data."""
from dataclasses import replace
import threading

from core.app_paths import AppPaths
from core.library_paths import PathResolver
from core.page_delivery import PageDeliveryExecutor, TransferPolicy
from core.task_contracts import CollectionRequest, CandidateDecision, OutputSpec, PageSpec, PathRef, SourcePage
from image_fixtures import PNG_BYTES


class Response:
    def __init__(self, data=PNG_BYTES, *, status=200, headers=None, chunks=None):
        self.data, self.status_code, self.closed = data, status, False
        self.headers = {"Content-Type":"image/png", "Content-Length":str(len(data))} if headers is None else headers
        self.chunks = chunks

    def iter_content(self, chunk_size):
        assert chunk_size == 65536
        chunks = self.chunks if self.chunks is not None else (self.data[i:i+chunk_size] for i in range(0,len(self.data),chunk_size))
        for part in chunks:
            if isinstance(part, BaseException):
                raise part
            if callable(part):
                part()
            else:
                yield part

    def close(self):
        self.closed = True


class Transport:
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    def __call__(self, url, **kwargs):
        self.calls.append((url, kwargs))
        assert self.responses, "Unexpected extra GET"
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        if callable(response):
            return response(url, kwargs)
        return response


def make_job(repo, *, artwork="123", extension="png", count=1, quality=None, request_id="test", urls=None):
    kwargs = {"quality":quality} if quality is not None else {}
    request = CollectionRequest(request_id, "offline-account", output=OutputSpec(folder_by_artist=False), **kwargs)
    job = repo.submit(request,"submit-"+request_id)
    assert repo.activate_next()["batch_id"] == job["batch_id"]
    pages = tuple(PageSpec(i,PathRef(relative_path=f"downl_Pixiv/{artwork}_p{i}.{extension}"),
            urls[i] if urls else f"https://i.pximg.net/img-original/2026/{artwork}_p{i}.{extension}") for i in range(count))
    candidate = CandidateDecision(artwork,"download",artist_id="7",page_count=count,pages=pages)
    source = SourcePage("follow","history","7","source-1",(candidate,),exhausted=True)
    ids = repo.accept_source_page(job["batch_id"],source)
    return job, ids, pages


def executor(repo, root, transport, **kwargs):
    return PageDeliveryExecutor(repo, PathResolver(AppPaths(root)), get=transport,
        policy=kwargs.pop("policy",TransferPolicy(retry_delay=0)), **kwargs)


def run(repo, job, delivery, service, stop=None):
    row = repo.claim_delivery(delivery,job["batch_id"])
    assert row
    return service(row,stop if stop is not None else threading.Event())
