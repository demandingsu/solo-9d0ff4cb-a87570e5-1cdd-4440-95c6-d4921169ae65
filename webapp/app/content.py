"""提取公告详情中的可比对可见文字。

只关注两块内容：
1. 带字段标签的信息表（如项目编号、发布时间、投标截止时间）；
2. 正文区（.content / .article-content / article / main 等常见容器）。

导航、页脚、脚本、样式以及明确隐藏的元素不参与比对。纯排版造成的空白差异
（缩进、换行、单元格内多余空格）会在归一化时被忽略。
"""
from __future__ import annotations

import re

from bs4 import BeautifulSoup, Comment, NavigableString, Tag

# 明确不可见或不属于正文的元素
_HIDDEN_TAGS = {
    "head", "script", "style", "noscript", "template", "iframe",
    "svg", "canvas", "object", "embed",
}

# 这些元素之间应保留换行，避免把相邻段落拼成一句；行内元素则自然衔接。
_BLOCK_TAGS = {
    "address", "article", "blockquote", "dd", "div", "dl", "dt", "fieldset",
    "figcaption", "figure", "footer", "form", "h1", "h2", "h3", "h4", "h5",
    "h6", "header", "hr", "li", "main", "nav", "ol", "p", "pre", "section",
    "table", "tbody", "td", "tfoot", "th", "thead", "tr", "ul",
}

# 常见正文容器，按优先级从高到低。先找精确类名，再用较宽泛的兜底。
_CONTENT_SELECTORS = (
    "div.content",
    "div#content",
    "div.article-content",
    "div.notice-content",
    "div.detail-content",
    "div.content-body",
    "div.article-body",
    "div.detail-body",
    "#article-content",
    "#notice-content",
    "#detail-content",
    "article",
    "main",
    "div[class*='article']",
    "div[class*='notice-detail']",
    "div[class*='detail']",
    "div[class*='content']",
)

_FIELD_LABEL_RE = re.compile(
    "|".join(
        (
            "项目编号", "招标项目编号", "采购项目编号", "项目编码", "项目名称",
            "公告类型", "发布时间", "公告发布时间", "发布日期",
            "投标截止时间", "投标截止日期", "开标时间",
            "投标文件递交截止时间", "截止时间",
        )
    )
)

# 未找到正文容器时，从整页中剔除这些站点框架元素
_NON_BODY_SELECTORS = (
    "nav", "header", "footer", "aside", "form", ".crumb", ".breadcrumb",
    ".breadcrumbs", ".nav", ".navbar", ".sidebar", ".footer", ".header",
)


def normalize_visible_text(text: str) -> str:
    """归一化可见文字，消除纯排版空白。

    - 不换行空格转为普通空格；
    - 每行内部的连续空白折叠为一个空格；
    - 去掉每行首尾空白和空行。
    """
    text = (text or "").replace("\xa0", " ").replace("　", " ")
    lines: list[str] = []
    for line in text.splitlines():
        clean = re.sub(r"\s+", " ", line).strip()
        if clean:
            lines.append(clean)
    return "\n".join(lines)


def comparison_key(text: str) -> str:
    """用于判断文字是否真正变化的指纹：忽略所有空白差异。"""
    return re.sub(r"\s+", "", text or "")


def _visible_text(node: Tag) -> str:
    """提取节点文字，跳过不可见标签与注释，并在块级元素之间保留换行。"""
    chunks: list[str] = []

    def walk(current) -> None:
        if isinstance(current, Comment):
            return
        if isinstance(current, NavigableString):
            chunks.append(str(current))
            return
        if not isinstance(current, Tag) or _is_hidden(current):
            return
        if current.name == "br":
            chunks.append("\n")
            return
        block = current.name in _BLOCK_TAGS
        if block:
            chunks.append("\n")
        for child in current.children:
            walk(child)
        if block:
            chunks.append("\n")

    walk(node)
    raw = "".join(chunks).replace("\r\n", "\n").replace("\r", "\n")
    return normalize_visible_text(raw)


def _is_hidden(node: Tag) -> bool:
    if node.name in _HIDDEN_TAGS:
        return True
    style = re.sub(r"\s+", "", str(node.get("style", "")).lower())
    if "display:none" in style or "visibility:hidden" in style:
        return True
    if node.has_attr("hidden"):
        return True
    return str(node.get("aria-hidden", "")).lower() == "true"


def _remove_hidden(soup: BeautifulSoup) -> None:
    for node in list(soup.find_all(True)):
        # 父节点已删除后，子节点可能已经脱离文档，避免重复 decompose。
        if node.parent is None:
            continue
        if _is_hidden(node):
            node.decompose()


def _join_cell_text(cells: list[Tag]) -> str:
    values = [_visible_text(cell) for cell in cells]
    values = [value.replace("\n", " ") for value in values if value]
    if not values:
        return ""
    if len(values) == 2:
        return f"{values[0]}：{values[1]}"
    return " ｜ ".join(values)


def _table_text(tables: list[Tag]) -> str:
    lines: list[str] = []
    for table in tables:
        for row in table.find_all("tr"):
            cells = row.find_all(["th", "td"])
            if not cells:
                continue
            line = _join_cell_text(cells)
            if line:
                lines.append(line)
        # 非表格布局的字段（少见的 div/span 模拟表格）也保留可见文字
        if not table.find_all("tr"):
            text = _visible_text(table)
            if text:
                lines.append(text)
    return "\n".join(lines)


def _content_node(soup: BeautifulSoup) -> Tag | None:
    for selector in _CONTENT_SELECTORS:
        for node in soup.select(selector):
            if (node.name == "main" or node.name == "article") and not _visible_text(node):
                continue
            if _visible_text(node):
                return node
    return None


def _fallback_body_text(soup: BeautifulSoup) -> str:
    """没有明确正文容器时，退化为剔除站点框架后的页面可见文字。"""
    body = soup.body
    if body is None:
        return ""
    clone = BeautifulSoup(str(body), "html.parser")
    for selector in _NON_BODY_SELECTORS:
        for node in clone.select(selector):
            node.decompose()
    for node in clone.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "table"]):
        node.decompose()
    return _visible_text(clone)


def extract_visible_content(html: str) -> str:
    """提取字段表与正文区的可见文字，返回可直接展示/存储的归一化文本。"""
    soup = BeautifulSoup(html, "html.parser")
    _remove_hidden(soup)

    all_tables = soup.find_all("table")
    field_tables = [table for table in all_tables if _FIELD_LABEL_RE.search(table.get_text(" ", strip=True))]
    selected_tables = field_tables or all_tables

    table_text = _table_text(selected_tables)
    content = _content_node(soup)

    if content is not None:
        # 若正文容器包含信息表，表已单独提取，避免重复。
        clone = BeautifulSoup(str(content), "html.parser")
        for table in clone.find_all("table"):
            table.decompose()
        for selector in _NON_BODY_SELECTORS:
            for node in clone.select(selector):
                node.decompose()
        body_text = _visible_text(clone)
    else:
        body_text = _fallback_body_text(soup)

    parts = [part for part in (table_text, body_text) if part.strip()]
    return normalize_visible_text("\n".join(parts))


# 兼容不同调用方的命名习惯
extract_announcement_text = extract_visible_content
extract_snapshot_text = extract_visible_content
