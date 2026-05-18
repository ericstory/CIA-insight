"""Export content briefs for each track — intended for mobile-opr /produce input.

Output layout (inside the topic report dir):
  briefs/
    briefs.json               — machine-readable, all tracks
    01-virtual-phone.md       — one brief per track (direct /produce input)
    02-real-time-translation.md
    ...

Usage:
  python3 cli.py export-briefs --topic "ai phone receptionist"
  python3 cli.py export-briefs --topic "ai phone receptionist" --min-items 3 --top-tt 15 --top-yt 10
"""
from __future__ import annotations

import json
import pathlib
import re
import sys
from datetime import date

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
import db
import config

_MIN_ITEMS_DEFAULT = 3   # skip track if total social < this
_TOP_TT = 15             # top N TikTok by plays
_TOP_YT = 10             # top N YouTube by views


# ── DB queries ────────────────────────────────────────────────────────────────

def _load_tracks(db_path: pathlib.Path) -> list[dict]:
    rows = db.query_df(db_path, "SELECT track_id, name, name_en, description FROM tracks ORDER BY track_id")
    if rows.empty:
        return []
    return rows.to_dict(orient="records")


def _load_tiktok(db_path: pathlib.Path, track_id: int, top: int) -> list[dict]:
    df = db.query_df(db_path, f"""
        SELECT t.text, t.plays, t.likes, t.shares, t.url
        FROM social_tiktok t
        JOIN social_cluster_map m ON m.item_id = t.id AND m.source = 'tiktok'
        WHERE m.cluster_id = ?
        ORDER BY t.plays DESC
        LIMIT {top}
    """, (track_id,))
    return df.to_dict(orient="records") if not df.empty else []


def _load_youtube(db_path: pathlib.Path, track_id: int, top: int) -> list[dict]:
    df = db.query_df(db_path, f"""
        SELECT y.title, y.views, y.likes, y.description, y.url
        FROM social_youtube y
        JOIN social_cluster_map m ON m.item_id = y.id AND m.source = 'youtube'
        WHERE m.cluster_id = ?
        ORDER BY y.views DESC
        LIMIT {top}
    """, (track_id,))
    return df.to_dict(orient="records") if not df.empty else []


# ── LLM synthesis ─────────────────────────────────────────────────────────────

def _haiku_brief(track_name_en: str, tiktok_items: list[dict], youtube_items: list[dict]) -> dict:
    """Call Haiku once per track to extract hooks + audience language + angle."""
    try:
        import anthropic
        api_key = config.get("ANTHROPIC_AUTH_TOKEN") or config.get("ANTHROPIC_API_KEY")
        base_url = config.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
        if not api_key:
            raise RuntimeError("ANTHROPIC_AUTH_TOKEN not set")
        client = anthropic.Anthropic(api_key=api_key, base_url=base_url)

        tt_lines = "\n".join(
            f"- [{_fmt_num(r['plays'])} plays] {str(r['text'] or '').strip()[:200]}"
            for r in tiktok_items[:10]
        )
        yt_lines = "\n".join(
            f"- [{_fmt_num(r['views'])} views] {str(r['title'] or '').strip()}"
            for r in youtube_items[:8]
        )

        prompt = f"""You are a short-video content strategist. Analyze these top-performing TikTok and YouTube posts in the "{track_name_en}" niche.

TikTok (by plays):
{tt_lines or "(none)"}

YouTube (by views):
{yt_lines or "(none)"}

Return ONLY a JSON object with these keys:
{{
  "viral_hooks": ["hook1", "hook2", ...],        // 3-5 recurring hooks/angles that drive views (English, short phrases)
  "audience_language": ["phrase1", ...],          // 4-6 exact phrases the audience uses to describe their pain/need
  "content_angle": "one paragraph in Chinese: recommended story angle + differentiation tip for a new video in this niche",
  "hashtags": ["#tag1", "#tag2", ...]             // 5-8 relevant hashtags extracted from TikTok posts
}}"""

        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=800,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = msg.content[0].text.strip()
        result = _parse_json_robust(raw)
        if result:
            return result
    except Exception as e:
        print(f"  [haiku] {track_name_en}: {e}")
    return {"viral_hooks": [], "audience_language": [], "content_angle": "", "hashtags": []}


def _parse_json_robust(raw: str) -> dict | None:
    """Try to parse LLM JSON output, with fallback for common encoding issues."""
    m = re.search(r'\{.*\}', raw, re.DOTALL)
    if not m:
        return None
    candidate = m.group(0)
    # First attempt: direct parse
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        pass
    # Second attempt: sanitize newlines inside string values
    # Replace literal newlines inside JSON strings with \n escape
    try:
        sanitized = re.sub(r'(?<!\\)\n', ' ', candidate)
        return json.loads(sanitized)
    except json.JSONDecodeError:
        pass
    # Third attempt: regex-extract each field individually
    result: dict = {}
    for key in ("viral_hooks", "audience_language", "hashtags"):
        arr_m = re.search(rf'"{key}"\s*:\s*(\[.*?\])', raw, re.DOTALL)
        if arr_m:
            try:
                result[key] = json.loads(arr_m.group(1))
            except Exception:
                result[key] = []
        else:
            result[key] = []
    angle_m = re.search(r'"content_angle"\s*:\s*"(.*?)"(?=\s*[,}])', raw, re.DOTALL)
    result["content_angle"] = angle_m.group(1).replace("\\n", " ") if angle_m else ""
    return result if result else None


