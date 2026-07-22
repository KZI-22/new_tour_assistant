from __future__ import annotations

import datetime as dt

from app.schemas.xhs_planning import XhsPostEvidence, XhsResearchResult

_BEIJING_TIMEZONE = dt.timezone(dt.timedelta(hours=8))


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
                f"- 发布时间：{_format_published_at(post.published_at)}",
                f"- 笔记 ID：{post.note_id}",
                f"- 查询时间：{_format_beijing_time(post.queried_at)}",
                "",
            ]
        )
        if post.images:
            lines.extend(
                [
                    "### 原帖图片",
                    "",
                    f"> 共 {len(post.images)} 张，按原帖顺序排列；点击图片查看原图。",
                    "",
                    _render_image_gallery(post),
                    "",
                ]
            )
        lines.extend(
            [
                "### 原帖正文",
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
            "- 正文按 MCP 返回内容原样输出；图片按详情页顺序展示；"
            "标题、作者、点赞数等为检索时读取的元数据。",
            "- 原帖属于作者主观体验，不代表平台全部内容；时效信息请在出发前复核。",
        ]
    )
    return "\n".join(lines)


def _render_image_gallery(post: XhsPostEvidence) -> str:
    images: list[str] = []
    for image in post.images:
        label = f"P{image.index}"
        if image.live_photo:
            label += " · 实况"
        images.append(
            f'[![小红书原帖图片 {label}](<{image.preview_url}>)](<{image.original_url}> "{label}")'
        )
    return " ".join(images)


def _format_published_at(value: str | None) -> str:
    if not value:
        return "未提供"
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError:
        return value
    if parsed.tzinfo is None:
        return f"{parsed:%Y-%m-%d %H:%M:%S}"
    return _format_beijing_time(parsed)


def _format_beijing_time(value: dt.datetime) -> str:
    local_time = value.astimezone(_BEIJING_TIMEZONE)
    return f"{local_time:%Y-%m-%d %H:%M:%S}（北京时间）"


__all__ = ["render_xhs_posts"]
