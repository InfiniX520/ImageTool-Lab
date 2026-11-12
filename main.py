#!/usr/bin/env python3
"""JpgPng entry: portable new library, explicit offline demo and legacy recovery."""
from __future__ import annotations

import argparse
from pathlib import Path


def main(argv=None):
    parser=argparse.ArgumentParser(description='JpgPng：关注画师历史补全与原图收藏')
    mode=parser.add_mutually_exclusive_group()
    mode.add_argument('--demo',action='store_true',help='离线演示，使用独立的示例资料目录')
    mode.add_argument('--legacy',action='store_true',help='打开保留的旧版界面；先关闭新版')
    parser.add_argument('--project-root',type=Path,help='指定资料目录；默认在程序旁边，与当前终端目录无关')
    args=parser.parse_args(argv)
    if args.legacy:
        if args.project_root is not None:
            parser.error('旧版入口不支持资料目录覆盖；请使用已保全的旧版资料')
        from gui.main_controller import MainController
        MainController().run()
        return
    from core.app_paths import AppPaths
    from gui.collection_app import build_app
    import tkinter as tk
    default=AppPaths().root
    selected=args.project_root or (default/'.runtime/offline_demo' if args.demo else default)
    paths=AppPaths(selected)
    if args.demo and any((paths.root/name).exists() for name in ('pixiv_config.json','www.pixiv.net_cookies.txt')):
        parser.error('演示目录含已有授权，请选择独立目录')
    root=tk.Tk()
    build_app(root,paths,demo=args.demo)
    root.mainloop()


if __name__=='__main__':
    main()
