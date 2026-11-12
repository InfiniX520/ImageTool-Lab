"""A UI/CLI snapshot must describe one database revision under concurrent writes."""
import pytest

from core.library_repository import LibraryRepository
from delivery_fixtures import Response, Transport, executor, make_job, run


@pytest.mark.parametrize('change',['phase','complete'])
def test_results_phase_and_counts_share_the_snapshot_read_transaction(tmp_path,monkeypatch,change):
    with LibraryRepository(tmp_path/'ledger.sqlite3') as repo:
        job,ids,_=make_job(repo)
        original_read=repo._readonly_connection
        injected=[];errors=[]
        def reading_connection():
            connection=original_read()
            def before_query(sql):
                if not injected and 'MAX(EVENT_SEQ)' in sql.upper():
                    injected.append(True)
                    try:
                        if change=='phase':repo.request_stop(job['job_id'],'concurrent-pause')
                        else:assert run(repo,job,ids[0],executor(repo,tmp_path,Transport(Response()))).state=='completed'
                    except BaseException as error:errors.append(error)
            connection.set_trace_callback(before_query)
            return connection
        monkeypatch.setattr(repo,'_readonly_connection',reading_connection)
        earlier=repo.snapshot(job['job_id'])
        later=repo.snapshot(job['job_id'])
        assert injected and not errors
        assert later['revision']>earlier['revision']
        assert earlier['results']['phase']==earlier['phase']
        assert later['results']['phase']==later['phase']
        assert earlier['results']['pages']['completed']==earlier['counts']['pages']['completed']==0
        if change=='phase':assert later['phase']=='pausing'
        else:assert later['results']['pages']['completed']==later['counts']['pages']['completed']==1
