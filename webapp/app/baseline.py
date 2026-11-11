"""SQLite 基线：按列表入口隔离，以详情 URL 识别公告。

只有在一次完整采集完成后才写入/替换基线；不完整采集既不比较，也不改动已有基线。
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urldefrag

from .content import comparison_key
from .models import Announcement

DB_PATH_ENV = "SNAPSHOT_DB_PATH"
DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "snapshots.sqlite3"


def get_db_path(db_path: str | Path | None = None) -> str:
    """数据库路径优先级：显式参数 > 环境变量 > 默认本地路径。"""
    if db_path is not None:
        return str(db_path)
    return os.environ.get(DB_PATH_ENV, str(DEFAULT_DB_PATH))


def scope_key(entry_url: str) -> str:
    """列表入口的隔离键：同一入口地址共用一份基线，不同入口互不干扰。"""
    return urldefrag(entry_url.strip())[0]


def _connect(db_path: str | Path | None = None) -> sqlite3.Connection:
    path = get_db_path(db_path)
    if path != ":memory:":
        Path(path).expanduser().parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


@contextmanager
def _connection(db_path: str | Path | None = None):
    """打开连接并在退出时提交/关闭，避免请求间泄漏文件句柄。"""
    conn = _connect(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(db_path: str | Path | None = None) -> None:
    """创建基线表；重复调用安全。"""
    with _connection(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS baseline_runs (
                entry_url TEXT PRIMARY KEY,
                captured_at TEXT NOT NULL,
                announcement_count INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS baseline_announcements (
                entry_url TEXT NOT NULL,
                detail_url TEXT NOT NULL,
                title TEXT NOT NULL DEFAULT '',
                content_text TEXT NOT NULL DEFAULT '',
                captured_at TEXT NOT NULL,
                PRIMARY KEY (entry_url, detail_url)
            );

            CREATE INDEX IF NOT EXISTS idx_baseline_entry
                ON baseline_announcements (entry_url);
            """
        )


@dataclass
class BaselineEntry:
    detail_url: str
    title: str
    content_text: str
    captured_at: str

    @property
    def compare_key(self) -> str:
        return comparison_key(self.content_text)


@dataclass
class TextChange:
    detail_url: str
    title: str
    before_text: str
    after_text: str
    captured_before: str = ""
    captured_after: str = ""

    @property
    def url(self) -> str:
        return self.detail_url

    @property
    def before(self) -> str:
        return self.before_text

    @property
    def after(self) -> str:
        return self.after_text


@dataclass
class BaselineResult:
    entry_url: str
    complete: bool
    baseline_existed: bool = False
    first_baseline: bool = False
    compared_count: int = 0
    added_count: int = 0
    removed_count: int = 0
    changes: list[TextChange] = field(default_factory=list)
    baseline_captured_at: str = ""


def load_baseline(entry_url: str, db_path: str | Path | None = None) -> dict[str, BaselineEntry] | None:
    """读取某入口的完整基线；从未建立时返回 None。"""
    init_db(db_path)
    scope = scope_key(entry_url)
    with _connection(db_path) as conn:
        run = conn.execute(
            "SELECT captured_at FROM baseline_runs WHERE entry_url = ?", (scope,)
        ).fetchone()
        if run is None:
            return None
        rows = conn.execute(
            "SELECT detail_url, title, content_text, captured_at "
            "FROM baseline_announcements WHERE entry_url = ?",
            (scope,),
        ).fetchall()
    return {
        row["detail_url"]: BaselineEntry(
            detail_url=row["detail_url"],
            title=row["title"],
            content_text=row["content_text"],
            captured_at=row["captured_at"],
        )
        for row in rows
    }


def baseline_captured_at(entry_url: str, db_path: str | Path | None = None) -> str:
    init_db(db_path)
    with _connection(db_path) as conn:
        row = conn.execute(
            "SELECT captured_at FROM baseline_runs WHERE entry_url = ?", (scope_key(entry_url),)
        ).fetchone()
    return row["captured_at"] if row else ""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def replace_baseline(
    entry_url: str,
    announcements: list[Announcement],
    db_path: str | Path | None = None,
) -> str:
    """用一次完整采集的结果替换入口基线，返回采集时间。"""
    init_db(db_path)
    scope = scope_key(entry_url)
    captured_at = _now()
    unique: dict[str, Announcement] = {}
    for ann in announcements:
        unique[ann.url] = ann

    with _connection(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM baseline_announcements WHERE entry_url = ?", (scope,))
        conn.executemany(
            "INSERT INTO baseline_announcements "
            "(entry_url, detail_url, title, content_text, captured_at) VALUES (?, ?, ?, ?, ?)",
            [
                (scope, ann.url, ann.title, ann.content_text, captured_at)
                for ann in unique.values()
            ],
        )
        conn.execute(
            "INSERT INTO baseline_runs (entry_url, captured_at, announcement_count) "
            "VALUES (?, ?, ?) ON CONFLICT(entry_url) DO UPDATE SET "
            "captured_at = excluded.captured_at, announcement_count = excluded.announcement_count",
            (scope, captured_at, len(unique)),
        )
        conn.commit()
    return captured_at


def process_baseline(
    entry_url: str,
    announcements: list[Announcement],
    complete: bool,
    db_path: str | Path | None = None,
) -> BaselineResult:
    """比较本次完整采集与上次完整基线，并在完整时更新基线。

    不完整时不比较、不写入任何行；调用方据此避免报告“未变化”。
    """
    scope = scope_key(entry_url)
    baseline = load_baseline(entry_url, db_path)
    result = BaselineResult(
        entry_url=scope,
        complete=complete,
        baseline_existed=baseline is not None,
    )

    if not complete:
        # 保留原基线，不产生任何“未变化”结论。
        result.baseline_captured_at = baseline_captured_at(entry_url, db_path)
        return result

    current: dict[str, Announcement] = {ann.url: ann for ann in announcements}
    captured_after = _now()

    if baseline is None:
        result.first_baseline = True
        result.added_count = len(current)
        result.baseline_captured_at = replace_baseline(entry_url, announcements, db_path)
        return result

    result.baseline_captured_at = baseline_captured_at(entry_url, db_path)
    for detail_url, current_ann in current.items():
        old = baseline.get(detail_url)
        if old is None:
            result.added_count += 1
            continue
        result.compared_count += 1
        if old.compare_key != comparison_key(current_ann.content_text):
            result.changes.append(
                TextChange(
                    detail_url=detail_url,
                    title=current_ann.title or old.title,
                    before_text=old.content_text,
                    after_text=current_ann.content_text,
                    captured_before=old.captured_at,
                    captured_after=captured_after,
                )
            )
    result.removed_count = sum(1 for url in baseline if url not in current)

    # 完整采集之后，以本次结果整体替换基线（包括新增、消失的公告）。
    result.baseline_captured_at = replace_baseline(entry_url, announcements, db_path)
    return result


# 便于外部调用方按直觉命名
update_baseline = replace_baseline
compare_with_baseline = process_baseline
