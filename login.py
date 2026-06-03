"""
login.py — 小红书创作者中心扫码登录（持久化 Profile）

用法：
    # 有显示器：弹出浏览器，扫码后自动保存
    python login.py

    # 无头服务器：截图到文件，下载后扫码
    python login.py --headless

说明：
    登录成功后，session 持久化到 browser_profile/xhs/（由 config.py 配置）。
    后续 scraper.py 直接复用该 Profile，无需再次登录。
    web_session 有效期通常为数周至数月，失效后重新运行本脚本即可。

策略：
    直接访问创作者中心，未登录时会跳转到登录页或弹出登录弹窗；
    比主站更能确保登录状态的真实可用性。
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from playwright.async_api import async_playwright, Page, BrowserContext

# 独立运行：无需父项目依赖
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

from config import Config
from logger import get_logger

logger = get_logger("login")

XHS_URL = "https://www.xiaohongshu.com"
CREATOR_URL = Config.CREATOR_CENTER_URL
LOGIN_TIMEOUT_SEC = 180  # 等待扫码最长时间（秒）


def _is_logged_in_by_session(web_session: str) -> bool:
    """判断 web_session 是否为真实登录态（以 '04' 开头且长度 ≥30）。"""
    return bool(web_session) and len(web_session) >= 30 and web_session.startswith("04")


def _is_login_url(url: str) -> bool:
    """判断当前 URL 是否为登录页面。"""
    url_lower = url.lower()
    return (
        "login" in url_lower
        or "signin" in url_lower
        or "sign_in" in url_lower
        or "passport" in url_lower
    )


async def _screenshot(page: Page, save_path: str) -> None:
    try:
        await page.screenshot(path=save_path, full_page=False)
        logger.info(f"截图已保存: {save_path}")
    except Exception as e:
        logger.debug(f"截图失败: {e}")


async def _try_trigger_qr_login(page: Page) -> bool:
    """
    尝试触发二维码登录界面。
    1. 先尝试点击"登录"按钮（创作者中心 or 主站）
    2. 然后尝试切换到扫码 Tab
    返回是否找到了 QR 相关元素。
    """
    # 点击登录按钮（顺序尝试多种选择器）
    login_selectors = [
        "[class*='login-btn']",
        "button:has-text('登录')",
        "a:has-text('登录')",
        "text=登录",
        "[data-testid='login']",
        ".login",
    ]
    for sel in login_selectors:
        try:
            el = await page.wait_for_selector(sel, timeout=2000, state="visible")
            if el:
                await el.click()
                await asyncio.sleep(1.5)
                logger.info(f"点击登录按钮: {sel}")
                break
        except Exception:
            continue

    # 切换到扫码登录 Tab（有些页面默认是手机号/密码登录）
    qr_tab_selectors = [
        "text=扫码登录",
        "text=扫一扫",
        "text=二维码登录",
        "[class*='qrcode-tab']",
        "[class*='qr-tab']",
        "[class*='scan']",
        "li:has-text('扫码')",
    ]
    found_qr = False
    for sel in qr_tab_selectors:
        try:
            el = await page.wait_for_selector(sel, timeout=2000, state="visible")
            if el:
                await el.click()
                await asyncio.sleep(1.0)
                logger.info(f"切换到扫码 Tab: {sel}")
                found_qr = True
                break
        except Exception:
            continue

    return found_qr


async def _wait_for_login(
    page: Page, context: BrowserContext, qr_path: str, headless: bool
) -> bool:
    """
    截图当前状态，然后轮询等待登录成功。
    成功判断：1) web_session 变为有效  2) 页面跳转到创作者中心
    """
    await asyncio.sleep(2)
    await _screenshot(page, qr_path)

    if headless:
        logger.info("=" * 60)
        logger.info(f"无头模式：请下载截图 {qr_path}，找到二维码后用小红书 App 扫码")
        logger.info(f"等待扫码（最长 {LOGIN_TIMEOUT_SEC} 秒）…")
        logger.info("=" * 60)
    else:
        logger.info(f"请在浏览器窗口中扫码登录（最长 {LOGIN_TIMEOUT_SEC} 秒）…")

    for elapsed in range(LOGIN_TIMEOUT_SEC):
        await asyncio.sleep(1)

        # 方式 1：检查 URL 是否跳转回创作者中心（登录成功后通常会跳转）
        current_url = page.url
        if "creator.xiaohongshu.com" in current_url and not _is_login_url(current_url):
            logger.info(f"检测到页面跳转到创作者中心: {current_url}")
            # 再验证一次 cookie
            cookies = await context.cookies(XHS_URL)
            ws = next((c["value"] for c in cookies if c["name"] == "web_session"), "")
            logger.info(f"登录成功！web_session={ws[:10]}…(len={len(ws)})，Profile 已持久化。")
            return True

        # 方式 2：检查 cookie web_session 是否变长/变新
        cookies = await context.cookies(XHS_URL)
        ws = next((c["value"] for c in cookies if c["name"] == "web_session"), "")
        if _is_logged_in_by_session(ws):
            logger.info(
                f"登录成功！web_session={ws[:10]}…(len={len(ws)})，Profile 已持久化。"
            )
            return True

        if elapsed % 15 == 14:
            logger.info(f"仍在等待扫码… 已过 {elapsed + 1}s（当前 URL: {current_url[:80]}）")
            await _screenshot(page, qr_path)

    return False


async def login_and_save(headless: bool = False) -> bool:
    profile_dir = Config.BROWSER_PROFILE_DIR
    os.makedirs(profile_dir, exist_ok=True)
    qr_path = os.path.join(profile_dir, "qrcode_login.png")

    logger.info(f"Profile 目录: {profile_dir}")
    if headless:
        logger.info("无头模式：将截图二维码到文件，请下载后用小红书 App 扫码")
    else:
        logger.info("启动浏览器，请在弹出窗口中扫码登录小红书…")

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            user_data_dir=profile_dir,
            headless=headless,
            user_agent=Config.DESKTOP_UA,
            viewport={"width": 1280, "height": 800},
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = context.pages[0] if context.pages else await context.new_page()

        # ── Step 1：直接访问创作者中心登录页
        login_url = f"{CREATOR_URL}/login"
        logger.info(f"访问创作者中心登录页: {login_url}")
        try:
            await page.goto(login_url, wait_until="domcontentloaded", timeout=20000)
        except Exception as e:
            logger.warning(f"创作者中心加载超时，继续等待: {e}")
        await asyncio.sleep(3)

        current_url = page.url
        logger.info(f"当前 URL: {current_url}")

        # ── Step 2：检查是否已经在创作者中心（已登录，被重定向到主页）
        if "creator.xiaohongshu.com" in current_url and not _is_login_url(current_url):
            logger.info(f"检测到已登录，直接进入创作者中心: {current_url}")
            # 保存 storage_state.json 以便 scraper.py 加载
            state_path = os.path.join(profile_dir, "storage_state.json")
            try:
                await context.storage_state(path=state_path)
                logger.info(f"Cookies 已保存到: {state_path}")
            except Exception as e:
                logger.warning(f"保存 storage_state 失败: {e}")
            await _screenshot(page, qr_path)
            await context.close()
            return True

        # ── Step 3：此时应在登录页，截图让用户看到二维码
        # creator.xiaohongshu.com/login 已直接显示二维码，无需额外点击
        logger.info("登录页已加载，请用小红书 App 扫描二维码…")
        await _screenshot(page, qr_path)

        if headless:
            logger.info("=" * 60)
            logger.info(f"无头模式：请下载截图 {qr_path} 找到二维码后用小红书 App 扫码")
            logger.info(f"等待扫码（最长 {LOGIN_TIMEOUT_SEC} 秒）…")
            logger.info("=" * 60)
        else:
            logger.info(f"请在浏览器窗口中扫码登录（最长 {LOGIN_TIMEOUT_SEC} 秒）…")

        # ── Step 4：轮询等待登录成功
        # 判断标准：URL 从 /login 跳转到其他创作者中心页面
        # 并增加 3 秒稳定期，避免跳转中间状态误判
        success = False
        for elapsed in range(LOGIN_TIMEOUT_SEC):
            await asyncio.sleep(1)

            current_url = page.url
            # 跳出登录页，说明登录成功
            if "creator.xiaohongshu.com" in current_url and not _is_login_url(current_url):
                # 稳定期：再等 3 秒确认不是瞬间跳转
                await asyncio.sleep(3)
                current_url = page.url
                if "creator.xiaohongshu.com" in current_url and not _is_login_url(current_url):
                    logger.info(f"登录成功！已进入创作者中心: {current_url}")
                    # ── 关键：显式保存 cookies 到 storage_state.json
                    # Playwright 不会自动保存无过期时间的会话 cookie，必须手动导出
                    state_path = os.path.join(profile_dir, "storage_state.json")
                    try:
                        await context.storage_state(path=state_path)
                        logger.info(f"Cookies 已保存到: {state_path}")
                    except Exception as e:
                        logger.warning(f"保存 storage_state 失败（不影响使用）: {e}")
                    success = True
                    break

            if elapsed % 15 == 14:
                logger.info(
                    f"仍在等待扫码… 已过 {elapsed + 1}s（当前 URL: {current_url[:80]}）"
                )
                await _screenshot(page, qr_path)

        await context.close()

        if not success:
            logger.error(f"等待超时（{LOGIN_TIMEOUT_SEC}s），未检测到登录成功。请重试。")
        return success


def check_session() -> bool:
    """
    快速检查当前 Profile 的登录态（同步版本，供其他模块调用）。
    检查方式：加载 storage_state.json 的 cookie，访问创作者中心，看是否被重定向到登录页。
    """
    import asyncio as _asyncio

    async def _check() -> bool:
        profile_dir = Config.BROWSER_PROFILE_DIR
        state_path = os.path.join(profile_dir, "storage_state.json")
        if not Path(profile_dir).exists() or not Path(state_path).exists():
            return False
        async with async_playwright() as p:
            context = await p.chromium.launch_persistent_context(
                user_data_dir=profile_dir,
                headless=True,
                user_agent=Config.DESKTOP_UA,
            )
            # 加载保存的 cookies
            try:
                import json as _json
                with open(state_path) as f:
                    state = _json.load(f)
                cookies = state.get("cookies", [])
                if cookies:
                    await context.add_cookies(cookies)
            except Exception:
                pass
            page = context.pages[0] if context.pages else await context.new_page()
            try:
                await page.goto(CREATOR_URL, wait_until="domcontentloaded", timeout=15000)
            except Exception:
                pass
            await _asyncio.sleep(3)
            current_url = page.url
            await context.close()
            return "creator.xiaohongshu.com" in current_url and not _is_login_url(current_url)

    return _asyncio.run(_check())


def main() -> None:
    parser = argparse.ArgumentParser(
        description="小红书扫码登录（持久化到 browser_profile/xhs/）"
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="无头模式：截图二维码到文件（适合无显示器服务器）",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="只检查当前 session 是否有效，不触发登录",
    )
    args = parser.parse_args()

    if args.check:
        ok = check_session()
        if ok:
            logger.info("✓ 当前 session 有效，无需重新登录。")
        else:
            logger.warning("✗ session 无效或不存在，请运行 python login.py 重新登录。")
        sys.exit(0 if ok else 1)

    success = asyncio.run(login_and_save(headless=args.headless))
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
