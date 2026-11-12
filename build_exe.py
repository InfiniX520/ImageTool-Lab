#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Reproducible PyInstaller build entry point.

The previous script executed a build and installed packages merely by being
imported.  This version is an explicit CLI: it never installs dependencies,
never copies user data/Cookies, and only removes generated directories when
``--clean`` is requested.
"""

from __future__ import annotations

import argparse
import glob
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="PiDL / PiKEY PyInstaller builder")
    parser.add_argument("--pidl-mode", choices=("onedir", "onefile"), default="onedir")
    parser.add_argument("--pikey-mode", choices=("onedir", "onefile"), default="onefile")
    parser.add_argument("--output-dir", default="release", help="generated release directory")
    parser.add_argument("--clean", action="store_true", help="remove generated build/dist/release outputs first")
    parser.add_argument("--skip-pikey", action="store_true", help="build only the main application")
    parser.add_argument("--skip-zip", action="store_true", help="do not create a release zip")
    return parser.parse_args(argv)


def bundle_flag(mode: str) -> list[str]:
    return ["--onefile"] if mode == "onefile" else ["--onedir"]


def find_dll_binary_args(search_root: str, dll_names: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    roots = [search_root, os.path.join(search_root, "Library", "bin")]
    for dll_name in dll_names:
        for root in roots:
            matches = glob.glob(os.path.join(root, "**", dll_name), recursive=True)
            if not matches:
                continue
            path = os.path.abspath(matches[0])
            if path not in seen:
                seen.add(path)
                result.append(f"--add-binary={path};.")
            break
    return result


def _safe_remove_generated(path: Path, root: Path) -> None:
    """Remove only a known generated child after verifying its boundary."""
    resolved_root = root.resolve()
    resolved_path = path.resolve()
    if resolved_path.parent != resolved_root or resolved_path.name not in {"build", "dist", "release"}:
        raise ValueError(f"refusing to remove outside generated workspace: {resolved_path}")
    if resolved_path.exists():
        shutil.rmtree(resolved_path)


def prepare_release_dir(root: Path, requested: str, *, clean: bool) -> Path:
    target = (root / requested).resolve() if not Path(requested).is_absolute() else Path(requested).resolve()
    if clean and target.exists():
        # A release path is user-selectable, so only clean it when it is a
        # direct child of this repository and explicitly requested.
        _safe_remove_generated(target, root)
    if target.exists():
        target = target.parent / f"{target.name}_{time.strftime('%Y%m%d_%H%M%S')}"
    target.mkdir(parents=True, exist_ok=True)
    return target


def _require_pyinstaller():
    try:
        import PyInstaller
    except ImportError as exc:  # pragma: no cover - depends on build env
        raise SystemExit(
            "PyInstaller 未安装；请在指定环境中显式执行 "
            "python -m pip install pyinstaller 后重试。"
        ) from exc
    return PyInstaller


def _run(command: list[str], root: Path) -> None:
    print("+", " ".join(command))
    subprocess.check_call(command, cwd=root)


def build(args: argparse.Namespace) -> Path:
    root = Path(__file__).resolve().parent
    pyinstaller = _require_pyinstaller()
    print(f"PyInstaller {pyinstaller.__version__}; interpreter: {sys.executable}")

    if args.clean:
        for name in ("build", "dist"):
            _safe_remove_generated(root / name, root)

    conda_env = os.path.dirname(sys.executable)
    tk_dlls = find_dll_binary_args(conda_env, ["tcl86t.dll", "tk86t.dll", "tcl86.dll", "tk86.dll"])
    runtime_dlls = find_dll_binary_args(
        conda_env,
        ["libssl-3-x64.dll", "libcrypto-3-x64.dll", "libssl-1_1-x64.dll", "libcrypto-1_1-x64.dll", "LIBBZ2.dll", "liblzma.dll", "ffi.dll", "libexpat.dll"],
    )
    common = [
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
        "--icon=NONE", "--add-data=config;config", "--hidden-import=pixivpy3",
        "--hidden-import=yaml", "--hidden-import=requests", "--hidden-import=cloudscraper",
        "--hidden-import=tkinter", "--hidden-import=tkinter.ttk",
        "--hidden-import=tkinter.scrolledtext", "--hidden-import=tkinter.filedialog",
        "--hidden-import=tkinter.messagebox", "--collect-all=pixivpy3",
        "--copy-metadata=pixivpy3", "--exclude-module=pandas",
        "--exclude-module=matplotlib", "--exclude-module=tensorflow", "--exclude-module=PyQt5",
    ]

    pidl_cmd = common + ["--name=PiDL", *bundle_flag(args.pidl_mode), "--windowed", *tk_dlls, *runtime_dlls, "main.py"]
    _run(pidl_cmd, root)

    if not args.skip_pikey:
        pikey_cmd = [
            sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
            "--name=PiKEY", *bundle_flag(args.pikey_mode), "--console", "--icon=NONE",
            "--hidden-import=requests", "--hidden-import=selenium",
            "--collect-submodules=selenium.webdriver", "--hidden-import=_ssl", "--hidden-import=_hashlib",
            "--exclude-module=pandas", "--exclude-module=matplotlib", "--exclude-module=tensorflow",
            "--exclude-module=PyQt5", *runtime_dlls, "utils/pixiv_auth.py",
        ]
        _run(pikey_cmd, root)

    release_dir = prepare_release_dir(root, args.output_dir, clean=args.clean)
    pidl_source = root / "dist" / ("PiDL.exe" if args.pidl_mode == "onefile" else "PiDL")
    if args.pidl_mode == "onefile":
        shutil.copy2(pidl_source, release_dir / "PiDL.exe")
    else:
        shutil.copytree(pidl_source, release_dir / "PiDL")

    if not args.skip_pikey:
        pikey_source = root / "dist" / ("PiKEY.exe" if args.pikey_mode == "onefile" else "PiKEY")
        if args.pikey_mode == "onefile":
            shutil.copy2(pikey_source, release_dir / "PiKEY.exe")
        else:
            shutil.copytree(pikey_source, release_dir / "PiKEY")

    # ``AppPaths`` resolves user data next to the executable.  In onedir
    # builds that executable lives inside ``PiDL/``; place the editable
    # default config and instructions at the same runtime root instead of one
    # directory above it.
    runtime_root = release_dir if args.pidl_mode == "onefile" else release_dir / "PiDL"
    config_out = runtime_root / "config"
    config_out.mkdir(parents=True, exist_ok=True)
    shutil.copy2(root / "config" / "default_config.yaml", config_out / "default_config.yaml")
    (runtime_root / "使用说明.txt").write_text(
        "PiDL 使用说明\n\n"
        "1. 在程序目录放置 www.pixiv.net_cookies.txt（Netscape 格式）。\n"
        "2. 启动 PiDL.exe，输入 Refresh Token 登录。\n"
        "3. Cookie、Token、下载记录不会被打包，请自行保管。\n",
        encoding="utf-8",
    )

    if args.clean:
        _safe_remove_generated(root / "build", root)
        for spec in (root / "PiDL.spec", root / "PiKEY.spec"):
            spec.unlink(missing_ok=True)

    if not args.skip_zip:
        archive_base = root / f"PiDL-package-{time.strftime('%Y%m%d_%H%M%S')}"
        shutil.make_archive(str(archive_base), "zip", root_dir=release_dir)
    return release_dir


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        release = build(args)
    except subprocess.CalledProcessError as exc:
        print(f"打包失败，退出码 {exc.returncode}", file=sys.stderr)
        return exc.returncode or 1
    print(f"打包完成: {release}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
