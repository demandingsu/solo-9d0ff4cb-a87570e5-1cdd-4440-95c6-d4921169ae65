"""端到端测试：启动本地静态站点 -> 爬虫抓取 -> 归并核对。

运行方式：
    PYTHONPATH=/tmp/pylibs python3 -I tests/test_end_to_end.py
"""
from __future__ import annotations

import functools
import http.server
import sys
import tempfile
import threading
from pathlib import Path

# -I 隔离模式下 PYTHONPATH 不生效，显式注入依赖目录
for candidate in ("/tmp/pylibs", "/tmp/pyroot/lib/python3.11/site-packages"):
    if Path(candidate).exists() and candidate not in sys.path:
        sys.path.insert(0, candidate)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "webapp"))

from app.crawler import crawl  # noqa: E402
from app.merger import merge_announcements  # noqa: E402
from app.models import Announcement, ResultStatus  # noqa: E402
from app.parser import parse_detail  # noqa: E402
from app.content import comparison_key, extract_visible_content  # noqa: E402
from app.baseline import load_baseline, process_baseline  # noqa: E402

SITE_DIR = Path(__file__).resolve().parent.parent / "example-site" / "site"
EXPECTED = {
    "CG-2026-001": (ResultStatus.EFFECTIVE, "2026-11-06 10:00:00", 2),
    "CG-2026-002": (ResultStatus.EFFECTIVE, "2026-10-28 15:00:00", 2),
    "CG-2026-003": (ResultStatus.PENDING, None, 2),
    "CG-2026-004": (ResultStatus.PENDING, None, 3),
    "CG-2026-005": (ResultStatus.EFFECTIVE, "2026-11-02 09:30:00", 1),
    "CG-2026-006": (ResultStatus.UNKNOWN, None, 1),
}


def start_server() -> str:
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(SITE_DIR))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    return f"http://127.0.0.1:{port}/list/page_1.html"


def assert_equal(actual, expected, message: str) -> None:
    if actual != expected:
        raise AssertionError(f"{message}：期望 {expected!r}，实际 {actual!r}")
    print(f"  ✓ {message}")


