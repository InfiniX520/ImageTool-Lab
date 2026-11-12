import json
import os
import base64
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from core.app_paths import AppPaths
from core.collection_settings import CollectionSettings, load_settings, save_settings
from core.library_paths import PathResolver
from core.output_locations import LocationError, OutputLocations, read_artist_ids
from core.page_planner import plan_original_pages
from core.task_contracts import ContractError, OutputSpec, PageSpec, PathRef


def locations(tmp_path):
    project=tmp_path/'project';project.mkdir()
    paths=AppPaths(project);resolver=PathResolver(paths)
    return OutputLocations(paths,resolver),project


def test_project_selection_remains_relative_after_moving_project(tmp_path):
    owner,project=locations(tmp_path)
    folder=project/'图片 收藏';folder.mkdir()
    values=owner.select_directory(str(folder))
    settings=CollectionSettings.from_form(values)
    save_settings(owner.resolver,settings)
    assert settings.output_ref==PathRef(relative_path='图片 收藏')
    assert not (project/'config/output_roots_v1.json').exists()
    moved=tmp_path/'移动 后';project.rename(moved)
    new=PathResolver(AppPaths(moved))
    assert load_settings(new).output_ref==settings.output_ref
    assert new.resolve(settings.output_ref)==moved/'图片 收藏'


def test_external_root_and_descendants_keep_identity_across_restart(tmp_path):
    owner,project=locations(tmp_path)
    folder=tmp_path/'本地图片';folder.mkdir()
    chosen=owner.select_directory(str(folder))
    settings=CollectionSettings.from_form(chosen)
    assert settings.output_ref.kind=='external' and settings.output_relative=='.'
    save_settings(owner.resolver,settings)
    child=folder/'画师';child.mkdir()
    nested=CollectionSettings.from_form(owner.select_directory(str(child)))
    assert nested.output_root_id==settings.output_root_id and nested.output_relative=='画师'
    assert len(owner.resolver.external_roots)==1
    other=PathResolver(AppPaths(project));restored=OutputLocations(AppPaths(project),other);restored.load()
    request=load_settings(other).request('after-restart','offline')
    assert other.resolve(request.output.root_ref)==folder
    assert str(folder) not in request.to_json()
    pages=plan_original_pages('123',1,['https://i.pximg.net/img-original/123_p0.png'],
        replace(request.output,folder_by_artist=False))
    assert pages[0].output_ref.relative_path=='123_p0.png'
    assert other.resolve(pages[0].output_ref)==folder/'123_p0.png'


def test_failed_mapping_save_does_not_publish_an_unrecoverable_root(tmp_path,monkeypatch):
    owner,project=locations(tmp_path);folder=tmp_path/'outside';folder.mkdir()
    def fail(*args):raise OSError('synthetic disk failure')
    monkeypatch.setattr('core.output_locations._write_mapping',fail)
    with pytest.raises(OSError):owner.select_directory(str(folder))
    assert owner.resolver.external_roots=={}
    assert not (project/'config/output_roots_v1.json').exists()


def test_existing_root_never_changes_when_new_directory_is_selected(tmp_path):
    owner,project=locations(tmp_path)
    old,new=tmp_path/'old',tmp_path/'new';old.mkdir();new.mkdir()
    request=CollectionSettings.from_form(owner.select_directory(str(old))).request('old-task','offline')
    new_values=owner.select_directory(str(new))
    assert new_values['output_root_id']!=request.output.root_ref.root_id
    assert owner.resolver.resolve(request.output.root_ref)==old
    with pytest.raises(ContractError):owner.resolver.register_external(request.output.root_ref.root_id,new)
    assert owner.resolver.resolve(request.output.root_ref)==old


@pytest.mark.parametrize('relative',['.','config','download_logs','runtime_logs','core','record_backups'])
def test_folder_picker_cannot_select_program_or_ledger_as_download_directory(tmp_path,relative):
    owner,project=locations(tmp_path);folder=project/relative;folder.mkdir(exist_ok=True)
    with pytest.raises(ValueError):owner.select_directory(str(folder))


