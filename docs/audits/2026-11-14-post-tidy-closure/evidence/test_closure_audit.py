"""Independent expected-safety probes, synthetic trees under this audit only."""
import hashlib,json,os,sqlite3,subprocess,sys
from pathlib import Path
ROOT=Path(os.environ.get('PIXIVKEEP_AUDIT_REPO',Path(__file__).resolve().parents[4]))
sys.path[:0]=[str(ROOT),str(ROOT/'tests')]
import pytest
from core import library_maintenance as m
import test_u1a_library_maintenance as h

def backup(tmp_path):
 db=h.make_library(tmp_path/'source'/'library.sqlite3',jobs=1)
 return Path(m.backup_library(db,tmp_path/'backups',label='sealed')['manifest_path']).parent

def test_cl01_verify_cleanup_preserves_an_unowned_file(tmp_path,monkeypatch):
 root=backup(tmp_path);created=[];original=m._verification_workspace
 def workspace(path):
  p,kind=original(path);foreign=p/'unowned.txt';foreign.write_bytes(b'not created by verifier');created.append(foreign);return p,kind
 monkeypatch.setattr(m,'_verification_workspace',workspace)
 result=m.verify_backup(root)
 print('CL01_FOREIGN',result['ok'],result['evidence']['cleanup'],created[0].exists())
 assert created[0].exists(), 'Cleanup must preserve files it did not create'

def test_cl01_verify_cleanup_does_not_follow_a_junction(tmp_path,monkeypatch):
 root=backup(tmp_path);outside=tmp_path/'separate-owned-test-directory';outside.mkdir()
 sentinel=outside/'must-stay.txt';sentinel.write_bytes(b'synthetic protected bytes')
 assert outside.resolve().is_relative_to(tmp_path.resolve())
 original=m._verification_workspace
 def workspace(path):
  p,kind=original(path);link=p/'foreign-junction'
  assert p.resolve().is_relative_to(tmp_path.resolve())
  result=subprocess.run(['cmd','/c','mklink','/J',str(link),str(outside)],capture_output=True)
  assert result.returncode==0, 'Synthetic junction creation failed'
  assert link.is_junction()
  return p,kind
 monkeypatch.setattr(m,'_verification_workspace',workspace)
 try:result=m.verify_backup(root)
 except OSError:result={'ok':False}
 print('CL01_JUNCTION',sentinel.exists(),result['ok'])
 assert sentinel.exists(), 'Verifier must never delete a junction target file'

def test_wr04_late_manifest_failure_is_typed_and_accounted(tmp_path,monkeypatch):
 db=h.make_library(tmp_path/'source'/'library.sqlite3',jobs=1)
 reached=[]
 def fail(*args,**kwargs):
  reached.append(True)
  raise OSError(28,'synthetic disk full')
 monkeypatch.setattr(m,'_write_manifest',fail)
 try:m.backup_library(db,tmp_path/'backups',label='failed')
 except Exception as error:
  print('WR04_LATE',reached,type(error).__name__,str(error))
  assert reached and isinstance(error,m.LibraryMaintenanceError)
 else:pytest.fail('Must report the injected error')
 # Residual ownership/accounting is checked separately; never demand recursive deletion.

def test_wr04_cleanup_error_preserves_primary_failure(tmp_path,monkeypatch):
 db=h.make_library(tmp_path/'source'/'library.sqlite3',jobs=1)
 def fail(*args,**kwargs):raise OSError('synthetic primary copy failure')
 def cleanup(*args,**kwargs):raise PermissionError('synthetic cleanup failure')
 monkeypatch.setattr(m,'_online_snapshot',fail);monkeypatch.setattr(m,'_discard',cleanup)
 with pytest.raises(m.LibraryMaintenanceError) as caught:
  m.backup_library(db,tmp_path/'backups',label='failed')
 assert 'copy' in str(caught.value) and 'cleanup' in str(caught.value)

@pytest.mark.parametrize('kind',['corrupt','incompatible','empty'])
def test_cl02_restore_check_cannot_approve_an_unusable_database(tmp_path,capsys,kind):
 db=tmp_path/'bad.sqlite3'
 if kind=='corrupt':db.write_bytes(b'not sqlite but definitely exists')
 elif kind=='empty':db.touch()
 else:
  with sqlite3.connect(db) as conn:conn.execute('CREATE TABLE unrelated (v TEXT)')
 code=m.main(['--json','restore-check',str(db)])
 result=json.loads(capsys.readouterr().out)
 print('CL02_RESTORE',kind,code,result['state'],result['status'])
 assert code!=0 and result['state']!='ok'

