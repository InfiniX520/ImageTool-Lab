"""Synthetic independent checks; expected safe behaviour, not assertions of bugs."""
import hashlib,json,os,sqlite3,sys,shutil
from pathlib import Path
ROOT=Path(os.environ.get('PIXIVKEEP_AUDIT_REPO',Path(__file__).resolve().parents[1]))
sys.path[:0]=[str(ROOT),str(ROOT/'tests')]
import pytest
from core import library_maintenance as m
import test_u1a_library_maintenance as h

def backup(tmp_path):
 source=h.make_library(tmp_path/'source'/'library.sqlite3',jobs=1)
 info=m.backup_library(source,tmp_path/'backups',label='sealed')
 return Path(info['manifest_path']).parent

# The audit injected its write at inspect_library, i.e. between the proof
# stages.  Amendment A1 removed that stage on purpose: health and logical now
# read one isolated copy, so the equivalent injection point is the moment the
# sealed member has just been copied.  The property is unchanged and stronger:
# the report must not be a pass when the file moved during the check, and it
# must say that what it examined was the sealed copy.
def test_pa01_verify_must_detect_write_between_proof_stages(tmp_path,monkeypatch):
 root=backup(tmp_path);copy=root/'library.sqlite3'
 original=m._copy_and_hash
 changed=[]
 def copy_and_hash(source,destination):
  fact=original(source,destination)
  if Path(source)==copy and not changed:
   with sqlite3.connect(copy) as conn:conn.execute("UPDATE jobs SET state='cancelled'")
   changed.append(True)
  return fact
 monkeypatch.setattr(m,'_copy_and_hash',copy_and_hash)
 result=m.verify_backup(root)
 monkeypatch.setattr(m,'_copy_and_hash',original)
 second=m.verify_backup(root)
 print('PA01_MIXED_PROOF', {k:result[k] for k in ['sealed','logical','health','compatible','ok']},'recheck_ok',second['ok'])
 assert changed and second['ok'] is False
 assert result['ok'] is False, 'All proof stages must describe one stable backup generation'
 assert result['evidence']['scope']=='isolated_copy'
 assert 'changed_during_check' in result['reasons']

def test_pa02_sqlite_similar_prefix_user_table_is_fingerprinted(tmp_path):
 one=h.make_library(tmp_path/'one.sqlite3',jobs=1)
 two=h.make_library(tmp_path/'two.sqlite3',jobs=1)
 for path,value in [(one,'before'),(two,'after')]:
  with sqlite3.connect(path) as conn:
   conn.execute('CREATE TABLE sqliteXuser (value TEXT)')
   conn.execute('INSERT INTO sqliteXuser VALUES (?)',(value,))
 a=m.logical_fingerprint(one);b=m.logical_fingerprint(two);result=m.compare_library(one,two)
 print('PA02_EXCLUDED_TABLE','sqliteXuser' in a['tables'],a['digest']==b['digest'],result['ok'])
 assert a['digest']!=b['digest']
 assert result['ok'] is False


def test_pa01_no_generation_satisfies_all_proofs(tmp_path,monkeypatch):
 root=backup(tmp_path);copy=root/'library.sqlite3';other=tmp_path/'other.sqlite3'
 shutil.copyfile(copy,other)
 with sqlite3.connect(other) as conn:conn.execute("UPDATE jobs SET state='cancelled'")
 p=root/'manifest.json';info=json.loads(p.read_text('utf-8'))
 # A structurally valid manifest: sealed bytes of A, logical content of B.
 # Neither generation alone is a valid backup under this manifest.
 info['logical_fingerprint']=m.logical_fingerprint(other)
 p.write_text(json.dumps(info),'utf-8')
 assert m.verify_backup(root)['ok'] is False
 original=m.logical_fingerprint;changed=[]
 def fingerprint(path,**kwargs):
  if Path(path)==copy and not changed:
   with sqlite3.connect(copy) as conn:conn.execute("UPDATE jobs SET state='cancelled'")
   changed.append(True)
  return original(path,**kwargs)
 monkeypatch.setattr(m,'logical_fingerprint',fingerprint)
 result=m.verify_backup(root)
 monkeypatch.setattr(m,'logical_fingerprint',original)
 assert m.verify_backup(root)['ok'] is False
 print('PA01_NO_COMMON_GENERATION',result['ok'],result['sealed']['state'],result['logical']['state'])
 assert result['ok'] is False

