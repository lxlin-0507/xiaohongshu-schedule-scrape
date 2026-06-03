"""
logger.py — 统一日志封装（来自父项目 logger.py，独立副本）
"""
import logging
import os
from pathlib import Path


def _level() -> int:
    name = os.getenv("LOG_LEVEL", "INFO").upper()
    return getattr(logging, name, logging.INFO)


def get_logger(name: str) -> logging.Logger:
    lg = logging.getLogger(name)
    if lg.handlers:
        return lg
    lg.setLevel(_level())
    h = logging.StreamHandler()
    h.setFormatter(
        logging.Formatter("[%(asctime)s] [%(levelname)s] %(name)s: %(message)s")
    )
    lg.addHandler(h)
    lg.propagate = False
    return lg


def add_file_handler(logger: logging.Logger, log_dir: str, date_str: str) -> None:
    """为 logger 追加一个滚动文件 handler，日志写到 log_dir/{date_str}.log。"""
    log_path = Path(log_dir) / f"{date_str}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    fh = logging.FileHandler(str(log_path), encoding="utf-8")
    fh.setFormatter(
        logging.Formatter("[%(asctime)s] [%(levelname)s] %(name)s: %(message)s")
    )
    logger.addHandler(fh)
