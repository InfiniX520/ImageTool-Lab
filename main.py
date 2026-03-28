#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""重构版程序入口（保留旧代码并兼容运行）。"""

from gui.main_controller import MainController


def main() -> None:
    """启动 GUI 控制器。"""
    controller = MainController()
    controller.run()


if __name__ == "__main__":
    main()
