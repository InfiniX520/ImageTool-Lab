#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Pixiv Refresh Token 获取工具

功能:
    通过 OAuth PKCE 流程获取 Pixiv 的 Refresh Token

使用方法:
    1. 运行此脚本: python pixiv_get_token.py
    2. 浏览器会自动打开 Pixiv 登录页面
    3. 按 F12 打开开发者工具，切换到 Network 标签
    4. 登录你的 Pixiv 账号
    5. 查找 pixiv:// 开头的请求，复制其中的 code 参数
    6. 粘贴到命令行中按回车
    7. 获得 refresh_token，复制保存好

依赖:
    pip install requests
"""

# ============================================================
# 导入模块
# ============================================================

from base64 import urlsafe_b64encode
from hashlib import sha256
from secrets import token_urlsafe
from urllib.parse import urlencode
import webbrowser
import requests

# ============================================================
# 常量配置
# ============================================================

USER_AGENT = "PixivAndroidApp/5.0.234 (Android 11; Pixel 5)"
REDIRECT_URI = "https://app-api.pixiv.net/web/v1/users/auth/pixiv/callback"
LOGIN_URL = "https://app-api.pixiv.net/web/v1/login"
AUTH_TOKEN_URL = "https://oauth.secure.pixiv.net/auth/token"
CLIENT_ID = "MOBrBDS8blbauoSck0ZfDbtuzpyT"
CLIENT_SECRET = "lsACyCD94FhDUtGTXi3QzcFE2uU1hqtDaKeqrdwj"


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


# ============================================================
# 主函数
# ============================================================

def get_token():
    """获取 Pixiv Refresh Token"""
    print("=" * 50)
    print("Pixiv Refresh Token 获取工具")
    print("=" * 50)
    
    code_verifier, code_challenge = oauth_pkce()
    
    login_params = {
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "client": "pixiv-android",
    }
    
    login_url = f"{LOGIN_URL}?{urlencode(login_params)}"
    
    print("\n[*] 正在打开浏览器...")
    print("[*] 如果浏览器没有自动打开，请手动访问以下链接：")
    print(login_url)
    
    webbrowser.open(login_url)
    
    print("\n" + "-" * 50)
    print("操作步骤：")
    print("1. 在浏览器中登录你的 Pixiv 账号")
    print("   a. 按 F12 打开开发者工具")
    print("   b. 切换到 Network 标签")
    print("2. 登录你的 Pixiv 账号")
    print("   a. 查看是否有跳转请求(一般会跳转失败,无所谓)")
    print("   b. 看看有没有 pixiv:// 开头的请求，或者红色的失败请求")
    print("   c. 快速复制(Copy --> Copy URL)")
    print("3. 粘贴到命令行中按回车")
    print("-" * 50)
    
    try:
        code = input("\n请粘贴 code: ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\n[!] 已取消")
        return
    
    if not code:
        print("[-] 错误: code 不能为空")
        return
    
    # 自动清理 code，去掉多余的参数
    if '&' in code:
        code = code.split('&')[0]
    if 'code=' in code:
        code = code.split('code=')[-1].split('&')[0]
    
    print(f"[*] 提取的 code: {code}")
    print("\n[*] 正在获取 token...")
    
    try:
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
            headers={"User-Agent": USER_AGENT},
            timeout=30
        )
        
        data = response.json()
        
        if "refresh_token" in data:
            print("\n" + "=" * 50)
            print("[+] 获取成功！")
            print("=" * 50)
            print(f"\n[+] Refresh Token: {data['refresh_token']}")
            print(f"\n[*] Access Token: {data['access_token']}")
            print(f"[*] 有效期: {data.get('expires_in', 0)} 秒")
            print("\n[*] 请保存好你的 Refresh Token，用于 pixiv_downloader.py 登录")
            print("[*] Refresh Token 长期有效，不需要频繁获取")
        else:
            print("\n[-] 获取失败:")
            print(data)
            if "error" in data:
                print(f"[-] 错误: {data.get('error')}")
                print(f"[-] 描述: {data.get('error_description', '未知错误')}")
    
    except Exception as e:
        print(f"\n[-] 请求失败: {e}")


# ============================================================
# 程序入口
# ============================================================

if __name__ == "__main__":
    get_token()
    input("\n按回车键退出...")
