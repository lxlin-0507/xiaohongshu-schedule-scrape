"""
scheduler.py — 定时任务入口（每天 09:00 自动采集）

用法：
    # 启动定时守护进程（前台运行，Ctrl+C 停止）
    python scheduler.py

    # 立即运行一次（跳过定时，用于测试）
    python scheduler.py --run-now

    # 指定运行时间（如 每天 08:30）
    python scheduler.py --hour 8 --minute 30

    # 后台运行（nohup）
    nohup python scheduler.py > output/logs/scheduler.log 2>&1 &

配置：
    修改 config.py 中的 SCHEDULE_HOUR / SCHEDULE_MINUTE 调整定时。
    或通过环境变量: SCHEDULE_HOUR=8 SCHEDULE_MINUTE=30 python scheduler.py
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

from config import Config
from logger import get_logger, add_file_handler
from login import check_session
from scraper import CreatorHotScraper, _write_tsv

logger = get_logger("scheduler")

try:
    from apscheduler.schedulers.blocking import BlockingScheduler
    from apscheduler.triggers.cron import CronTrigger
    _HAS_APSCHEDULER = True
except ImportError:
    _HAS_APSCHEDULER = False
    logger.warning("APScheduler 未安装，将回退到简单循环模式。建议: pip install apscheduler>=3.10")


# ─────────────────────────────────────────────────────────────
# 采集任务
# ─────────────────────────────────────────────────────────────

def run_scraping_task() -> None:
    """
    单次采集任务（由调度器调用，也可直接调用做手动测试）。
    """
    now = datetime.now()
    date_str = now.strftime("%Y%m%d")

    # 为当天的日志添加文件 handler
    logs_dir = str(Path(Config.OUTPUT_DIR) / "logs")
    add_file_handler(logger, logs_dir, date_str)

    logger.info(f"{'='*50}")
    logger.info(f"定时采集任务启动 — {now:%Y-%m-%d %H:%M:%S}")
    logger.info(f"{'='*50}")

    # ── 检查 session 有效性（访问创作者中心，看是否跳转到 /login）
    logger.info("检查登录 session 有效性…")
    if not check_session():
        msg = (
            "Session 已失效，跳过本次采集。\n"
            "请手动运行 python login.py 重新扫码登录后，session 将自动恢复。"
        )
        logger.warning(msg)
        _write_flag(f"session_expired_{date_str}.flag", now)
        return
    logger.info("Session 有效，开始采集。")

    # 检查 profile 目录
    if not Path(Config.BROWSER_PROFILE_DIR).exists():
        logger.error(
            f"Profile 目录不存在: {Config.BROWSER_PROFILE_DIR}\n"
            "请先运行 python login.py 完成登录，再启动定时任务。"
        )
        return

    try:
        scraper = CreatorHotScraper(
            headless=True,
            output_dir=Config.OUTPUT_DIR,
            topics_limit=Config.TOPICS_PER_RUN,
        )
        topics = asyncio.run(scraper.scrape())
    except Exception as e:
        logger.exception(f"采集任务异常: {e}")
        _alert_on_failure(str(e), now)
        return

    if not topics:
        msg = "本次采集结果为空（0 条话题）。可能原因：session 失效 / 接口变更。"
        logger.warning(msg)
        _alert_on_failure(msg, now)
        return

    out_path = _write_tsv(topics, Config.OUTPUT_DIR, now)
    logger.info(f"采集完成：{len(topics)} 条话题 → {out_path}")
    logger.info(f"{'='*50}\n")


def _alert_on_failure(reason: str, ts: datetime) -> None:
    """
    失败告警（当前实现：写入 output/alerts/ 目录）。
    Phase 3 可扩展为邮件 / 企微 / Bark 通知。
    """
    alert_dir = Path(Config.OUTPUT_DIR) / "alerts"
    alert_dir.mkdir(parents=True, exist_ok=True)
    alert_file = alert_dir / f"alert_{ts:%Y%m%d_%H%M%S}.txt"
    alert_file.write_text(
        f"时间: {ts:%Y-%m-%d %H:%M:%S}\n原因: {reason}\n",
        encoding="utf-8",
    )
    logger.warning(f"告警已写入: {alert_file}")


def _write_flag(filename: str, ts: datetime) -> None:
    """写入 flag 文件到 output/flags/，便于外部监控脚本检测异常。"""
    flag_dir = Path(Config.OUTPUT_DIR) / "flags"
    flag_dir.mkdir(parents=True, exist_ok=True)
    flag_file = flag_dir / filename
    flag_file.write_text(
        f"时间: {ts:%Y-%m-%d %H:%M:%S}\n",
        encoding="utf-8",
    )
    logger.info(f"Flag 文件已写入: {flag_file}")


# ─────────────────────────────────────────────────────────────
# 调度器
# ─────────────────────────────────────────────────────────────

def _run_with_apscheduler(hour: int, minute: int) -> None:
    scheduler = BlockingScheduler(timezone="Asia/Shanghai")
    scheduler.add_job(
        run_scraping_task,
        trigger=CronTrigger(hour=hour, minute=minute),
        id="xhs_creator_hot",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=600,  # 10 分钟内可补跑
    )
    next_run = scheduler.get_jobs()[0].next_run_time if scheduler.get_jobs() else None
    logger.info(f"定时任务已注册：每天 {hour:02d}:{minute:02d}（Asia/Shanghai）")
    if next_run:
        logger.info(f"下次运行时间: {next_run:%Y-%m-%d %H:%M:%S}")
    logger.info("按 Ctrl+C 停止调度器。")
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("调度器已停止。")


def _run_simple_loop(hour: int, minute: int) -> None:
    """APScheduler 不可用时的简单循环方案（每分钟检查一次）。"""
    import time as _time

    logger.info(f"简单循环模式：每天 {hour:02d}:{minute:02d} 运行采集（精度 ±1 分钟）")
    logger.info("按 Ctrl+C 停止。")

    last_run_date: str = ""
    try:
        while True:
            now = datetime.now()
            today = now.strftime("%Y%m%d")
            if (
                now.hour == hour
                and now.minute == minute
                and today != last_run_date
            ):
                last_run_date = today
                run_scraping_task()
            _time.sleep(30)
    except (KeyboardInterrupt, SystemExit):
        logger.info("调度器已停止。")


# ─────────────────────────────────────────────────────────────
# CLI 入口
# ─────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="小红书热点话题定时采集调度器",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--run-now",
        action="store_true",
        help="立即运行一次采集任务（用于测试）",
    )
    parser.add_argument(
        "--hour",
        type=int,
        default=Config.SCHEDULE_HOUR,
        help="定时运行的小时（24h 制）",
    )
    parser.add_argument(
        "--minute",
        type=int,
        default=Config.SCHEDULE_MINUTE,
        help="定时运行的分钟",
    )
    args = parser.parse_args()

    # 设置基础日志
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] [%(levelname)s] %(name)s: %(message)s",
    )

    # 确保输出目录存在
    Path(Config.OUTPUT_DIR).mkdir(parents=True, exist_ok=True)

    if args.run_now:
        logger.info("手动触发立即运行…")
        run_scraping_task()
        return

    if _HAS_APSCHEDULER:
        _run_with_apscheduler(args.hour, args.minute)
    else:
        _run_simple_loop(args.hour, args.minute)


if __name__ == "__main__":
    main()
