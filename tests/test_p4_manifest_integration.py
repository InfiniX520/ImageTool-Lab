from dataclasses import replace
import json
import os

import pytest

import core.library_queries as query_module
from core.app_paths import AppPaths
from core.follow_collection import FollowCollection
from core.follow_contracts import ArtworkPage
from core.library_paths import PathResolver
from core.library_queries import LibraryQueries, LibraryQuery
from core.library_repository import LibraryRepository
from core.task_contracts import BatchBudget, CandidateDecision, PathRef, SourcePage
from core.task_coordinator import TaskCoordinator
from delivery_fixtures import Response, Transport, executor, make_job, run
from test_follow_collection import Source, meta, resume, settled, submit


def test_complete_follow_pipeline_exports_the_ledger_after_user_moves_a_file(tmp_path):
    source=Source();source.pages[('7','null')]=ArtworkPage((meta(123),),exhausted=True)
    repo=LibraryRepository(tmp_path/'ledger.sqlite3');transport=Transport(Response())
    resolver=PathResolver(AppPaths(tmp_path))
    with TaskCoordinator(repo,executor(repo,tmp_path,transport),workers=1) as owner:
        job=submit(owner);FollowCollection(owner,source).run(job['batch_id']);settled(repo,job)
        (tmp_path/'downl_Pixiv/123_p0.png').rename(tmp_path/'user-moved.png')
        reader=LibraryQueries(repo)
        batch=reader.fetch(LibraryQuery(batch_id=job['batch_id']))
        history=reader.fetch(LibraryQuery(kind='history'))
        assert batch.rows[0]['new_completed']==batch.rows[0]['completed']==1
        assert len(history.rows)==1
        output=PathRef(relative_path='reports/follow.md')
        result=reader.export_markdown(LibraryQuery(batch_id=job['batch_id']),output,resolver)
        text=resolver.resolve(output).read_text(encoding='utf-8')
        assert result.row_count==1 and 'https://www.pixiv.net/artworks/123' in text
        assert 'i.pximg.net' not in text and 'img-original' not in text
        assert len(transport.calls)==1


def test_source_replay_existing_counts_agree_with_query_and_export(tmp_path):
    source=Source();source.pages[('7','null')]=ArtworkPage((meta(123),meta(124)),exhausted=True)
    repo=LibraryRepository(tmp_path/'ledger.sqlite3')
    with TaskCoordinator(repo,executor(repo,tmp_path,Transport(Response(),Response())),workers=1) as owner:
        first=submit(owner,budget=BatchBudget(max_works=1));pump=FollowCollection(owner,source)
        pump.run(first['batch_id']);settled(repo,first,'paused')
        again=resume(owner,first,'continue')
        pump.run(again['batch_id']);result=settled(repo,again)
        rows=LibraryQueries(repo).fetch(LibraryQuery(batch_id=again['batch_id'])).rows
        assert sum(row['existing'] for row in rows)==result['pages']['existing']==1
        assert sum(row['new_completed'] for row in rows)==result['pages']['new_completed']==1
        assert all(row['state']=='completed' for row in rows)


def test_export_snapshot_excludes_a_new_real_delivery_committed_after_its_watermark(tmp_path,monkeypatch):
    with LibraryRepository(tmp_path/'ledger.sqlite3') as repo:
        job,ids,pages=make_job(repo)
        assert run(repo,job,ids[0],executor(repo,tmp_path,Transport(Response()))).state=='completed'
        reader=LibraryQueries(repo);original=reader._write_header
        extra=replace(pages[0],output_ref=PathRef(relative_path='downl_Pixiv/124_p0.png'),url='https://i.pximg.net/img-original/2026/124_p0.png')
        def during_export(*args,**kwargs):
            original(*args,**kwargs)
            pending=repo.accept_source_page(job['batch_id'],SourcePage('follow','history','7','concurrent',
                (CandidateDecision('124','download',artist_id='7',page_count=1,pages=(extra,)),)))
            assert run(repo,job,pending[0],executor(repo,tmp_path,Transport(Response()))).state=='completed'
        monkeypatch.setattr(reader,'_write_header',during_export)
        result=reader.export_markdown(LibraryQuery(kind='history'),PathRef(relative_path='reports/history.md'),PathResolver(AppPaths(tmp_path)))
        assert result.row_count==1
        current=reader.fetch(LibraryQuery(kind='history'))
        assert len(current.rows)==2 and current.watermark>result.watermark
        assert 'artworks/124' not in (tmp_path/'reports/history.md').read_text(encoding='utf-8')


@pytest.mark.skipif(os.name!='nt',reason='Windows directory link swap boundary')
def test_export_rechecks_parent_links_before_publishing(tmp_path,monkeypatch):
    project=tmp_path/'project';project.mkdir()
    target=project/'reports';target.mkdir()
    elsewhere=tmp_path/'elsewhere'
    assert target.resolve().is_relative_to(tmp_path.resolve())
    assert elsewhere.resolve().is_relative_to(tmp_path.resolve())
    with LibraryRepository(tmp_path/'ledger.sqlite3') as repo:
        make_job(repo)
        original=query_module._verify_identity
        switched=[]
        def swap(path,identity):
            original(path,identity)
            target.rename(elsewhere)
            import _winapi
            _winapi.CreateJunction(str(elsewhere),str(target))
            switched.append(True)
        monkeypatch.setattr(query_module,'_verify_identity',swap)
        with pytest.raises((ValueError,OSError,RuntimeError)):
            LibraryQueries(repo).export_markdown(LibraryQuery(kind='history'),PathRef(relative_path='reports/result.md'),PathResolver(AppPaths(project)))
        assert switched
        assert not (elsewhere/'result.md').exists()
