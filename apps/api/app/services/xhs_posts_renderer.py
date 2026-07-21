from __future__ import annotations

from app.schemas.xhs_planning import XhsResearchResult


def render_xhs_posts(research: XhsResearchResult) -> str:
    lines = [
        "# 小红书原帖检索结果",
        "",
        f"> 搜索词：{research.keyword}",
        "> 以下正文由小红书 MCP 从笔记详情读取，未经过 LLM 改写。",
        "",
    ]
    for index, post in enumerate(research.posts, start=1):
        lines.extend(
            [
                f"## {index}. 《{post.title}》",
                "",
                f"- 作者：{post.author_name}",
                f"- 点赞：{post.liked_count_raw or '未提供'}",
                f"- 发布时间：{post.published_at or '未提供'}",
                f"- 笔记 ID：{post.note_id}",
                f"- 查询时间：{post.queried_at.isoformat()}",
                "",
                post.content,
                "",
            ]
        )
        if index < len(research.posts):
            lines.extend(["---", ""])

    if research.warnings:
        lines.extend(["## 读取说明", ""])
        lines.extend(f"- {warning}" for warning in research.warnings)
        lines.append("")

    lines.extend(
        [
            "## 使用说明",
            "",
            "- 正文按 MCP 返回内容原样输出；标题、作者、点赞数等为检索时读取的元数据。",
            "- 原帖属于作者主观体验，不代表平台全部内容；时效信息请在出发前复核。",
        ]
    )
    return "\n".join(lines)


__all__ = ["render_xhs_posts"]
