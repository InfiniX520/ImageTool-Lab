"""OS file pickers and explicit-action notices, owned by the existing Tk root."""
from tkinter import filedialog, messagebox, simpledialog


class NativeDialogs:
    def __init__(self, root):
        self.root = root
        self._busy = False

    def _call(self, function, **kwargs):
        if self._busy:
            return None
        self._busy = True
        try:
            return function(parent=self.root, **kwargs)
        finally:
            self._busy = False

    def choose_directory(self, initialdir):
        return self._call(filedialog.askdirectory, title='选择图片保存目录',
                          initialdir=initialdir, mustexist=True)

    def authorization(self):
        return self._call(simpledialog.askstring, title='更新 Pixiv 授权', show='•',
            prompt='粘贴 Pixiv Refresh Token（不是密码或 Cookie）。\n保存后点击“检查账号状态”验证；已有任务会核对账号。')

    def choose_export(self, kind, initialdir, initialfile):
        return self._call(filedialog.asksaveasfilename,
            title='下载名单另存为' if kind == 'records' else '会话日志另存为',
            initialdir=initialdir, initialfile=initialfile,
            defaultextension='.md' if kind == 'records' else '.txt',
            filetypes=[('Markdown 下载名单', '*.md')] if kind == 'records' else
                      [('文本日志', '*.txt'), ('日志文件', '*.log')], confirmoverwrite=True)

    def choose_artists(self, initialdir):
        return self._call(filedialog.askopenfilename, title='导入画师 ID 或画师链接',
                          initialdir=initialdir, filetypes=[('UTF-8 文本', '*.txt')])

    def error(self, text):
        self._call(messagebox.showerror, title='操作未完成', message=text)

    def info(self, title, text):
        self._call(messagebox.showinfo, title=title, message=text)

    def confirm_cancel(self, title):
        return self._call(messagebox.askyesno, title='取消这个任务？',
            message=f'{title}\n\n已保存的图片和下载记录会保留。\n如果只是暂时停下，可以选择“暂停”。',
            icon='question', default='no') is True
