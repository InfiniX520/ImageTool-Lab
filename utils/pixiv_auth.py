#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Pixiv Refresh Token 获取工具 - 浏览器自动填充版本

功能:
    通过 OAuth PKCE 流程获取 Pixiv 的 Refresh Token
    使用用户的真实浏览器（带历史记录和保存的密码）
    用户只需点击登录，程序自动捕获 code

使用方法:
    1. 运行此脚本: python -m utils.pixiv_auth
    2. 浏览器会自动打开登录页面（使用你的浏览器配置）
    3. 浏览器会自动填充账号密码（如果之前保存过）
    4. 点击登录按钮
    5. 程序自动捕获 code 并获取 Token

依赖:
    pip install requests selenium
"""

# ============================================================
# 导入模块
# ============================================================

import atexit
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from base64 import urlsafe_b64encode
from hashlib import sha256
from secrets import token_urlsafe
from urllib.parse import urlencode

import requests

SELENIUM_AVAILABLE = None
webdriver = None
EdgeOptions = None
ChromeOptions = None
ACTIVE_DRIVER = None
_EXIT_HANDLERS_READY = False
_WIN_CTRL_HANDLER = None


def ensure_selenium_available():
    """惰性导入 selenium，降低脚本冷启动开销。"""
    global SELENIUM_AVAILABLE
    global webdriver
    global EdgeOptions
    global ChromeOptions

    if SELENIUM_AVAILABLE is not None:
        return SELENIUM_AVAILABLE

    try:
        from selenium import webdriver as _webdriver
        from selenium.webdriver.chrome.options import Options as _ChromeOptions
        from selenium.webdriver.edge.options import Options as _EdgeOptions

        webdriver = _webdriver
        EdgeOptions = _EdgeOptions
        ChromeOptions = _ChromeOptions
        SELENIUM_AVAILABLE = True
    except ImportError:
        SELENIUM_AVAILABLE = False

    return SELENIUM_AVAILABLE


# ============================================================
# 常量配置
# ============================================================

USER_AGENT = "PixivAndroidApp/5.0.234 (Android 11; Pixel 5)"
REDIRECT_URI = "https://app-api.pixiv.net/web/v1/users/auth/pixiv/callback"
LOGIN_URL = "https://app-api.pixiv.net/web/v1/login"
AUTH_TOKEN_URL = "https://oauth.secure.pixiv.net/auth/token"
CLIENT_ID = "MOBrBDS8blbauoSck0ZfDbtuzpyT"
CLIENT_SECRET = "lsACyCD94FhDUtGTXi3QzcFE2uU1hqtDaKeqrdwj"
HASH_SECRET = "28c1fdd170a5204386cb1313c7077b34f83e4aaf4aa829ce78c231e05b0bae2c"


# ============================================================
# 工具函数
# ============================================================


def s256(data):
    """SHA256 哈希并 Base64 编码"""
    return urlsafe_b64encode(sha256(data).digest()).rstrip(b"=").decode("ascii")


def oauth_pkce():
    """生成 PKCE 验证码对"""
    code_verifier = token_urlsafe(32)
    code_challenge = s256(code_verifier.encode("ascii"))
    return code_verifier, code_challenge


def extract_code(text):
    """从文本中提取 code"""
    if not text:
        return None

    patterns = [
        r"code=([a-zA-Z0-9_-]{40,})",
        r"pixiv://account/login\?code=([a-zA-Z0-9_-]{40,})",
        r"^([a-zA-Z0-9_-]{40,})$",
    ]

    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(1)

    return None


def get_default_browser():
    """获取系统默认浏览器"""
    try:
        import winreg

        # 读取默认浏览器
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\http\UserChoice",
        ) as key:
            prog_id = winreg.QueryValueEx(key, "ProgId")[0]

            if "Chrome" in prog_id:
                return "chrome"
            elif "Edge" in prog_id or "MSEdge" in prog_id:
                return "edge"
            elif "Firefox" in prog_id:
                return "firefox"
            else:
                return "chrome"  # 默认尝试 Chrome
    except:
        # 如果无法读取，尝试检测已安装的浏览器
        return "chrome"  # 默认 Chrome


def create_driver_with_profile():
    """创建使用用户配置的 WebDriver（使用默认浏览器）"""
    if not ensure_selenium_available():
        return None

    driver = None

    # 检测默认浏览器
    default_browser = get_default_browser()
    print(f"[*] 检测到默认浏览器: {default_browser.upper()}")

    # 根据默认浏览器创建驱动
    if default_browser == "chrome":
        # 尝试 Chrome
        try:
            options = ChromeOptions()
            options.add_experimental_option("excludeSwitches", ["enable-automation"])
            options.add_experimental_option("useAutomationExtension", False)

            # 启用性能日志
            options.set_capability("goog:loggingPrefs", {"performance": "ALL", "browser": "ALL"})
            options.add_argument("--enable-logging")
            options.add_argument("--v=1")

            driver = webdriver.Chrome(options=options)
            print("[+] 使用 Chrome 浏览器（带用户配置）")

            # 启用 Network 域
            try:
                driver.execute_cdp_cmd("Network.enable", {})
                print("[+] 已启用网络监听")
            except:
                print("[!] 无法启用网络监听，将使用备用方法")

            return driver
        except Exception as e:
            print(f"[!] Chrome 不可用: {e}")
            print("[*] 尝试使用 Edge...")

    # 尝试 Edge（作为备选）
    try:
        options = EdgeOptions()
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        options.add_experimental_option("useAutomationExtension", False)

        # 启用性能日志
        options.set_capability("goog:loggingPrefs", {"performance": "ALL", "browser": "ALL"})
        options.add_argument("--enable-logging")
        options.add_argument("--v=1")

        driver = webdriver.Edge(options=options)
        print("[+] 使用 Edge 浏览器（带用户配置）")

        # 启用 Network 域
        try:
            driver.execute_cdp_cmd("Network.enable", {})
            print("[+] 已启用网络监听")
        except:
            print("[!] 无法启用网络监听，将使用备用方法")

        return driver
    except Exception as e:
        print(f"[!] Edge 不可用: {e}")

    return None


def is_driver_alive(driver) -> bool:
    """检查 webdriver 服务进程是否仍存活。"""
    try:
        service = getattr(driver, "service", None)
        process = getattr(service, "process", None)
        if process is None:
            return True
        return process.poll() is None
    except Exception:
        return True


def force_cleanup_driver(driver, quit_timeout: float = 3.0) -> None:
    """尽力关闭 webdriver；必要时强制回收驱动进程树。"""
    if not driver:
        return

    service_pid = None
    try:
        service = getattr(driver, "service", None)
        process = getattr(service, "process", None)
        service_pid = getattr(process, "pid", None)
    except Exception:
        service_pid = None

    def _quit_driver() -> None:
        try:
            driver.quit()
        except Exception:
            pass

    quitter = threading.Thread(target=_quit_driver, daemon=True)
    quitter.start()
    quitter.join(timeout=quit_timeout)

    if quitter.is_alive():
        print("[!] 浏览器退出超时，正在执行强制清理...")

    try:
        service = getattr(driver, "service", None)
        if service:
            service.stop()
    except Exception:
        pass

    if service_pid and os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(service_pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        except Exception:
            pass


def cleanup_active_driver() -> None:
    """清理当前活动驱动，避免残留占用。"""
    global ACTIVE_DRIVER
    current = ACTIVE_DRIVER
    ACTIVE_DRIVER = None
    if current:
        force_cleanup_driver(current)


def _signal_cleanup_handler(signum, frame) -> None:
    """处理中断信号时先清理 webdriver 相关进程。"""
    del frame
    print(f"\n[!] 接收到终止信号({signum})，正在清理进程...")
    cleanup_active_driver()
    raise SystemExit(130)


def install_exit_handlers() -> None:
    """安装退出清理钩子，覆盖 Ctrl+C/终止信号与普通退出。"""
    global _EXIT_HANDLERS_READY
    global _WIN_CTRL_HANDLER
    if _EXIT_HANDLERS_READY:
        return

    atexit.register(cleanup_active_driver)

    for sig_name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, sig_name, None)
        if sig is not None:
            try:
                signal.signal(sig, _signal_cleanup_handler)
            except Exception:
                pass

    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            ctrl_close_codes = {2, 5, 6}
            handler_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)

            def _windows_ctrl_handler(ctrl_type: int) -> bool:
                if ctrl_type in ctrl_close_codes:
                    cleanup_active_driver()
                return False

            _WIN_CTRL_HANDLER = handler_type(_windows_ctrl_handler)
            ctypes.windll.kernel32.SetConsoleCtrlHandler(_WIN_CTRL_HANDLER, True)
        except Exception:
            pass

    _EXIT_HANDLERS_READY = True


def monitor_browser_for_code(driver, timeout=300):
    """监控浏览器网络请求和 URL，自动捕获 code"""
    print("[*] 等待你在浏览器中登录...")
    print("[*] 浏览器会自动填充账号密码（如果之前保存过）")
    print("[*] 请点击登录按钮")
    print("[*] 程序会自动监听网络请求")
    print()

    start_time = time.time()
    last_status_bucket = -1
    last_url = ""
    last_browser_log_check = 0.0
    last_page_probe_check = 0.0
    last_non_login_ts = None
    blank_page_hits = 0
    poll_interval = 0.5

    while (time.time() - start_time) < timeout:
        try:
            if not is_driver_alive(driver):
                print("[!] 浏览器驱动已退出")
                return None

            try:
                handles = driver.window_handles
                if not handles:
                    print("[!] 浏览器窗口已关闭")
                    return None
            except Exception as window_error:
                window_error_text = str(window_error)
                if (
                    "no such window" in window_error_text
                    or "invalid session id" in window_error_text
                ):
                    print("[!] 浏览器窗口已关闭")
                    return None

            now_ts = time.time()

            # 方法1: 检查当前 URL
            try:
                current_url = driver.current_url
                current_url_lower = (current_url or "").lower()

                if current_url_lower.startswith(
                    (
                        "about:blank",
                        "data:,",
                        "chrome://newtab",
                        "chrome://new-tab-page",
                        "edge://newtab",
                        "edge://new-tab-page",
                    )
                ):
                    blank_page_hits += 1
                else:
                    blank_page_hits = 0

                if blank_page_hits >= 6 and (now_ts - start_time) > 3:
                    print("[!] 浏览器页面已关闭或处于空白页，结束等待")
                    return None

                in_login_flow = ("pixiv.net" in current_url_lower) or (
                    "pixiv://" in current_url_lower
                )
                if not in_login_flow and current_url_lower:
                    if last_non_login_ts is None:
                        last_non_login_ts = now_ts
                    elif now_ts - last_non_login_ts >= 15:
                        print("[!] 当前页面已离开 Pixiv 登录流程，结束等待")
                        return None
                else:
                    last_non_login_ts = None

                if current_url != last_url:
                    last_url = current_url
                    print(f"[*] URL 变化: {current_url[:60]}...")

                    # 检查 URL 是否包含 code
                    if "code=" in current_url or "pixiv://" in current_url:
                        print(f"[+] 检测到回调 URL")
                        code = extract_code(current_url)
                        if code:
                            print(f"[+] 成功从 URL 捕获 code!")
                            return code
            except:
                pass

            # 方法2: 检查浏览器性能日志（Network 请求）
            try:
                logs = driver.get_log("performance")
                for entry in logs:
                    try:
                        payload = json.loads(entry.get("message", "{}"))
                        message = payload.get("message", {})
                        method = message.get("method", "")
                        params = message.get("params", {})

                        if method == "Network.requestWillBeSent":
                            request = params.get("request", {})
                            url = request.get("url", "")
                            if url and ("pixiv://" in url or ("code=" in url and "pixiv" in url)):
                                print(f"[+] 检测到网络请求: {url[:60]}...")
                                code = extract_code(url)
                                if code:
                                    print(f"[+] 成功从 Network 请求捕获 code!")
                                    return code

                        elif method == "Page.frameNavigated":
                            frame = params.get("frame", {})
                            url = frame.get("url", "")
                            if url and ("pixiv://" in url or ("code=" in url and "pixiv" in url)):
                                print(f"[+] 检测到页面导航: {url[:60]}...")
                                code = extract_code(url)
                                if code:
                                    print(f"[+] 成功从页面导航捕获 code!")
                                    return code

                        elif method == "Network.responseReceived":
                            response = params.get("response", {})
                            url = response.get("url", "")
                            if url and ("pixiv://" in url or ("code=" in url and "pixiv" in url)):
                                print(f"[+] 检测到响应: {url[:60]}...")
                                code = extract_code(url)
                                if code:
                                    print(f"[+] 成功从响应捕获 code!")
                                    return code
                    except Exception:
                        continue
            except Exception:
                pass

            # 方法3: 检查浏览器日志
            if now_ts - last_browser_log_check >= 1.0:
                last_browser_log_check = now_ts
                try:
                    browser_logs = driver.get_log("browser")
                    for entry in browser_logs:
                        message = entry.get("message", "")
                        if "pixiv://" in message or "code=" in message:
                            print(f"[+] 检测到浏览器日志: {message[:60]}...")
                            code = extract_code(message)
                            if code:
                                print(f"[+] 成功从浏览器日志捕获 code!")
                                return code
                except Exception:
                    pass

            # 方法4: 执行 JavaScript 检查页面
            if now_ts - last_page_probe_check >= 3.0:
                last_page_probe_check = now_ts
                try:
                    # 检查页面源码
                    page_source = driver.page_source
                    if "pixiv://" in page_source:
                        code = extract_code(page_source)
                        if code:
                            print(f"[+] 成功从页面源码捕获 code!")
                            return code

                    # 检查 window.location
                    js_url = driver.execute_script("return window.location.href;")
                    if js_url and ("pixiv://" in js_url or "code=" in js_url):
                        print(f"[+] 检测到 JS location: {js_url[:60]}...")
                        code = extract_code(js_url)
                        if code:
                            print(f"[+] 成功从 JS location 捕获 code!")
                            return code
                except Exception:
                    pass

            time.sleep(poll_interval)

            # 每 10 秒显示一次提示
            elapsed = int(time.time() - start_time)
            status_bucket = elapsed // 10
            if status_bucket > 0 and status_bucket != last_status_bucket:
                last_status_bucket = status_bucket
                remaining = timeout - elapsed
                print(f"[*] 等待中... (剩余 {remaining} 秒)")
                time.sleep(0.1)

        except Exception as e:
            error_str = str(e)
            # 浏览器被关闭
            if "invalid session id" in error_str or "disconnected" in error_str:
                print(f"[!] 浏览器已关闭")
                return None
            # 其他错误继续尝试
            time.sleep(0.5)

    print("[-] 超时：未能捕获 code")
    return None


def request_token(code, code_verifier):
    """请求 token"""
    try:
        local_time = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
        hash_string = f"{local_time}{HASH_SECRET}"
        x_client_hash = hashlib.md5(hash_string.encode()).hexdigest()

        headers = {
            "User-Agent": USER_AGENT,
            "X-Client-Time": local_time,
            "X-Client-Hash": x_client_hash,
        }

        response = requests.post(
            AUTH_TOKEN_URL,
            data={
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "code": code,
                "code_verifier": code_verifier,
                "grant_type": "authorization_code",
                "include_policy": "true",
                "redirect_uri": REDIRECT_URI,
            },
            headers=headers,
            timeout=30,
        )

        data = response.json()

        if "refresh_token" in data:
            return True, data
        else:
            error_msg = data.get("errors", {}).get("system", {}).get("message", "未知错误")
            return False, error_msg

    except Exception as e:
        return False, str(e)


def safe_input(prompt: str) -> str:
    """在可交互终端中读取输入；不可交互时直接返回空串。"""
    try:
        if sys.stdin is None or not sys.stdin.isatty():
            return ""
    except Exception:
        return ""

    try:
        return input(prompt)
    except (EOFError, KeyboardInterrupt):
        return ""


# ============================================================
# 主函数
# ============================================================


def get_token():
    """获取 Pixiv Refresh Token - 浏览器自动填充版本"""
    print("=" * 60)
    print("PiKEY - Pixiv Token 获取工具（浏览器自动填充）")
    print("=" * 60)
    print()

    # 检查 selenium
    if not ensure_selenium_available():
        print("[-] 未安装 selenium 库")
        print("[*] 请在指定环境中显式安装: python -m pip install selenium")
        return

    print("✨ 使用浏览器保存的密码，自动填充！")
    print()
    print("=" * 60)
    print("📌 使用说明")
    print("=" * 60)
    print()
    print("1️⃣  程序会打开浏览器（使用你的浏览器配置）")
    print()
    print("2️⃣  浏览器会自动跳转到 Pixiv 登录页面")
    print()
    print("3️⃣  如果你之前保存过密码，浏览器会自动填充")
    print("    👉 账号密码会自动出现在输入框")
    print()
    print("4️⃣  你只需要点击 '登录' 按钮")
    print("    👉 就这一步需要你操作！")
    print()
    print("5️⃣  程序会自动捕获 code 并获取 Token")
    print()
    print("=" * 60)
    print()
    print("💡 提示：")
    print("   • 使用你的真实浏览器配置")
    print("   • 可以使用浏览器保存的密码")
    print("   • 只需点击登录按钮")
    print("   • 其他全自动")
    print()
    print("=" * 60)
    print()

    safe_input("按回车键打开浏览器...")
    print()

    # 生成 PKCE
    code_verifier, code_challenge = oauth_pkce()

    login_params = {
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "client": "pixiv-android",
    }

    login_url = f"{LOGIN_URL}?{urlencode(login_params)}"

    # 创建浏览器驱动
    print("[*] 正在启动浏览器...")
    driver = create_driver_with_profile()
    global ACTIVE_DRIVER
    ACTIVE_DRIVER = driver

    if not driver:
        print("[-] 无法创建浏览器驱动")
        print("[!] 请确保已安装 Edge 或 Chrome 浏览器")
        return

    try:
        print("[*] 正在打开登录页面...")
        driver.get(login_url)

        print()
        print("=" * 60)
        print("请在浏览器中操作")
        print("=" * 60)
        print()
        print("👉 如果浏览器自动填充了账号密码")
        print("   直接点击 '登录' 按钮")
        print()
        print("👉 如果没有自动填充")
        print("   手动输入账号密码，然后点击 '登录'")
        print()
        print("👉 登录后，程序会自动捕获 Token")
        print("   请不要关闭浏览器")
        print()
        print("=" * 60)
        print()

        # 监控浏览器，等待登录
        code = monitor_browser_for_code(driver, timeout=300)

        if code is None:
            print()
            print("=" * 60)
            print("❌ 获取失败")
            print("=" * 60)
            print()
            print("💡 可能的原因：")
            print("   • 未在浏览器中完成登录")
            print("   • 登录失败（账号密码错误）")
            print("   • 网络连接问题")
            print("   • 浏览器被关闭")
            print()
            print("💡 建议：")
            print("   • 重新运行程序")
            print("   • 确保在浏览器中点击了登录")
            print("   • 检查账号密码是否正确")
            print()
            print("=" * 60)
            return

        print()
        print("[*] 正在获取 Token...")

        # 请求 token
        success, result = request_token(code, code_verifier)

        if success:
            print()
            print("=" * 60)
            print("✓ 获取成功！")
            print("=" * 60)
            print()
            print("【Refresh Token】")
            print(result["refresh_token"])
            print()
            print("【Access Token】")
            print(result["access_token"])
            print()
            print(f"【有效期】{result.get('expires_in', 0)} 秒")
            print()
            print("=" * 60)
            print("请复制上面的 Refresh Token，在 PiDL 中登录使用")
            print("Refresh Token 长期有效，请妥善保管！")
            print("=" * 60)
        else:
            print()
            print("[-] 获取失败")
            print(f"[-] 错误: {result}")
            print()
            print("💡 建议：")
            print("   • 重新运行程序")
            print("   • 检查网络连接")

    finally:
        cleanup_active_driver()


# ============================================================
# 程序入口
# ============================================================

if __name__ == "__main__":
    try:
        install_exit_handlers()
        get_token()
    except KeyboardInterrupt:
        print("\n\n[!] 已取消")
    except Exception as e:
        print(f"\n\n[-] 发生错误: {e}")