def test_wr04_copy_name_must_be_among_sealed_members(tmp_path):
 root=backup(tmp_path);p=root/'manifest.json';info=json.loads(p.read_text('utf-8'))
 data=b'bookkeeping';(root/'marker.bin').write_bytes(data)
 info['members'].append({'name':'marker.bin','bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()})
 info['sealed_members']=['marker.bin']
 p.write_text(json.dumps(info),'utf-8')
 try:result=m.verify_backup(root)
 except m.LibraryMaintenanceError:return
 print('WR04_UNSEALED_MAIN',result['ok'],result['sealed'])
 assert result['ok'] is False

def test_wr04_bad_member_bytes_returns_typed_error(tmp_path):
 root=backup(tmp_path);p=root/'manifest.json';info=json.loads(p.read_text('utf-8'))
 info['members'][0]['bytes']='not-an-integer';p.write_text(json.dumps(info),'utf-8')
 try:result=m.verify_backup(root)
 except m.LibraryMaintenanceError:return
 assert result['ok'] is False

def test_control_normal_backup_still_verifies(tmp_path):
 assert m.verify_backup(backup(tmp_path))['ok'] is True

def test_control_normal_table_change_is_detected(tmp_path):
 root=backup(tmp_path)
 with sqlite3.connect(root/'library.sqlite3') as conn:conn.execute("UPDATE jobs SET state='cancelled'")
 assert m.verify_backup(root)['ok'] is False


def artifact_library(root, count, external=False):
 db=h.make_library(root/'download_logs'/'library.sqlite3',jobs=0)
 now='2026-11-14T00:00:00+00:00'
 # Synthetic scanner input: artifacts are read-only rows; no writer/recovery is run.
 with sqlite3.connect(db) as conn:
  for n in range(count):
   data=f'image-{n}'.encode();relative=f'images/{n:04d}.jpg'
   ref={'kind':'external','root_id':'folder_test','relative_path':'p.jpg'} if external else {'kind':'project','root_id':'project','relative_path':relative}
   conn.execute('INSERT INTO artifacts(artifact_id,delivery_id,job_id,illust_id,page_index,output_key,output_ref_json,quality_json,media_type,size,sha256,verification,completed_at,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(
    f'a-{n:04d}',f'd-{n:04d}','synthetic-job',str(n+1),0,f'key-{n:04d}',json.dumps(ref),'{}','image/jpeg',len(data),hashlib.sha256(data).hexdigest(),'structure_checked',now,now))
   if not external:
    target=root/relative;target.parent.mkdir(exist_ok=True);target.write_bytes(data)
 return db


# The audit's fixture put the external root *inside* the project root.  The
# product's PathResolver forbids overlapping roots (two identities for the
# same files), and the desktop writes project-relative roots for that case,
# so such a file is corrupt and the command line must refuse it - see
# test_wr02_an_overlapping_root_is_refused_below.  The same assertion is kept
# here with the only legal shape: a root outside the project.
def test_wr02_cli_loads_saved_external_root_mapping(tmp_path,capsys):
 db=artifact_library(tmp_path,1,external=True)
 external=tmp_path.parent/(tmp_path.name+'-external');external.mkdir();(external/'p.jpg').write_bytes(b'image-0')
 config=tmp_path/'config';config.mkdir()
 (config/'output_roots_v1.json').write_text(json.dumps({'version':1,'roots':{'folder_test':str(external)}}),'utf-8')
 code=m.main(['--json','scan',str(db),'--root',str(tmp_path)])
 result=json.loads(capsys.readouterr().out)
 print('WR02_EXTERNAL',code,result['rows'][0]['verdict'])
 assert code==0 and result['rows'][0]['verdict']=='match'


def test_wr02_an_overlapping_root_is_refused_below(tmp_path,capsys):
 # A saved root that lives inside the project is corrupt, not a match and not
 # a fallback: the whole command stops with a typed mapping error (exit 3).
 db=artifact_library(tmp_path,1,external=True)
 external=tmp_path/'external';external.mkdir();(external/'p.jpg').write_bytes(b'image-0')
 config=tmp_path/'config';config.mkdir()
 (config/'output_roots_v1.json').write_text(json.dumps({'version':1,'roots':{'folder_test':str(external)}}),'utf-8')
 code=m.main(['--json','scan',str(db),'--root',str(tmp_path)])
 result=json.loads(capsys.readouterr().out)
 print('WR02_OVERLAP',code,result.get('code'))
 assert code==3 and result['code']=='mapping_unreadable'
 assert 'rows' not in result


def test_wr03_bare_continuation_cannot_claim_full_scan(tmp_path,capsys):
 from core.library_audit import scan_library_artifacts
 from core.library_paths import PathResolver
 from core.app_paths import AppPaths
 db=artifact_library(tmp_path,201)
 # Obtain a bare cursor from the stable engine page API, independently of
 # whether a future CLI default already performs a complete traversal.
 payload=scan_library_artifacts((db,PathResolver(AppPaths(tmp_path))),limit=200)
 cursor=payload['next_cursor']
 assert payload['truncated'] and cursor
 code=m.main(['scan',str(db),'--after',','.join(cursor)])
 output=capsys.readouterr().out
 print('WR03_CONTINUATION',code,output.strip())
 assert code!=0 and '覆盖全部 1 行' not in output
