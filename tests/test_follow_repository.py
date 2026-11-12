from dataclasses import replace
import sqlite3

import pytest

from core.follow_contracts import FollowArtist, FollowingPage
from core.library_repository import BudgetExceeded, CommandConflict, CursorConflict, InvalidStateError, LibraryRepository
from core.task_contracts import BatchBudget, CandidateDecision, CollectionRequest, OutputSpec, PageSpec, PathRef, SourcePage
from delivery_fixtures import Response, Transport, executor, make_job, run


def start(repo,request_id='request',account='account',budget=None):
    options={'budget':budget} if budget is not None else {}
    request=CollectionRequest(request_id,account,**options)
    job=repo.submit(request,'submit-'+request_id)
    assert repo.activate_next()['batch_id']==job['batch_id']
    return job


def page(*ids,next_cursor='null',exhausted=True,total=None,coverage=True):
    return FollowingPage(tuple(FollowArtist(str(value),'name-'+str(value)) for value in ids),next_cursor,exhausted,total,coverage)


def snapshot(repo,job,*ids,command='sync',refresh=False):
    value=repo.begin_follow_snapshot(job['batch_id'],command,refresh=refresh)
    return repo.accept_following_page(job['batch_id'],value['snapshot_id'],'page-'+command,'null',page(*ids,total=len(ids)))


def test_full_snapshot_is_idempotent_and_rename_reorder_keeps_artist_identity(tmp_path):
    with LibraryRepository(tmp_path/'library.sqlite3') as repo:
        job=start(repo)
        original=snapshot(repo,job,7,8)
        replay=repo.accept_following_page(job['batch_id'],original['snapshot_id'],'page-sync','null',page(7,8,total=2))
        assert replay==original
        next_one=repo.begin_follow_snapshot(job['batch_id'],'refresh',refresh=True)
        changed=FollowingPage((FollowArtist('8','renamed'),FollowArtist('7','same'),FollowArtist('9','new')),exhausted=True,expected_total=3)
        repo.accept_following_page(job['batch_id'],next_one['snapshot_id'],'second','null',changed)
        rows=repo.follow_artists('account')
        assert [row['artist_id'] for row in rows]==['7','8','9']
        assert rows[1]['display_name']=='renamed'
        assert [row['artist_id'] for row in repo.follow_artists('account',snapshot_id=next_one['snapshot_id'])]==['8','7','9']


def test_incomplete_snapshot_never_unfollows_absent_artists_and_complete_does(tmp_path):
    with LibraryRepository(tmp_path/'library.sqlite3') as repo:
        job=start(repo);snapshot(repo,job,7,8)
        partial=repo.begin_follow_snapshot(job['batch_id'],'next',refresh=True)
        repo.accept_following_page(job['batch_id'],partial['snapshot_id'],'part','null',page(8,next_cursor='{"offset":1}',exhausted=False))
        assert [row['artist_id'] for row in repo.follow_artists('account')]==['7','8']
        repo.accept_following_page(job['batch_id'],partial['snapshot_id'],'end','{"offset":1}',page(exhausted=True,total=1))
        assert [row['artist_id'] for row in repo.follow_artists('account')]==['8']
        revived=snapshot(repo,job,7,8,command='rejoin',refresh=True)
        assert revived['state']=='complete'
        assert [row['artist_id'] for row in repo.follow_artists('account')]==['7','8']
        assert [row['ordinal'] for row in repo.follow_artists('account')]==[0,1]


@pytest.mark.parametrize('total,coverage',[(3,True),(1,False)])
def test_uncertain_or_count_mismatched_snapshot_does_not_infer_unfollow(tmp_path,total,coverage):
    with LibraryRepository(tmp_path/'library.sqlite3') as repo:
        job=start(repo);snapshot(repo,job,7,8)
        value=repo.begin_follow_snapshot(job['batch_id'],'next',refresh=True)
        result=repo.accept_following_page(job['batch_id'],value['snapshot_id'],'uncertain','null',page(8,total=total,coverage=coverage))
        assert result['state']=='coverage_uncertain' and not result['coverage_complete']
        assert len(repo.follow_artists('account'))==2


def test_partial_snapshot_and_known_artist_survive_reopen_and_new_batch(tmp_path):
    database=tmp_path/'library.sqlite3'
    with LibraryRepository(database) as repo:
        job=start(repo);value=repo.begin_follow_snapshot(job['batch_id'],'sync')
        repo.accept_following_page(job['batch_id'],value['snapshot_id'],'one','null',page(7,next_cursor='{"offset":1}',exhausted=False))
    with LibraryRepository(database) as repo:
        resumed=repo.resume(job['job_id'],'continue');repo.activate_next()
        selected=repo.begin_follow_snapshot(resumed['batch_id'],'select')
        assert selected['snapshot_id']==value['snapshot_id'] and selected['cursor_json']=='{"offset":1}'
        done=repo.accept_following_page(resumed['batch_id'],value['snapshot_id'],'two',selected['cursor_json'],page(8,total=2))
        assert done['state']=='complete'
        assert len(repo.follow_artists('account'))==2


