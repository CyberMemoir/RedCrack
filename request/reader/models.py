from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

from .errors import ReaderError

NOTE_ID = re.compile(r"^[0-9a-fA-F]{24}$")
NOTE_PATH = re.compile(r"^/(?:explore|discovery/item|search_result)/([0-9a-fA-F]{24})/?$")
NOTE_HOSTS = {"www.xiaohongshu.com", "xiaohongshu.com"}
SHORT_HOSTS = {"xhslink.com", "www.xhslink.com"}


def normalize_note_url(value: str) -> str:
    """接受笔记 ID、完整链接或包含小红书链接的分享文案。"""
    value = value.strip()
    if NOTE_ID.fullmatch(value):
        return f"https://www.xiaohongshu.com/explore/{value.lower()}"
    match = re.search(r"https?://[^\s<>\"'，。；！？（）【】]+", value)
    if not match:
        raise ValueError("请输入小红书笔记链接、分享文案或 24 位笔记 ID")
    url = match.group().rstrip(".,;!?)〕]")
    parts = urlsplit(url)
    if parts.username or parts.password or parts.port not in (None, 80, 443):
        raise ValueError("不支持带凭据或非标准端口的链接")
    if parts.hostname in SHORT_HOSTS:
        if not parts.path.strip("/"):
            raise ValueError("短链接缺少分享路径")
        return urlunsplit(("https", parts.hostname, parts.path, parts.query, ""))
    if parts.hostname not in NOTE_HOSTS:
        raise ValueError("仅支持 xiaohongshu.com 笔记或 xhslink.com 分享链接")
    path_match = NOTE_PATH.fullmatch(parts.path)
    if not path_match:
        raise ValueError("链接中未找到有效的笔记 ID")
    query = parse_qs(parts.query)
    kept = {k: query[k][0] for k in ("xsec_token", "xsec_source") if query.get(k)}
    path = f"/explore/{path_match.group(1).lower()}"
    return urlunsplit(("https", "www.xiaohongshu.com", path, urlencode(kept), ""))


def note_id_from_url(url: str) -> str:
    match = NOTE_PATH.fullmatch(urlsplit(url).path)
    return match.group(1).lower() if match else ""


def _get(data: dict, *names: str, default: Any = "") -> Any:
    for name in names:
        if data.get(name) is not None:
            return data[name]
    return default


def _http_url(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    value = "https:" + value if value.startswith("//") else value
    if value.startswith("http://"):
        value = "https://" + value[7:]
    return value if value.startswith("https://") else ""


def _image_url(item: dict) -> str:
    # 保留页面返回的变体，不根据 file_id 拼接或改写 CDN 路径。
    for key in ("urlDefault", "url_default", "url", "urlPre", "url_pre"):
        if url := _http_url(item.get(key)):
            return url
    variants = _get(item, "infoList", "info_list", default=[]) or []
    variants = sorted(variants, key=lambda x: x.get("image_scene", x.get("imageScene")) != "WB_DFT")
    return next((url for x in variants if (url := _http_url(x.get("url")))), "")


@dataclass(slots=True)
class MediaAsset:
    kind: str
    url: str


@dataclass(slots=True)
class Note:
    note_id: str
    title: str = ""
    description: str = ""
    kind: str = "normal"
    author: str = ""
    author_id: str = ""
    url: str = ""
    tags: list[str] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)
    published_at: int | str | None = None
    media: list[MediaAsset] = field(default_factory=list)
    observed_at: str | None = None
    cached: bool = False
    evidence: list[dict[str, Any]] = field(default_factory=list)
    subtitle_tracks: list[dict[str, str]] = field(default_factory=list)
    access: dict[str, Any] = field(default_factory=dict)
    tag_refs: list[dict[str, str]] = field(default_factory=list)
    content_scope: str = "unknown"

    @property
    def canonical_url(self) -> str:
        return f"https://www.xiaohongshu.com/explore/{self.note_id.lower()}"

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "canonical_url": self.canonical_url}


