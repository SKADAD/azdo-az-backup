from __future__ import annotations

import logging
import os
import re
import sys
import time
from functools import wraps
from pathlib import Path
from typing import Callable, TypeVar

T = TypeVar("T")

_ROOT_LOGGER_NAME = "azdo_backup"
_LOG_INITIALIZED = False


def get_logger(name: str = _ROOT_LOGGER_NAME) -> logging.Logger:
    """Return a logger under the package root logger.

    The handler/level live on the root package logger; module loggers
    (``azdo_backup.client`` etc.) propagate to it so every module logs.
    """
    global _LOG_INITIALIZED
    if not _LOG_INITIALIZED:
        root = logging.getLogger(_ROOT_LOGGER_NAME)
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-5s %(name)s: %(message)s")
        )
        root.addHandler(handler)
        root.setLevel(os.environ.get("AZDO_BACKUP_LOG", "INFO").upper())
        root.propagate = False
        _LOG_INITIALIZED = True
    if name != _ROOT_LOGGER_NAME and not name.startswith(_ROOT_LOGGER_NAME + "."):
        name = f"{_ROOT_LOGGER_NAME}.{name}"
    return logging.getLogger(name)


def ensure_dir(path: os.PathLike | str) -> Path:
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    return p


_INVALID_FS_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def safe_filename(name: str, max_len: int = 120) -> str:
    cleaned = _INVALID_FS_CHARS.sub("_", name).strip().rstrip(".")
    if not cleaned:
        cleaned = "_"
    return cleaned[:max_len]


def retry(
    tries: int = 5,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    exceptions: tuple[type[BaseException], ...] = (Exception,),
) -> Callable[[Callable[..., T]], Callable[..., T]]:
    log = get_logger()

    def deco(fn: Callable[..., T]) -> Callable[..., T]:
        @wraps(fn)
        def wrapper(*args, **kwargs) -> T:
            attempt = 0
            while True:
                try:
                    return fn(*args, **kwargs)
                except exceptions as exc:
                    attempt += 1
                    if attempt >= tries:
                        raise
                    delay = min(max_delay, base_delay * (2 ** (attempt - 1)))
                    log.warning(
                        "%s failed (attempt %d/%d): %s — retrying in %.1fs",
                        fn.__name__, attempt, tries, exc, delay,
                    )
                    time.sleep(delay)
        return wrapper
    return deco


def chunks(seq, size: int):
    buf = []
    for item in seq:
        buf.append(item)
        if len(buf) >= size:
            yield buf
            buf = []
    if buf:
        yield buf
