from dataclasses import replace
import json
import os

import pytest

from core.app_paths import AppPaths
from core.collection_settings import CollectionSettings, SettingsError, load_settings, save_settings
from core.library_paths import PathResolver


def test_independent_optional_filters_and_relative_output_survive_saving(tmp_path):
    resolver = PathResolver(AppPaths(tmp_path))
    settings = CollectionSettings(min_bookmarks='1234', exclude_ai=True)
    save_settings(resolver, settings)
    reopened = load_settings(resolver)
    disabled = reopened.request('first', 'offline-account')
    assert disabled.query.filters.exclude_ai and disabled.query.filters.min_bookmarks is None
    enabled = reopened.request('second', 'offline-account', {'min_bookmarks_enabled': True})
    assert enabled.query.filters.min_bookmarks == 1234 and enabled.query.filters.exclude_ai
    assert enabled.output.root_ref.relative_path == 'downl_Pixiv'
    assert not (tmp_path/'pixiv_config.json').exists()


def test_artist_id_and_official_link_parsing_is_local_and_preserves_selection(tmp_path):
    request = CollectionSettings().request('artist-test','offline-account',
        {'artist_ids': '7，https://www.pixiv.net/users/8/ 7\nhttps://www.pixiv.net/en/users/9'})
    assert json.loads(request.query.parameters_json) == {'artist_ids': ['7','8','9']}
    for form in ({'artist_ids':'https://evil.test/users/7'}, {'artist_ids':'7','start_ordinal':'3'},
                 {'start_ordinal':'0'}, {'start_artist_id':'https://www.pixiv.net/users/7'}):
        with pytest.raises(SettingsError):
            CollectionSettings().request('bad','offline-account',form)


@pytest.mark.parametrize('form', [
    {'output_relative':'E:/outside'}, {'output_relative':'../outside'}, {'output_relative':'config/pictures'},
    {'workers':'9'}, {'workers':True}, {'max_requests':'0'}, {'max_works':'1e3'},
    {'min_bookmarks_enabled':True,'min_bookmarks':'-1'}, {'user_agent':'value\r\nCookie: fake'},
])
def test_invalid_saved_choices_are_reported_without_silent_clamping(form):
    with pytest.raises(SettingsError):
        CollectionSettings.from_form(form)


def test_failed_preferences_publish_keeps_prior_settings(tmp_path, monkeypatch):
    resolver = PathResolver(AppPaths(tmp_path))
    before = CollectionSettings(max_works=45)
    save_settings(resolver, before)
    def refuse(*args): raise OSError('Synthetic publish refusal')
    monkeypatch.setattr(os,'replace',refuse)
    with pytest.raises(OSError):save_settings(resolver,replace(before,max_works=46))
    assert load_settings(resolver)==before
    assert tuple((tmp_path/'config').glob('.collection-*.tmp'))==()


def test_unknown_sensitive_fields_and_oversize_preferences_are_rejected(tmp_path):
    resolver=PathResolver(AppPaths(tmp_path));path=tmp_path/'config/collection_v2.json';path.parent.mkdir()
    path.write_text(json.dumps({'version':1,'preferences':{'token':'synthetic'}}),encoding='utf-8')
    with pytest.raises(SettingsError):load_settings(resolver)
    path.write_bytes(b' '*33000)
    with pytest.raises(SettingsError):load_settings(resolver)
