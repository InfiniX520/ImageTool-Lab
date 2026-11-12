"""Independent checks at the real Tk/controller/background ownership boundary."""
import sys
import subprocess
import textwrap


def test_disposed_view_cannot_finalize_tk_variables_in_a_later_worker():
    # Tcl can abort the whole process for a wrong-thread destructor. Observe
    # that boundary in a bounded child so a regression still saves its JUnit.
    code=textwrap.dedent('''
        import gc,sys,threading,tkinter as tk
        from gui.collection_view import CollectionView
        errors=[]
        sys.unraisablehook=lambda value:errors.append(type(value.exc_value).__name__)
        gc.disable()
        for implicit in (False,True):
            for number in range(3):
                root=tk.Tk();root.withdraw()
                view=CollectionView(root,lambda action,payload:None)
                if not implicit:
                    view.dispose();view.destroy()
                root.destroy()
                del view,root
                worker=threading.Thread(target=gc.collect,name='synthetic-later-worker')
                worker.start();worker.join(1)
                assert not worker.is_alive(), 'Tk destruction blocked a later worker'
                assert errors==[], errors
        print('owned_tk_resources_released')
    ''')
    result=subprocess.run([sys.executable,'-s','-B','-X','utf8','-c',code],capture_output=True,
        text=True,encoding='utf-8',timeout=15)
    assert result.returncode==0, result.stderr[-4000:]
    assert 'owned_tk_resources_released' in result.stdout
