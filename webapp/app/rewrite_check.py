"""静默改写核查：抓取一次完整列表 -> 与 SQLite 基线比对 -> 完整时更新基线。"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .baseline import BaselineResult, TextChange, process_baseline
from .crawler import CrawlReport, crawl


@dataclass
class RewriteCheckResult:
    entry_url: str
    report: CrawlReport
    baseline: BaselineResult

    @property
    def complete(self) -> bool:
        return self.report.complete

    @property
    def crawl_report(self) -> CrawlReport:
        return self.report

    @property
    def changes(self) -> list[TextChange]:
        return self.baseline.changes

    @property
    def first_baseline(self) -> bool:
        return self.baseline.first_baseline

    @property
    def baseline_existed(self) -> bool:
        return self.baseline.baseline_existed

    @property
    def incomplete_reasons(self) -> list[str]:
        return self.report.incomplete_reasons


def check_silent_rewrites(
    entry_url: str,
    db_path: str | Path | None = None,
) -> RewriteCheckResult:
    """执行一次静默改写核查。

    分页、详情读取/解析失败或触达抓取上限时，报告为不完整，并且不会读写覆盖基线。
    """
    report = crawl(entry_url)
    baseline_result = process_baseline(
        entry_url,
        report.announcements,
        complete=report.complete,
        db_path=db_path,
    )
    return RewriteCheckResult(entry_url=entry_url, report=report, baseline=baseline_result)


# 常用别名
check_rewrites = check_silent_rewrites
run_check = check_silent_rewrites