def parse_note(item: dict, *, fallback_url: str = "") -> Note:
    card = _get(item, "note_card", "noteCard", "note", default=item)
    if not isinstance(card, dict):
        raise ReaderError("笔记数据格式不受支持")
    note_id = str(
        _get(card, "note_id", "noteId", "id") or item.get("id") or note_id_from_url(fallback_url)
    )
    if not NOTE_ID.fullmatch(note_id):
        raise ReaderError("响应缺少有效的笔记 ID")
    note_id = note_id.lower()
    token = _get(item, "xsec_token", "xsecToken") or _get(card, "xsec_token", "xsecToken")
    url = (
        normalize_note_url(fallback_url)
        if fallback_url and note_id_from_url(fallback_url) == note_id
        else f"https://www.xiaohongshu.com/explore/{note_id}"
    )
    if token:
        # A response token belongs to this ID; retain a known source rather than
        # silently relabelling detail/feed access as search access.
        source = parse_qs(urlsplit(url).query).get("xsec_source", [""])[0]
        source = (
            source
            or _get(item, "xsec_source", "xsecSource")
            or _get(card, "xsec_source", "xsecSource")
        )
        query = {"xsec_token": token}
        if source:
            query["xsec_source"] = source
        url = f"https://www.xiaohongshu.com/explore/{note_id}?{urlencode(query)}"
    user = card.get("user") or {}
    media: list[MediaAsset] = []
    images = _get(card, "image_list", "imageList", default=[]) or []
    for entry in images:
        if isinstance(entry, dict) and (image_url := _image_url(entry)):
            media.append(MediaAsset("image", image_url))
    kind = str(card.get("type") or "normal")
    if kind == "video":
        video = card.get("video") or {}
        direct = _http_url(_get(video, "url", "url_default", "urlDefault"))
        stream = (video.get("media") or {}).get("stream") or {}
        candidates = []
        for codec in ("h264", "h265", "av1"):
            for entry in stream.get(codec) or []:
                primary = _get(entry, "master_url", "masterUrl")
                backups = _get(entry, "backup_urls", "backupUrls", default=[]) or []
                source = _http_url(primary) or next((u for x in backups if (u := _http_url(x))), "")
                if source:
                    try:
                        score = int(_get(entry, "width", default=0)) * int(
                            _get(entry, "height", default=0)
                        )
                    except (ValueError, TypeError):
                        score = 0
                    candidates.append((score, source))
            if candidates:  # 优先 H.264，兼容常见播放器。
                break
        if candidates or direct:
            media.insert(
                0,
                MediaAsset(
                    "video", max(candidates, key=lambda x: x[0])[1] if candidates else direct
                ),
            )
    unique = list({(asset.kind, asset.url): asset for asset in media}.values())
    raw_tags = _get(card, "tag_list", "tagList", default=[]) or []
    tags = [str(x.get("name")) for x in raw_tags if isinstance(x, dict) and x.get("name")]
    interactions = _get(card, "interact_info", "interactInfo", default={}) or {}
    stats = {
        key: _get(interactions, *aliases, default=None)
        for key, aliases in {
            "likes": ("liked_count", "likedCount"),
            "collects": ("collected_count", "collectedCount"),
            "comments": ("comment_count", "commentCount"),
            "shares": ("share_count", "shareCount"),
        }.items()
    }
    return Note(
        note_id=note_id.lower(),
        title=str(_get(card, "title", "display_title", "displayTitle")),
        description=str(card.get("desc") or ""),
        kind=kind,
        author=str(_get(user, "nickname", "nick_name", "nickName")),
        author_id=str(_get(user, "user_id", "userId")),
        url=url,
        tags=tags,
        stats=stats,
        published_at=_get(card, "time", "publish_time", "publishTime", default=None),
        media=unique,
        tag_refs=[
            {"id": str(_get(tag, "id", "tag_id", "tagId")), "name": str(tag.get("name") or "")}
            for tag in raw_tags
            if isinstance(tag, dict) and _get(tag, "id", "tag_id", "tagId")
        ],
    )


def parse_visible_card(card: dict, *, source_url: str) -> Note:
    """Choose a same-ID access link; preserve source-card navigation privately."""
    original = normalize_note_url(card["url"])
    wanted = note_id_from_url(original)
    links = []
    for candidate in card.get("links", [original]):
        try:
            url = normalize_note_url(candidate)
        except ValueError:
            continue
        if note_id_from_url(url) == wanted:
            links.append((url, candidate))
    chosen, original_access = next(
        ((url, raw) for url, raw in links if parse_qs(urlsplit(url).query).get("xsec_token")),
        (original, card["url"]),
    )
    return Note(
        wanted,
        title=card["title"],
        author=card["author"],
        url=chosen,
        content_scope="search_card",
        access={
            "url": original_access,
            "source_url": source_url,
            "method": "navigate"
            if parse_qs(urlsplit(chosen).query).get("xsec_token")
            else "source_card",
        },
    )


def parse_search_payload(payload: dict) -> list[Note]:
    data = payload.get("data") or {}
    if not isinstance(data, dict):
        return []
    result = []
    for item in data.get("items") or []:
        if not isinstance(item, dict) or item.get("model_type", "note") not in (
            "note",
            "note_card",
        ):
            continue
        try:
            result.append(parse_note(item))
        except ReaderError:
            continue
    return result