def main() -> int:
    entry = start_server()
    print(f"[1] 本地示例站点：{entry}")

    print("[2] 从列表页沿下一页抓取并解析详情 ...")
    report = crawl(entry)
    assert_equal(len(report.visited_list_pages), 2, "遍历了 2 个列表页（下一页跟随成功）")
    assert_equal(len(report.announcements), 11, "解析出 11 条公告")
    assert_equal(report.errors, [], "抓取过程无错误")

    # 抽查一条详情页的字段标签解析
    first = next(a for a in report.announcements if a.project_no == "CG-2026-001" and a.notice_type == "更正公告")
    assert_equal(first.publish_time, "2026-10-15 16:20:00", "更正公告发布时间按标签正确解析")
    assert_equal(first.deadline, "2026-11-06 10:00:00", "更正公告新截止时间按标签正确解析")

    pending_notice = next(a for a in report.announcements if a.project_no == "CG-2026-003" and a.notice_type == "延期公告")
    assert_equal(pending_notice.deadline, None, "无新时间的延期页截止时间为 None")
    assert_equal(pending_notice.declares_extension, True, "延期页被识别为声明延期")

    print("[3] 按项目编号归并并按发布时间处理 ...")
    results = merge_announcements(report.announcements)
    assert_equal(len(results), 6, "归并出 6 个项目")
    by_no = {r.project_no: r for r in results}

    for project_no, (status, deadline, count) in EXPECTED.items():
        result = by_no[project_no]
        assert_equal(result.status, status, f"{project_no} 状态为 {status.value}")
        assert_equal(result.current_deadline, deadline,
                     f"{project_no} 当前截止时间为 {deadline or '空（不显示旧值）'}")
        assert_equal(len(result.announcements), count, f"{project_no} 保留 {count} 条公告链接")
        # 公告按发布时间升序
        times = [a.publish_time for a in result.announcements]
        assert_equal(times, sorted(times), f"{project_no} 公告按发布时间升序排列")

    print("[4] 解析器兜底能力（dl/dt/dd 与纯文本标签）...")
    alt_html = """
    <html><body><h1>某设备采购更正公告</h1>
      <dl><dt>项目编号：</dt><dd>ALT-999</dd>
          <dt>公告类型</dt><dd>更正公告</dd>
          <dt>发布时间：</dt><dd>2026年11月01日 10:00</dd></dl>
      <p>投标截止时间：2026年11月20日 09:30，特此通知。</p>
    </body></html>
    """
    ann = parse_detail(alt_html, "http://test/alt.html")
    assert_equal(ann.project_no, "ALT-999", "dl/dt/dd 结构解析项目编号")
    assert_equal(ann.notice_type, "更正公告", "dl/dt/dd 结构解析公告类型")
    assert_equal(ann.publish_time, "2026年11月01日 10:00", "dl/dt/dd 结构解析发布时间")
    assert_equal(ann.deadline, "2026年11月20日 09:30", "纯文本标签兜底解析截止时间")

    print("[5] 无项目编号的页面应被拒绝 ...")
    try:
        parse_detail("<html><body><p>没有字段标签的页面</p></body></html>", "http://test/x.html")
    except ValueError:
        print("  ✓ 无项目编号页面抛出 ValueError")
    else:
        raise AssertionError("无项目编号页面未被拒绝")

    print("[6] 静默改写：忽略排版空白、基线建立与不完整保护 ...")
    html_a = """
    <html><body>
      <nav>导航文字不参与比对</nav>
      <table class="fields">
        <tr><th>项目编号</th><td>RW-001</td></tr>
        <tr><th>发布时间</th><td>2026-10-01 09:00:00</td></tr>
      </table>
      <div class="content">
        <p>投标截止时间：<strong>2026-10-20 10:00</strong>。</p>
        <p>请按时递交投标文件。</p>
      </div>
      <footer>页脚不参与比对</footer>
    </body></html>
    """
    html_a_reformatted = """
    <html><body>
      <nav>导航文字不参与比对</nav>
      <table class="fields">
        <tr>
          <th>项目编号</th>
          <td aria-hidden="true">隐藏的附加文字</td>
          <td>RW-001</td>
        </tr>
        <tr><th>发布时间</th><td>2026-10-01 09:00:00</td></tr>
      </table>
      <div class="content">
        <p>投标截止时间：<strong>2026-10-20 10:00</strong>。</p>

        <p>请按时&#160;递交投标文件。</p>
      </div>
      <footer>页脚不参与比对</footer>
    </body></html>
    """
    text_a = extract_visible_content(html_a)
    text_a2 = extract_visible_content(html_a_reformatted)
    assert_equal(comparison_key(text_a), comparison_key(text_a2),
                 "纯排版空白与隐藏元素不影响可见文字比对")

    url = "http://test/detail/rw-001.html"
    ann_a = Announcement(url=url, project_no="RW-001", title="原始公告", content_text=text_a)
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "baseline.sqlite3"
        first = process_baseline("http://test/list", [ann_a], complete=True, db_path=db)
        assert_equal(first.first_baseline, True, "首次完整采集建立基线")
        assert_equal(first.changes, [], "首次采集不报告改写")

        html_b = html_a.replace("2026-10-20 10:00", "2026-10-27 10:00").replace(
            "请按时递交投标文件。", "投标安排已调整，不再另行发布更正。"
        )
        ann_b = Announcement(
            url=url, project_no="RW-001", title="原始公告", content_text=extract_visible_content(html_b)
        )
        second = process_baseline("http://test/list", [ann_b], complete=True, db_path=db)
        assert_equal(len(second.changes), 1, "同一详情 URL 的文字变化被识别")
        assert_equal(second.changes[0].detail_url, url, "以详情 URL 识别公告")
        assert_equal("2026-10-20" in second.changes[0].before_text, True, "展示变动前文字")
        assert_equal("2026-10-27" in second.changes[0].after_text, True, "展示变动后文字")

        saved = load_baseline("http://test/list", db)
        assert_equal(saved[url].content_text, ann_b.content_text, "完整采集后基线更新为本次结果")

        third = process_baseline("http://test/list", [ann_a], complete=False, db_path=db)
        assert_equal(third.changes, [], "不完整采集不进行改写比对")
        assert_equal(load_baseline("http://test/list", db)[url].content_text,
                     ann_b.content_text, "不完整采集不覆盖原基线")

    print("\n全部测试通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