def test_parent_of_existing_external_root_is_rejected_without_changing_mapping(tmp_path):
    owner,project=locations(tmp_path)
    outer=tmp_path/'outer';inner=outer/'inner';inner.mkdir(parents=True)
    owner.select_directory(str(inner))
    mapping=project/'config/output_roots_v1.json';before=mapping.read_bytes()
    with pytest.raises(LocationError,match='location_overlap'):owner.select_directory(str(outer))
    assert mapping.read_bytes()==before


def test_linked_directory_is_not_registered(tmp_path):
    owner,project=locations(tmp_path);outside=tmp_path/'outside';outside.mkdir()
    alias=tmp_path/'alias'
    if os.name=='nt':
        # Directory junctions exercise the Windows containment check without
        # requiring the optional symlink privilege or Developer Mode.
        quoted=lambda value:"'"+str(value).replace("'","''")+"'"
        command=f"New-Item -ItemType Junction -Path {quoted(alias)} -Target {quoted(outside)} -ErrorAction Stop | Out-Null"
        encoded=base64.b64encode(command.encode('utf-16le')).decode('ascii')
        subprocess.run(['powershell','-NoProfile','-EncodedCommand',encoded],check=True,capture_output=True)
        assert alias.is_junction()
    else:
        alias.symlink_to(outside,target_is_directory=True)
    with pytest.raises(ContractError):owner.select_directory(str(alias))
    assert owner.resolver.external_roots=={}


def test_invalid_mapping_is_validated_as_a_whole(tmp_path):
    owner,project=locations(tmp_path);a=tmp_path/'a';a.mkdir()
    mapping=project/'config/output_roots_v1.json';mapping.parent.mkdir()
    mapping.write_text(json.dumps({'version':1,'roots':{'a':str(a),'b':str(a/'child')}}),encoding='utf-8')
    with pytest.raises(LocationError,match='location_mapping_invalid'):owner.load()
    assert owner.resolver.external_roots=={}


def test_save_as_is_a_one_off_target_and_does_not_write_until_export(tmp_path):
    owner,project=locations(tmp_path);folder=tmp_path/'导出';folder.mkdir()
    target=owner.export_target(str(folder/'名单.md'),'records')
    assert target.resolver.resolve(target.ref)==folder/'名单.md'
    assert not (folder/'名单.md').exists()
    assert not (project/'config').exists()
    assert owner.resolver.external_roots=={}
    with pytest.raises(LocationError,match='export_extension'):owner.export_target(str(folder/'main.py'),'records')
    with pytest.raises(LocationError,match='export_protected'):owner.export_target(str(project/'config/note.md'),'records')


def test_dot_can_represent_only_an_external_root_not_a_page(tmp_path):
    ref=PathRef(kind='external',root_id='selected',relative_path='.')
    with pytest.raises(ContractError):PathRef(relative_path='.')
    with pytest.raises(ContractError):PageSpec(0,ref,'https://i.pximg.net/img-original/123_p0.png')
    for unsafe in ('../outside','./file.png','child/../file.png'):
        with pytest.raises(ContractError):replace(ref,relative_path=unsafe)


def test_import_accepts_ids_or_full_links_without_extracting_random_digits(tmp_path):
    path=tmp_path/'名单.txt'
    path.write_text('123\nhttps://www.pixiv.net/users/456\n123\n',encoding='utf-8-sig')
    assert read_artist_ids(str(path))==('123','456')
    path.write_text('https://unrelated.example/123',encoding='utf-8')
    with pytest.raises(ValueError):read_artist_ids(str(path))
    path.write_bytes(b'\xff\xfe')
    with pytest.raises(LocationError,match='artist_file_encoding'):read_artist_ids(str(path))
    path.write_bytes(b'1'* (128*1024+1))
    with pytest.raises(LocationError,match='artist_file_size'):read_artist_ids(str(path))