def test_snapshot_page_rollback_keeps_members_cursor_and_receipt_atomic(tmp_path):
    database=tmp_path/'library.sqlite3'
    with LibraryRepository(database) as repo:
        job=start(repo);value=repo.begin_follow_snapshot(job['batch_id'],'sync')
        with sqlite3.connect(database) as injection:
            injection.execute("CREATE TRIGGER fail_follow BEFORE INSERT ON events WHEN NEW.kind='follow_snapshot_page' BEGIN SELECT RAISE(ABORT,'synthetic'); END")
        with pytest.raises(sqlite3.IntegrityError):repo.accept_following_page(job['batch_id'],value['snapshot_id'],'one','null',page(7,total=1))
        assert repo.follow_artists('account')==()
        assert repo.get_follow_snapshot(value['snapshot_id'])['cursor_json']=='null'
        with sqlite3.connect(database) as injection:
            assert injection.execute('SELECT COUNT(*) FROM follow_snapshot_pages').fetchone()[0]==0
            injection.execute('DROP TRIGGER fail_follow')
        assert repo.accept_following_page(job['batch_id'],value['snapshot_id'],'one','null',page(7,total=1))['state']=='complete'


def test_snapshot_receipt_commands_and_account_boundaries_reject_changed_identity(tmp_path):
    with LibraryRepository(tmp_path/'library.sqlite3') as repo:
        job=start(repo);value=snapshot(repo,job,7)
        with pytest.raises(CursorConflict):repo.accept_following_page(job['batch_id'],value['snapshot_id'],'page-sync','null',page(8,total=1))
        with pytest.raises(CommandConflict):repo.begin_follow_snapshot(job['batch_id'],'sync',refresh=True)
        repo.request_stop(job['job_id'],'stop');repo.settle_stop(job['batch_id'])
        other=start(repo,'other','another-account')
        with pytest.raises(InvalidStateError):repo.accept_following_page(other['batch_id'],value['snapshot_id'],'foreign','null',page(9,total=1))
        with pytest.raises(InvalidStateError):repo.follow_artists('another-account',snapshot_id=value['snapshot_id'])


def test_source_request_and_artist_budgets_are_persistent_and_not_refunded(tmp_path):
    with LibraryRepository(tmp_path/'library.sqlite3') as repo:
        job=start(repo,budget=BatchBudget(max_requests=2,max_artists=1))
        repo.reserve_source_request(job['batch_id'],kind='following')
        repo.reserve_source_request(job['batch_id'],kind='detail')
        with pytest.raises(BudgetExceeded):repo.reserve_source_request(job['batch_id'],kind='artist_works')
        assert repo.admit_source_artist(job['batch_id'],'7') is True
        assert repo.admit_source_artist(job['batch_id'],'7') is False
        with pytest.raises(BudgetExceeded):repo.admit_source_artist(job['batch_id'],'8')
        assert repo.source_budget_status(job['batch_id'])=={'reserved_requests':2,'max_requests':2,'artists':1,'max_artists':1}
        repo.request_stop(job['job_id'],'pause')
        with pytest.raises(InvalidStateError):repo.reserve_source_request(job['batch_id'],kind='detail')


def test_trusted_complete_pages_do_not_take_the_new_work_budget(tmp_path):
    with LibraryRepository(tmp_path/'library.sqlite3') as repo:
        old,ids,pages=make_job(repo)
        assert run(repo,old,ids[0],executor(repo,tmp_path,Transport(Response()))).state=='completed'
        repo.request_stop(old['job_id'],'old-stop');repo.settle_stop(old['batch_id'])
        request=CollectionRequest('fresh','offline-account',output=OutputSpec(folder_by_artist=False),budget=BatchBudget(max_works=1))
        current=repo.submit(request,'fresh-submit');repo.activate_next()
        second_page=PageSpec(0,PathRef(relative_path='downl_Pixiv/124_p0.png'),'https://i.pximg.net/img-original/2026/124_p0.png')
        source=SourcePage('follow','history','7','mixed',(CandidateDecision('123','download',artist_id='7',page_count=1,pages=pages),
            CandidateDecision('124','download',artist_id='7',page_count=1,pages=(second_page,))))
        repo.accept_source_page(current['batch_id'],source)
        assert repo.budget_status(current['batch_id'])['used_works']==1
        counts=repo.batch_results(current['batch_id'])
        assert counts['pages']['existing']==1 and counts['pages']['pending']==1
        assert counts['works']['completed']==1
        third=replace(second_page,output_ref=PathRef(relative_path='downl_Pixiv/125_p0.png'),url='https://i.pximg.net/img-original/2026/125_p0.png')
        with pytest.raises(BudgetExceeded):repo.accept_source_page(current['batch_id'],SourcePage('follow','history','7','over-budget',
            (CandidateDecision('125','download',artist_id='7',page_count=1,pages=(third,)),)))
        assert repo.budget_status(current['batch_id'])['used_works']==1


def test_related_request_parent_is_persisted_and_part_of_command_idempotency(tmp_path):
    with LibraryRepository(tmp_path/'library.sqlite3') as repo:
        parent=repo.submit(CollectionRequest('one','account'),'parent')
        request=CollectionRequest('two','account')
        child=repo.submit(request,'child',parent_job_id=parent['job_id'])
        assert repo.submit(request,'child',parent_job_id=parent['job_id'])==child
        from core.library_repository import CommandConflict
        with pytest.raises(CommandConflict):repo.submit(request,'child')
        with pytest.raises(InvalidStateError):repo.submit(CollectionRequest('other','other-account'),'other',parent_job_id=parent['job_id'])
        with sqlite3.connect(repo.database_path) as conn:
            assert conn.execute('SELECT parent_job_id FROM jobs WHERE job_id=?',(child['job_id'],)).fetchone()[0]==parent['job_id']
