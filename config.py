"""
config.py — scheduled_scraping 独立配置

所有可调参数统一在此管理，支持 .env / .env.local 文件覆盖。
不依赖父项目任何文件，可独立运行。
"""
import os
from pathlib import Path

try:
    from dotenv import load_dotenv
    _HERE = Path(__file__).resolve().parent
    for _name in (".env.local", ".env"):
        _p = _HERE / _name
        if _p.exists():
            load_dotenv(str(_p), override=True)
            break
except ImportError:
    pass

_HERE = Path(__file__).resolve().parent


def _abs(path: str) -> str:
    """相对路径基于本文件所在目录，绝对路径原样返回。"""
    p = Path(path)
    return str(p) if p.is_absolute() else str(_HERE / path)


class Config:
    PROJECT_ROOT = str(_HERE)

    # ── 浏览器 Profile（登录态持久化）────────────────────────────
    # 首次使用必须先运行: python login.py 完成扫码登录
    # 提示：如果要复用父项目已登录的 Profile，可设置环境变量：
    #   BROWSER_PROFILE_DIR=../browser_profile/xhs
    BROWSER_PROFILE_DIR: str = _abs(
        os.getenv("BROWSER_PROFILE_DIR", "browser_profile/xhs")
    )

    # ── 创作者中心 URL ────────────────────────────────────────────
    CREATOR_CENTER_URL: str = os.getenv(
        "CREATOR_CENTER_URL", "https://creator.xiaohongshu.com"
    )

    # ── 采集参数 ──────────────────────────────────────────────────
    # 每次采集目标话题数（实际数量取决于平台返回）
    TOPICS_PER_RUN: int = int(os.getenv("TOPICS_PER_RUN", "100"))

    # ── 接口发现配置（Phase 1 已完成）────────────────────────────────
    # Phase 1 发现的精确接口：/api/galaxy/creator/select/topic/detail
    # 响应结构：data[].labelName（分区）+ data[].selectTopics[].title（话题名）
    #           data[].selectTopics[].viewNum（观看总次数，整数）
    # 其他保留关键词作为兜底，避免接口变更时完全失效
    INSPIRE_API_PATTERNS: list[str] = [
        "select/topic/detail",   # Phase 1 确认的精确接口（优先匹配）
        "inspire",
        "hot_topic",
        "topic_list",
        "trend",
        "hotspot",
    ]

    # ── 浏览器配置 ────────────────────────────────────────────────
    # 是否无头运行（生产用 true；调试用 false 可看到浏览器界面）
    HEADLESS: bool = os.getenv("HEADLESS", "true").lower() not in ("0", "false", "no")

    # 是否使用移动端 UA（Phase 4 实验：桌面端分区少，移动端可能更多）
    USE_MOBILE_UA: bool = os.getenv("USE_MOBILE_UA", "false").lower() not in (
        "0", "false", "no"
    )

    # 是否点入话题详情页（高风险，默认关闭）
    FETCH_DETAIL: bool = os.getenv("FETCH_DETAIL", "false").lower() not in (
        "0", "false", "no"
    )

    # ── 限速参数（不得随意调小）──────────────────────────────────
    # 页面操作间隔（秒）— 模拟真实浏览节奏
    PAGE_ACTION_DELAY_MIN: float = float(os.getenv("PAGE_ACTION_DELAY_MIN", "1.5"))
    PAGE_ACTION_DELAY_MAX: float = float(os.getenv("PAGE_ACTION_DELAY_MAX", "3.5"))

    # 等待 API 响应的超时时间（秒）
    API_WAIT_TIMEOUT: float = float(os.getenv("API_WAIT_TIMEOUT", "30.0"))

    # 详情页操作间隔（仅 FETCH_DETAIL=true 时生效）
    DETAIL_DELAY_MIN: float = float(os.getenv("DETAIL_DELAY_MIN", "5.0"))
    DETAIL_DELAY_MAX: float = float(os.getenv("DETAIL_DELAY_MAX", "10.0"))
    DETAIL_MAX_PAGES: int = int(os.getenv("DETAIL_MAX_PAGES", "20"))

    # ── 定时任务配置 ──────────────────────────────────────────────
    SCHEDULE_HOUR: int = int(os.getenv("SCHEDULE_HOUR", "10"))
    SCHEDULE_MINUTE: int = int(os.getenv("SCHEDULE_MINUTE", "40"))

    # ── 输出目录 ──────────────────────────────────────────────────
    OUTPUT_DIR: str = _abs(os.getenv("OUTPUT_DIR", "output"))

    # ── User-Agent 配置 ───────────────────────────────────────────
    DESKTOP_UA: str = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    )
    MOBILE_UA: str = (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1"
    )
