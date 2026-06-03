"""
scraper.py — 小红书创作者中心「创作灵感 → 热点话题」采集器

工作流程：
1. 使用持久化登录 Profile 打开 https://creator.xiaohongshu.com
2. 路由拦截所有 API 响应，识别热点话题列表接口
3. 提取话题名 + 观看人数（不点入详情页，降低封控风险）
4. 遍历分区 Tab，聚合多分区话题
5. 输出 TSV 文件

前置条件：
    必须先运行 python login.py 完成扫码登录。

用法：
    # 正常采集（默认无头）
    python scraper.py

    # 接口发现模式（有头浏览器，记录所有 API 响应）
    python scraper.py --discover --no-headless

    # 解析已保存的发现数据（无需打开浏览器）
    python scraper.py --parse-only output/discover/20260602_090000/api_responses.jsonl

    # 调试（有头浏览器）
    python scraper.py --no-headless
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from playwright.async_api import (
    BrowserContext,
    Page,
    Response,
    async_playwright,
)

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

from config import Config
from logger import get_logger

logger = get_logger("scraper")


# ─────────────────────────────────────────────────────────────
# 数据结构
# ─────────────────────────────────────────────────────────────

def _make_topic(
    rank: int,
    topic: str,
    view_count: str,
    category: str,
    collected_at: str,
) -> Dict[str, Any]:
    return {
        "rank": rank,
        "topic": topic.strip(),
        "view_count": view_count.strip() if view_count else "",
        "category": category.strip() if category else "",
        "collected_at": collected_at,
    }


# ─────────────────────────────────────────────────────────────
# JSON 响应解析（Phase 1 后根据实际结构完善）
# ─────────────────────────────────────────────────────────────

def _format_view_count(n: Any) -> str:
    """将观看人数整数格式化为可读文本（如 1483103594 → '14.8亿'）。"""
    try:
        v = int(n)
    except (TypeError, ValueError):
        return str(n) if n else ""
    if v >= 100_000_000:
        return f"{v / 100_000_000:.1f}亿"
    if v >= 10_000:
        return f"{v / 10_000:.1f}万"
    return str(v)


def _parse_select_topic_detail(data: Any, collected_at: str) -> List[Dict]:
    """
    精确解析 /api/galaxy/creator/select/topic/detail 接口响应。
    （Phase 1 确认的接口结构）

    响应结构：
      data[].labelName          → 分区名（如 "美食"、"美妆"）
      data[].selectTopics[]     → 话题列表
        .title                  → 话题名
        .viewNum                → 观看总次数（整数，如 1483103594）
        .joinNum                → 参与笔记数
    """
    results: List[Dict] = []
    if not isinstance(data, dict):
        return results
    code = data.get("code", data.get("result"))
    if code not in (0, None):
        return results
    categories = data.get("data", [])
    if not isinstance(categories, list):
        return results

    rank = 1
    for cat in categories:
        label_name = str(cat.get("labelName", "综合")).strip()
        topics = cat.get("selectTopics", [])
        if not isinstance(topics, list):
            continue
        for item in topics:
            title = str(item.get("title", "")).strip()
            if not title:
                continue
            view_str = _format_view_count(item.get("viewNum", ""))
            results.append(_make_topic(rank, title, view_str, label_name, collected_at))
            rank += 1
    return results


def _try_parse_topic_list(data: Any, category: str, collected_at: str) -> List[Dict]:
    """
    尝试从 API 响应 JSON 中提取话题列表。
    由于 Phase 1 尚未确认接口结构，采用多路启发式匹配：
      - 寻找包含 "title"/"name"/"topic" + "view_count"/"hot_count"/"view_num" 的列表
      - 支持嵌套路径：data.items / data.list / data.topics / data 本身
    Phase 1 完成后，根据实际结构简化本函数。
    """
    results: List[Dict] = []

    def _extract_from_list(items: List, cat: str) -> List[Dict]:
        out: List[Dict] = []
        for idx, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            # 话题名：多种字段名
            name = (
                item.get("title")
                or item.get("name")
                or item.get("topic")
                or item.get("keyword")
                or item.get("topic_name")
                or item.get("hot_word")
                or ""
            )
            if not name or not isinstance(name, str):
                continue
            name = name.strip()
            if len(name) < 2:
                continue

            # 观看人数：多种字段名
            view_raw = (
                item.get("view_count")
                or item.get("hot_count")
                or item.get("view_num")
                or item.get("watching_count")
                or item.get("reader_count")
                or item.get("heat")
                or item.get("score")
                or ""
            )
            view_str = str(view_raw) if view_raw else ""

            # 分区：优先用接口返回的，其次用传入的
            item_cat = (
                item.get("category")
                or item.get("category_name")
                or item.get("tag")
                or cat
            )

            out.append(_make_topic(idx + 1, name, view_str, str(item_cat), collected_at))
        return out

    def _search_nested(obj: Any, depth: int = 0) -> List[Dict]:
        """递归查找可能是话题列表的节点。"""
        if depth > 4:
            return []
        if isinstance(obj, list) and len(obj) > 0:
            candidates = _extract_from_list(obj, category)
            if len(candidates) >= 3:  # 至少 3 个才认为是话题列表
                return candidates
        if isinstance(obj, dict):
            # 优先搜索常见 key
            for key in ["items", "list", "topics", "data", "hot_topics",
                        "topic_list", "hotspot_list", "hot_list", "result"]:
                if key in obj:
                    found = _search_nested(obj[key], depth + 1)
                    if found:
                        return found
            # 兜底：遍历所有值
            for v in obj.values():
                found = _search_nested(v, depth + 1)
                if found:
                    return found
        return []

    if isinstance(data, dict):
        code = data.get("code", data.get("success"))
        if code not in (0, True, 200, "0", "success", None):
            return []  # API 返回错误码，跳过
        found = _search_nested(data)
        results.extend(found)
    elif isinstance(data, list):
        results.extend(_extract_from_list(data, category))

    return results


def _parse_view_count_text(text: str) -> str:
    """规范化观看人数文本（保留原始文本，但添加「人在看」后缀如果是纯数字）。"""
    if not text:
        return ""
    t = text.strip()
    # 如果是纯数字，不修改（保留平台原始格式）
    return t


# ─────────────────────────────────────────────────────────────
# 输出
# ─────────────────────────────────────────────────────────────

def _write_tsv(topics: List[Dict], output_dir: str, ts: datetime) -> str:
    day = ts.strftime("%Y%m%d")
    slot = ts.strftime("%H%M")
    day_dir = Path(output_dir) / day
    day_dir.mkdir(parents=True, exist_ok=True)
    path = day_dir / f"xhs_creator_hot_{day}_{slot}.tsv"

    header = "rank\ttopic\tview_count\tcategory\tcollected_at\n"
    lines = [header]
    for i, t in enumerate(topics, 1):
        lines.append(
            f"{i}\t{t['topic']}\t{t['view_count']}\t{t['category']}\t{t['collected_at']}\n"
        )
    path.write_text("".join(lines), encoding="utf-8")
    return str(path)


def _write_discover_log(
    responses: List[Dict], output_dir: str, ts: datetime
) -> str:
    ts_str = ts.strftime("%Y%m%d_%H%M%S")
    disc_dir = Path(output_dir) / "discover" / ts_str
    disc_dir.mkdir(parents=True, exist_ok=True)
    path = disc_dir / "api_responses.jsonl"
    with open(path, "w", encoding="utf-8") as f:
        for r in responses:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return str(path)


# ─────────────────────────────────────────────────────────────
# 采集器主体
# ─────────────────────────────────────────────────────────────

class CreatorHotScraper:
    """
    小红书创作者中心热点话题采集器。

    Phase 1（接口发现）：运行 scrape(discover=True)，
        所有命中 INSPIRE_API_PATTERNS 的 API 响应都会被记录到
        output/discover/{ts}/api_responses.jsonl，
        同时做一次启发式解析（即使结构不匹配也无害）。

    Phase 2（稳定采集）：根据 Phase 1 发现的接口结构，
        完善 _parse_topic_response()，稳定提取 100 条话题。
    """

    def __init__(
        self,
        profile_dir: Optional[str] = None,
        headless: Optional[bool] = None,
        output_dir: Optional[str] = None,
        topics_limit: Optional[int] = None,
    ):
        self.profile_dir = profile_dir or Config.BROWSER_PROFILE_DIR
        self.headless = headless if headless is not None else Config.HEADLESS
        self.output_dir = output_dir or Config.OUTPUT_DIR
        self.topics_limit = topics_limit or Config.TOPICS_PER_RUN
        self._collected: List[Dict] = []
        self._discover_log: List[Dict] = []

    # ── 工具方法 ──────────────────────────────────────────────

    async def _jitter(
        self,
        min_s: Optional[float] = None,
        max_s: Optional[float] = None,
    ) -> None:
        lo = min_s if min_s is not None else Config.PAGE_ACTION_DELAY_MIN
        hi = max_s if max_s is not None else Config.PAGE_ACTION_DELAY_MAX
        await asyncio.sleep(random.uniform(lo, hi))

    def _should_intercept(self, url: str) -> bool:
        """判断 URL 是否匹配热点话题接口的关键词。"""
        url_lower = url.lower()
        return any(pat.lower() in url_lower for pat in Config.INSPIRE_API_PATTERNS)

    async def _handle_response(
        self, response: Response, discover: bool, collected_at: str
    ) -> None:
        """路由拦截回调：解析并记录命中的 API 响应。"""
        url = response.url
        if not self._should_intercept(url):
            return
        if response.status not in (200, 201):
            return
        # 只处理 JSON 响应
        content_type = response.headers.get("content-type", "")
        if "json" not in content_type:
            return

        try:
            body = await response.json()
        except Exception:
            try:
                text = await response.text()
                body = json.loads(text)
            except Exception:
                return

        if discover:
            entry = {
                "url": url,
                "status": response.status,
                "timestamp": datetime.now().isoformat(),
                "body": body,
            }
            self._discover_log.append(entry)
            logger.info(f"[DISCOVER] 拦截到 API: {url}")

        # ── 精确解析：select/topic/detail（Phase 1 确认接口）
        if "select/topic/detail" in url:
            topics = _parse_select_topic_detail(body, collected_at)
            if topics:
                logger.info(f"  → [精确解析] {len(topics)} 个话题 (来源: {url})")
                self._collected.extend(topics)
                return

        # ── 启发式解析（兜底，用于接口变更时的降级处理）
        category = _guess_category_from_url(url)
        topics = _try_parse_topic_list(body, category, collected_at)
        if topics:
            logger.info(f"  → [启发式] 解析到 {len(topics)} 个话题 (来源: {url})")
            self._collected.extend(topics)

    # ── 导航与交互 ────────────────────────────────────────────

    async def _navigate_to_inspire(self, page: Page) -> bool:
        """
        导航到创作者中心，尝试找到「创作灵感」或「热点」入口并点击。
        返回 True 表示成功找到并点击了入口。
        """
        logger.info(f"导航到创作者中心: {Config.CREATOR_CENTER_URL}")
        try:
            await page.goto(
                Config.CREATOR_CENTER_URL,
                wait_until="domcontentloaded",
                timeout=30000,
            )
        except Exception as e:
            logger.error(f"页面加载失败: {e}")
            return False

        await self._jitter(2.0, 4.0)

        current_url = page.url
        logger.info(f"当前页面 URL: {current_url}")

        # 检查是否需要登录
        if await _is_login_required(page):
            logger.error(
                f"检测到登录页面（当前 URL: {current_url}），session 已失效！\n"
                "请运行 python login.py 重新扫码登录。"
            )
            return False

        # 截图（调试用）
        screenshots_dir = Path(self.output_dir) / "screenshots"
        screenshots_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        await page.screenshot(path=str(screenshots_dir / f"creator_center_{ts}.png"))

        # 尝试找到「创作灵感」或「热点」导航入口
        inspire_selectors = [
            "text=创作灵感",
            "text=灵感",
            "text=热点",
            "[href*='inspire']",
            "[href*='inspiration']",
            "[href*='hot']",
            "a:has-text('灵感')",
            "a:has-text('创作')",
            ".nav-item:has-text('灵感')",
            "[class*='inspire']",
            "[class*='inspiration']",
        ]

        for sel in inspire_selectors:
            try:
                el = await page.wait_for_selector(sel, timeout=3000)
                if el:
                    logger.info(f"找到入口元素: {sel}")
                    await el.click()
                    await self._jitter(2.0, 3.5)
                    await page.screenshot(
                        path=str(screenshots_dir / f"after_click_{ts}.png")
                    )
                    return True
            except Exception:
                continue

        logger.warning(
            "未找到「创作灵感」导航入口，将在当前页面等待 API 响应。\n"
            "建议使用 --no-headless 模式手动确认页面结构。"
        )
        return True  # 继续在当前页面等待拦截

    async def _iterate_category_tabs(self, page: Page, collected_at: str) -> None:
        """
        遍历分区 Tab（美食/穿搭/美妆/…），对每个 Tab 等待 API 响应。
        Phase 1 发现实际 Tab 结构后，可在此处补充精确选择器。
        """
        # 通用的 Tab 选择器模板（Phase 1 后根据实际 DOM 精化）
        tab_selectors = [
            "[class*='tab-item']",
            "[class*='category-tab']",
            "[class*='nav-tab']",
            "[role='tab']",
            ".tab",
        ]

        tabs_found = []
        for sel in tab_selectors:
            try:
                tabs = await page.query_selector_all(sel)
                if tabs:
                    tabs_found = tabs
                    logger.info(f"找到 {len(tabs)} 个 Tab 元素 (selector: {sel})")
                    break
            except Exception:
                continue

        if not tabs_found:
            logger.warning("未找到分区 Tab，将只采集当前可见的话题列表。")
            await asyncio.sleep(Config.API_WAIT_TIMEOUT / 3)
            return

        for i, tab in enumerate(tabs_found):
            try:
                tab_text = await tab.inner_text()
                tab_text = (tab_text or "").strip()
                logger.info(f"  → 点击 Tab [{i+1}/{len(tabs_found)}]: {tab_text or '(无文本)'}")
                await tab.click()
                await self._jitter()
                # 等待 API 响应到达（在 _handle_response 中处理）
                await asyncio.sleep(2.0)
            except Exception as e:
                logger.debug(f"Tab 点击失败（{e}），跳过")
                continue

    # ── DOM 兜底解析 ──────────────────────────────────────────

    async def _dom_fallback(self, page: Page, collected_at: str) -> List[Dict]:
        """
        当 API 拦截未采集到足够话题时，尝试直接解析 DOM。
        选择器为通用模板，Phase 1 确认实际结构后精化。
        """
        results: List[Dict] = []

        # 常见的话题卡片容器选择器
        card_selectors = [
            "[class*='hot-topic']",
            "[class*='topic-card']",
            "[class*='topic-item']",
            "[class*='inspire-item']",
            "[class*='hot-item']",
            "[class*='trend-item']",
        ]

        for sel in card_selectors:
            try:
                cards = await page.query_selector_all(sel)
                if not cards:
                    continue
                logger.info(f"[DOM兜底] 找到 {len(cards)} 个卡片 ({sel})")
                for idx, card in enumerate(cards):
                    text = (await card.inner_text()).strip()
                    if not text or len(text) < 2:
                        continue
                    # 简单拆分：第一行为话题名，后续行可能包含观看数
                    lines = [l.strip() for l in text.splitlines() if l.strip()]
                    if not lines:
                        continue
                    topic_name = lines[0]
                    view_count = ""
                    for line in lines[1:]:
                        if "人" in line or "万" in line or re.search(r"\d", line):
                            view_count = line
                            break
                    results.append(
                        _make_topic(idx + 1, topic_name, view_count, "DOM兜底", collected_at)
                    )
                if results:
                    return results
            except Exception as e:
                logger.debug(f"DOM兜底选择器 {sel} 失败: {e}")
                continue

        return results

    # ── 主入口 ────────────────────────────────────────────────

    async def scrape(self, discover: bool = False) -> List[Dict]:
        """
        执行采集。

        Args:
            discover: True 时记录所有命中 INSPIRE_API_PATTERNS 的 API 响应到文件（Phase 1 用）。

        Returns:
            话题列表，每项为 {rank, topic, view_count, category, collected_at}。
        """
        collected_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._collected = []
        self._discover_log = []

        # 检查 profile 目录
        if not Path(self.profile_dir).exists():
            logger.error(
                f"Profile 目录不存在: {self.profile_dir}\n"
                "请先运行 python login.py 完成登录。"
            )
            return []

        async with async_playwright() as p:
            try:
                context = await p.chromium.launch_persistent_context(
                    user_data_dir=self.profile_dir,
                    headless=self.headless,
                    user_agent=(
                        Config.MOBILE_UA if Config.USE_MOBILE_UA else Config.DESKTOP_UA
                    ),
                    viewport={"width": 390, "height": 844} if Config.USE_MOBILE_UA
                    else {"width": 1440, "height": 900},
                    args=["--disable-blink-features=AutomationControlled"],
                )
            except Exception as e:
                logger.error(f"启动浏览器失败: {e}")
                return []

            # ── 加载登录 cookies（解决 Playwright 不自动持久化 session cookie 的问题）
            state_path = Path(self.profile_dir) / "storage_state.json"
            if state_path.exists():
                try:
                    with open(state_path, "r", encoding="utf-8") as f:
                        saved_state = json.load(f)
                    cookies = saved_state.get("cookies", [])
                    if cookies:
                        await context.add_cookies(cookies)
                        logger.info(f"已从 storage_state.json 加载 {len(cookies)} 个 cookie")
                except Exception as e:
                    logger.warning(f"加载 storage_state.json 失败: {e}")
            else:
                logger.warning(
                    f"未找到 {state_path}，请先运行 python login.py 完成登录。"
                )

            page = context.pages[0] if context.pages else await context.new_page()

            # 注册全局响应拦截
            async def _on_response(response: Response) -> None:
                await self._handle_response(response, discover, collected_at)

            page.on("response", _on_response)

            try:
                # 导航到创作者中心
                nav_ok = await self._navigate_to_inspire(page)
                if not nav_ok:
                    return []

                # 等待初始 API 响应
                await asyncio.sleep(3.0)

                # 遍历分区 Tab
                await self._iterate_category_tabs(page, collected_at)

                # 等待最后一批响应
                await asyncio.sleep(2.0)

                # 如果拦截结果不够，尝试 DOM 兜底
                if len(self._collected) < 10:
                    logger.info("API 拦截结果不足 10 条，尝试 DOM 兜底解析…")
                    dom_results = await self._dom_fallback(page, collected_at)
                    if dom_results:
                        logger.info(f"DOM 兜底解析到 {len(dom_results)} 个话题")
                        self._collected.extend(dom_results)

            finally:
                await context.close()

        # 去重 + 截断
        topics = _deduplicate(self._collected)
        topics = topics[: self.topics_limit]

        # 重新编号
        for i, t in enumerate(topics, 1):
            t["rank"] = i

        # 保存发现日志
        if discover and self._discover_log:
            ts = datetime.now()
            log_path = _write_discover_log(self._discover_log, self.output_dir, ts)
            logger.info(f"[DISCOVER] API 响应记录已保存: {log_path}")
            logger.info(
                f"[DISCOVER] 共记录 {len(self._discover_log)} 条 API 响应，"
                f"解析到 {len(topics)} 个话题。"
            )
            if not topics:
                logger.info(
                    "\n提示：如果话题数为 0，说明自动解析未匹配到正确的接口结构。\n"
                    f"请查看 {log_path} 中的 body 字段，\n"
                    "找到包含热点话题列表的那条响应，\n"
                    "然后根据实际 JSON 结构更新 scraper.py 的 _try_parse_topic_list()。"
                )

        logger.info(f"采集完成：共 {len(topics)} 个话题")
        return topics


# ─────────────────────────────────────────────────────────────
# 工具函数
# ─────────────────────────────────────────────────────────────

def _guess_category_from_url(url: str) -> str:
    """从 URL 路径猜测话题分类名（粗略）。"""
    known = {
        "beauty": "美妆护肤",
        "fashion": "穿搭",
        "food": "美食",
        "fitness": "健康健身",
        "travel": "旅行",
        "baby": "育儿",
        "game": "游戏",
        "digital": "数码",
        "home": "家居",
        "pet": "宠物",
    }
    url_lower = url.lower()
    for key, name in known.items():
        if key in url_lower:
            return name
    return "综合"


async def _is_login_required(page: Page) -> bool:
    """检测页面是否要求登录（出现登录弹窗或被重定向到登录页）。"""
    url = page.url
    if "login" in url.lower() or "signin" in url.lower():
        return True
    login_indicators = [
        "text=请登录",
        "text=立即登录",
        "[class*='login-modal']",
        "[class*='sign-in']",
    ]
    for sel in login_indicators:
        try:
            el = await page.query_selector(sel)
            if el:
                return True
        except Exception:
            pass
    return False


def _deduplicate(topics: List[Dict]) -> List[Dict]:
    """按话题名去重，保留首次出现的条目。"""
    seen: set[str] = set()
    result: List[Dict] = []
    for t in topics:
        name = t.get("topic", "").strip()
        if name and name not in seen:
            seen.add(name)
            result.append(t)
    return result


# ─────────────────────────────────────────────────────────────
# 解析模式（仅解析已保存的发现日志，不启动浏览器）
# ─────────────────────────────────────────────────────────────

def parse_discover_log(jsonl_path: str) -> List[Dict]:
    """
    离线解析发现模式记录的 API 响应文件，输出话题列表。
    用于 Phase 1 验收和调试。
    优先使用精确解析（select/topic/detail），兜底用启发式解析。
    """
    results: List[Dict] = []
    collected_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(jsonl_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                url = entry.get("url", "")
                body = entry.get("body", {})

                # 精确解析：select/topic/detail
                if "select/topic/detail" in url:
                    topics = _parse_select_topic_detail(body, collected_at)
                    if topics:
                        print(f"\n✓ [精确] 从 {url} 解析到 {len(topics)} 个话题：")
                        for t in topics[:5]:
                            print(f"  [{t['rank']}] {t['topic']}  {t['view_count']}  {t['category']}")
                        if len(topics) > 5:
                            print(f"  ... 共 {len(topics)} 个")
                        results.extend(topics)
                        continue

                # 启发式兜底解析
                category = _guess_category_from_url(url)
                topics = _try_parse_topic_list(body, category, collected_at)
                if topics:
                    print(f"\n✓ 从 {url} 解析到 {len(topics)} 个话题：")
                    for t in topics[:5]:
                        print(f"  [{t['rank']}] {t['topic']}  {t['view_count']}")
                    if len(topics) > 5:
                        print(f"  ... 共 {len(topics)} 个")
                    results.extend(topics)
                else:
                    print(f"  ✗ 无法从 {url} 解析话题（结构不匹配）")
            except Exception as e:
                logger.debug(f"解析行失败: {e}")
    return _deduplicate(results)


# ─────────────────────────────────────────────────────────────
# CLI 入口
# ─────────────────────────────────────────────────────────────

def main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(
        description="小红书创作者中心热点话题采集器",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--discover",
        action="store_true",
        help="接口发现模式：记录所有命中的 API 响应到 output/discover/（Phase 1 用）",
    )
    parser.add_argument(
        "--no-headless",
        action="store_true",
        help="显示浏览器界面（调试用）",
    )
    parser.add_argument(
        "--output-dir",
        default=Config.OUTPUT_DIR,
        help="输出目录",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=Config.TOPICS_PER_RUN,
        help="最多采集话题数",
    )
    parser.add_argument(
        "--parse-only",
        metavar="JSONL_PATH",
        help="仅解析已保存的发现日志文件，不启动浏览器",
    )
    args = parser.parse_args(argv)

    # 设置日志级别
    logging.basicConfig(
        level="DEBUG" if args.discover else "INFO",
        format="[%(asctime)s] [%(levelname)s] %(name)s: %(message)s",
    )

    # 离线解析模式
    if args.parse_only:
        if not os.path.exists(args.parse_only):
            print(f"文件不存在: {args.parse_only}")
            sys.exit(1)
        topics = parse_discover_log(args.parse_only)
        print(f"\n共解析到 {len(topics)} 个去重话题。")
        sys.exit(0)

    # 在线采集
    headless = not args.no_headless
    scraper = CreatorHotScraper(
        headless=headless,
        output_dir=args.output_dir,
        topics_limit=args.limit,
    )

    topics = asyncio.run(scraper.scrape(discover=args.discover))

    if not topics:
        logger.warning(
            "本次采集结果为空。\n"
            "如果是首次运行，请先执行 python login.py 完成登录，\n"
            "然后运行 python scraper.py --discover --no-headless 查找正确的接口。"
        )
        sys.exit(1)

    # 写 TSV
    ts = datetime.now()
    out_path = _write_tsv(topics, args.output_dir, ts)
    logger.info(f"结果已保存: {out_path} ({len(topics)} 条话题)")

    # 打印前 10 条
    print(f"\n{'─'*60}")
    print(f"  小红书创作灵感热点 Top {min(10, len(topics))}（{ts:%Y-%m-%d %H:%M}）")
    print(f"{'─'*60}")
    for t in topics[:10]:
        print(f"  [{t['rank']:3d}] {t['topic']:<30s}  {t['view_count']:<10s}  {t['category']}")
    if len(topics) > 10:
        print(f"  ... 共 {len(topics)} 条，详见 {out_path}")
    print(f"{'─'*60}\n")


import logging

if __name__ == "__main__":
    main()
