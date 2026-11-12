"""Entry-point routing never opens the real library or authenticates in tests."""
from pathlib import Path
from types import SimpleNamespace
import pytest
import main


def test_normal_entry_is_live_but_only_builds_idle_window(monkeypatch,tmp_path):
    calls=[]
    root=SimpleNamespace(mainloop=lambda:calls.append('loop'))
    monkeypatch.setattr('tkinter.Tk',lambda:root)
    monkeypatch.setattr('gui.collection_app.build_app',lambda r,paths,**kwargs:calls.append((r,paths.root,kwargs)))
    monkeypatch.chdir(tmp_path)
    main.main([])
    assert calls[0][1]==Path(main.__file__).resolve().parent
    assert calls[0][2]=={'demo':False}
    assert not (tmp_path/'download_logs').exists()


def test_demo_uses_separate_root_and_refuses_authorized_folder(monkeypatch,tmp_path):
    calls=[]
    monkeypatch.setattr('tkinter.Tk',lambda:SimpleNamespace(mainloop=lambda:None))
    monkeypatch.setattr('gui.collection_app.build_app',lambda r,paths,**kwargs:calls.append((paths.root,kwargs)))
    main.main(['--demo','--project-root',str(tmp_path)])
    assert calls==[(tmp_path,{'demo':True})]
    (tmp_path/'pixiv_config.json').write_text('{}')
    with pytest.raises(SystemExit) as error:
        main.main(['--demo','--project-root',str(tmp_path)])
    assert error.value.code==2 and len(calls)==1


def test_legacy_recovery_uses_old_controller_without_new_core(monkeypatch):
    calls=[]
    monkeypatch.setattr('gui.main_controller.MainController',lambda:SimpleNamespace(run=lambda:calls.append('legacy')))
    main.main(['--legacy'])
    assert calls==['legacy']