def test_cl03_unproven_scan_prefix_is_not_certified(tmp_path):
 from core.library_scan import scan_library_report
 from core.library_audit import ScanBudget
 from core.library_paths import PathResolver
 from core.app_paths import AppPaths
 from core.library_repository import LibraryRepository
 import test_u1a_library_audit as ah
 root=tmp_path/'library';root.mkdir()
 with LibraryRepository(tmp_path/'source.sqlite3') as repo:
  resolver=PathResolver(AppPaths(root));repo.bind_path_resolver(resolver)
  ah.publish(repo,root,'11','12')
  result=scan_library_report((repo.database_path,resolver),budget=ScanBudget(max_files=1))
 print('CL03_PREFIX',result['coverage'],result['examined'],result['resume'])
 assert result['resume']['complete_prefix'] is False

def test_wr04_unavailable_verify_workspace_has_unreadable_exit(tmp_path,monkeypatch,capsys):
 root=backup(tmp_path)
 def fail(*args,**kwargs):raise OSError('synthetic no space')
 monkeypatch.setattr(m,'_verification_workspace',fail)
 code=m.main(['--json','verify',str(root)]);result=json.loads(capsys.readouterr().out)
 print('WR04_WORKSPACE',code,result['reasons'])
 assert code==3 and result['ok'] is False

def test_control_static_backup_verifies(tmp_path):
 assert m.verify_backup(backup(tmp_path))['ok'] is True

def test_control_legal_similar_prefix_table_changes_digest(tmp_path):
 one=h.make_library(tmp_path/'one.sqlite3',jobs=0);two=h.make_library(tmp_path/'two.sqlite3',jobs=0)
 for db,value in [(one,'a'),(two,'b')]:
  with sqlite3.connect(db) as conn:
   conn.execute('CREATE TABLE sqliteXuser(value TEXT)');conn.execute('INSERT INTO sqliteXuser VALUES (?)',(value,))
 assert m.compare_library(one,two)['ok'] is False

def test_wr06_refresh_drops_the_previous_inflight_page():
 import test_collection_verification_panel as ui
 controller,view=ui._queued_controller()
 controller.handle('select_task',{'id':'job-a'})
 controller.handle('navigate',{'page':'verify'})
 for _ in range(2):controller._poll()
 first=controller.runtime.pending['pages'][0]
 controller.handle('verification_refresh',{})
 ui._resolve(first,ui._rows_page([ui._row(batch='batch-a')],batch='batch-a'))
 for _ in range(3):controller._poll()
 print('WR06_REFRESH',len(controller._verify_display),len(controller.runtime.pending['pages']))
 assert controller._verify_display==[], 'Refresh must not accept a pre-refresh reply'

def test_wr04_nul_member_is_a_typed_refusal(tmp_path,capsys):
 root=backup(tmp_path);path=root/'manifest.json';data=json.loads(path.read_text('utf-8'))
 data['members'][0]['name']='bad\x00name'
 data['copy_name']='bad\x00name';data['sealed_members']=['bad\x00name']
 path.write_text(json.dumps(data),'utf-8')
 code=m.main(['--json','verify',str(root)])
 result=json.loads(capsys.readouterr().out)
 assert code==3 and result.get('status')=='error'

def test_pa03_missing_path_identity_cannot_be_reported_proven(tmp_path,monkeypatch):
 import importlib.util
 from types import SimpleNamespace
 location=os.environ.get('PIXIVKEEP_ARCHIVE_COLLECTOR')
 if not location:pytest.skip('Local archive collector is outside the public repository; set PIXIVKEEP_ARCHIVE_COLLECTOR to its audited snapshot')
 script=Path(location)
 spec=importlib.util.spec_from_file_location('audited_archive_collector',script)
 archive=importlib.util.module_from_spec(spec);spec.loader.exec_module(archive)
 target=tmp_path/'file.bin';target.write_bytes(b'owned synthetic input')
 original=archive.lstat_of;calls=[]
 def named(path):
  info=original(path);calls.append(True)
  if len(calls)>1:
   return SimpleNamespace(st_ino=0,st_dev=0,st_nlink=0,st_size=info.st_size,st_mtime_ns=info.st_mtime_ns,st_file_attributes=info.st_file_attributes)
  return info
 monkeypatch.setattr(archive,'lstat_of',named)
 record=archive.file_record(target,hashes=True)
 print('PA03_PARTIAL_ID',record['identity'],len(calls))
 assert record['identity']=='unknown'
