"""A real narrow window at 150% must still switch to the stacked layout."""
import gc
import tkinter as tk

from gui.collection_view import CollectionView


def test_real_narrow_scaled_window_reflows_settings_and_follow_options():
    gc.collect()
    root=tk.Tk();root.withdraw();root.tk.call('tk','scaling',144/72)
    view=CollectionView(root,lambda *args:None);view.pack(fill='both',expand=True)
    try:
        root.geometry('760x720+30+30');root.deiconify();root.attributes('-disabled',True)
        view.show_page('settings');root.update()
        assert view.winfo_width()==760
        assert int(view._settings_side.grid_info()['row'])==1
        assert int(view._settings_side.grid_info()['column'])==0
        assert not view._follow_wide
        view._settings_scroll.canvas.yview_moveto(1);root.update()
        button=view._action_buttons['update_authorization'][0]
        assert button.winfo_ismapped()
        left=button.winfo_rootx()-root.winfo_rootx()
        assert 0<=left<root.winfo_width()
    finally:
        view.dispose();root.destroy()
        del view,root
        gc.collect()