def _fmt_num(n) -> str:
    if n is None:
        return "?"
    n = int(n)
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n/1_000:.0f}K"
    return str(n)


def _slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


# ── Markdown renderer ──────────────────────────────────────────────────────────

def _render_md(track: dict, tiktok: list[dict], youtube: list[dict], llm: dict) -> str:
    name_en = track.get("name_en") or track.get("name", "")
    name_zh = track.get("name", "")
    desc = track.get("description") or ""
    today = date.today().isoformat()

    lines = [
        f"# Content Brief: {name_en}",
        f"",
        f"> 赛道：{name_zh}  |  生成日期：{today}",
        f"> {desc}",
        f"",
        f"---",
        f"",
        f"## 爆款钩子（TikTok 提炼）",
        f"",
    ]
    for h in llm.get("viral_hooks") or []:
        lines.append(f"- {h}")
    if not llm.get("viral_hooks"):
        lines.append("_(数据不足)_")

    lines += [
        f"",
        f"## 受众语言",
        f"",
    ]
    for p in llm.get("audience_language") or []:
        lines.append(f'- "{p}"')
    if not llm.get("audience_language"):
        lines.append("_(数据不足)_")

    lines += [
        f"",
        f"## 建议内容角度",
        f"",
        llm.get("content_angle") or "_(数据不足)_",
        f"",
        f"## 常用话题标签",
        f"",
        " ".join(llm.get("hashtags") or []) or "_(无)_",
        f"",
        f"---",
        f"",
        f"## TikTok 爆款原文（Top {min(len(tiktok), 15)}，按播放量）",
        f"",
        f"| 播放 | 点赞 | 内容摘要 | 链接 |",
        f"|------|------|----------|------|",
    ]
    for r in tiktok:
        text = str(r.get("text") or "").replace("\n", " ").strip()[:120]
        plays = _fmt_num(r.get("plays"))
        likes = _fmt_num(r.get("likes"))
        url = r.get("url") or ""
        lines.append(f"| {plays} | {likes} | {text} | [↗]({url}) |")

    lines += [
        f"",
        f"## YouTube 爆款（Top {min(len(youtube), 10)}，按播放量）",
        f"",
        f"| 播放 | 标题 | 链接 |",
        f"|------|------|------|",
    ]
    for r in youtube:
        title = str(r.get("title") or "").strip()[:100]
        views = _fmt_num(r.get("views"))
        url = r.get("url") or ""
        lines.append(f"| {views} | {title} | [↗]({url}) |")

    return "\n".join(lines) + "\n"


# ── Main entry ─────────────────────────────────────────────────────────────────

def export_briefs(
    db_path: pathlib.Path,
    min_items: int = _MIN_ITEMS_DEFAULT,
    top_tt: int = _TOP_TT,
    top_yt: int = _TOP_YT,
) -> pathlib.Path:
    """Generate content briefs for all tracks with sufficient social data."""
    out_dir = db_path.parent / "briefs"
    out_dir.mkdir(exist_ok=True)

    tracks = _load_tracks(db_path)
    if not tracks:
        print("No tracks found. Run propose-tracks + apply-tracks first.")
        return out_dir

    topic = db.get_meta(db_path, "topic", "") or ""
    all_briefs = {"topic": topic, "generated_at": date.today().isoformat(), "tracks": []}

    for i, track in enumerate(tracks, 1):
        tid = track["track_id"]
        name_en = track.get("name_en") or track.get("name", f"Track {tid}")
        tiktok = _load_tiktok(db_path, tid, top_tt)
        youtube = _load_youtube(db_path, tid, top_yt)
        total = len(tiktok) + len(youtube)

        if total < min_items:
            print(f"  skip  [{i}] {name_en}  (only {total} social items)")
            continue

        print(f"  brief [{i}] {name_en}  (TikTok={len(tiktok)}, YouTube={len(youtube)}) → calling Haiku…")
        llm = _haiku_brief(name_en, tiktok, youtube)

        brief = {
            "track_id": tid,
            "name": track.get("name", ""),
            "name_en": name_en,
            "description": track.get("description") or "",
            "tiktok_count": len(tiktok),
            "youtube_count": len(youtube),
            "viral_hooks": llm.get("viral_hooks", []),
            "audience_language": llm.get("audience_language", []),
            "content_angle": llm.get("content_angle", ""),
            "hashtags": llm.get("hashtags", []),
            "top_tiktok": [
                {"text": r.get("text", ""), "plays": r.get("plays"), "url": r.get("url")}
                for r in tiktok[:10]
            ],
            "top_youtube": [
                {"title": r.get("title", ""), "views": r.get("views"), "url": r.get("url")}
                for r in youtube[:8]
            ],
        }
        all_briefs["tracks"].append(brief)

        slug = f"{i:02d}-{_slugify(name_en)}"
        md_path = out_dir / f"{slug}.md"
        md_path.write_text(_render_md(track, tiktok, youtube, llm), encoding="utf-8")
        print(f"         → {md_path.name}")

    json_path = out_dir / "briefs.json"
    json_path.write_text(json.dumps(all_briefs, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nbriefs.json: {json_path}")
    print(f"total tracks exported: {len(all_briefs['tracks'])}")
    return out_dir
