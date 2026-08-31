"""Normalize the first Voyager member-share-feed collection without fetching anything."""

import json
import re
from urllib.parse import urlsplit, urlunsplit

from fortross.linkedin.errors import ParseError
from fortross.models import LinkedInPost, PostAuthor, PostContent

_UPDATE = "com.linkedin.voyager.dash.feed.Update"
_ACTIVITY = re.compile(r"urn:li:activity:(\d{5,})(?!\d)")


def _dict(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _text(value: object) -> str | None:
    value = _dict(value).get("text")
    return value.strip() or None if isinstance(value, str) else None


def _https(value: object, *, strip_query: bool = False) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parts = urlsplit(value)
        if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
            return None
        return urlunsplit(parts._replace(query="", fragment="")) if strip_query else value
    except ValueError:
        return None


def _identifier(record: dict) -> str:
    urn = _dict(record.get("metadata")).get("backendUrn") or record.get("entityUrn")
    match = _ACTIVITY.search(urn) if isinstance(urn, str) else None
    if not match:
        raise ParseError("Feed update has no recognizable activity ID")
    return match.group(1)


def _media(content: object) -> list[str]:
    """Only content images, never actor avatars. Choose one rendition per vector image."""
    images: list[str] = []
    stack = [(content, 0)]
    visited = 0
    while stack and visited < 10_000:
        node, depth = stack.pop()
        visited += 1
        if depth > 20:
            continue
        if isinstance(node, list):
            stack.extend((child, depth + 1) for child in reversed(node))
        elif isinstance(node, dict):
            root = _https(node.get("rootUrl"))
            artifacts = node.get("artifacts")
            if root and isinstance(artifacts, list):
                candidates = []
                for item in artifacts:
                    item = _dict(item)
                    segment = item.get("fileIdentifyingUrlPathSegment")
                    width = item.get("width")
                    if isinstance(segment, str):
                        url = _https(root + segment)
                        if url:
                            candidates.append((width if isinstance(width, int) else 0, url))
                if candidates:
                    images.append(max(candidates, key=lambda item: item[0])[1])
                continue
            direct = node.get("imageUrl")
            url = _https(_dict(direct).get("url") if isinstance(direct, dict) else direct)
            if url:
                images.append(url)
            stack.extend(
                (child, depth + 1)
                for key, child in reversed(list(node.items()))
                if key not in {"actor", "actorComponent", "header", "navigationContext"}
            )
    return list(dict.fromkeys(images))


def _content(record: dict) -> PostContent:
    identifier = _identifier(record)
    actor = _dict(record.get("actor"))
    return PostContent(
        id=identifier,
        url=f"https://www.linkedin.com/feed/update/urn:li:activity:{identifier}/",
        text=_text(_dict(record.get("commentary")).get("text")),
        author=PostAuthor(
            name=_text(actor.get("name")),
            url=_https(_dict(actor.get("navigationContext")).get("actionTarget"), strip_query=True),
        ),
        published_at_display=_text(actor.get("subDescription")),
        # No explicit publication timestamp was present in the observed contract.
        media=_media([record.get("content"), record.get("additionalContents")]),
    )


def parse_posts_feed(document: str, limit: int = 50) -> tuple[list[LinkedInPost], bool, list[str]]:
    if not 1 <= limit <= 50:
        raise ValueError("Posts limit must be between 1 and 50")
    try:
        payload = json.loads(document)
    except (ValueError, RecursionError) as exc:
        raise ParseError("LinkedIn feed response was not valid JSON") from exc
    payload = _dict(payload)
    envelope = _dict(payload.get("data"))
    data = _dict(envelope.get("data")) if "data" in envelope else envelope
    if any(item.get("errors") for item in (payload, envelope, data)):
        raise ParseError("LinkedIn returned GraphQL feed errors; no fallback request made")
    collection = _dict(data.get("feedDashProfileUpdatesByMemberShareFeed"))
    references = collection.get("*elements")
    records = payload.get("included")
    if not isinstance(references, list) or not isinstance(records, list):
        raise ParseError("LinkedIn feed collection structure was not recognized")
    if len(references) > 1000:
        raise ParseError("LinkedIn feed collection exceeded the parser safety bound")
    index = {}
    for record in records:
        record = _dict(record)
        urn = record.get("entityUrn")
        if isinstance(urn, str):
            if urn in index and index[urn] != record:
                raise ParseError("LinkedIn feed contained conflicting entity records")
            index[urn] = record

    posts: dict[str, LinkedInPost] = {}
    warnings = [
        "Only the first feed page was requested; completeness and chronological order "
        "are not guaranteed",
        "Exact publication timestamps are unavailable; relative labels are returned unchanged",
        "Media extraction includes image URLs, not video streams or document downloads",
    ]
    for reference in references:
        record = index.get(reference) if isinstance(reference, str) else None
        if not isinstance(record, dict) or record.get("$type") != _UPDATE:
            raise ParseError("LinkedIn feed referenced an unresolved or unsupported update")
        content = _content(record)
        original_ref = record.get("*resharedUpdate")
        original = index.get(original_ref) if isinstance(original_ref, str) else None
        if original is None:
            original = record.get("resharedUpdate")
        original_content = None
        if isinstance(original, dict) and original.get("$type") == _UPDATE:
            original_content = _content(original)
        repost = True if original_ref or original else None
        if repost is None and _dict(record.get("metadata")).get("rootShare") is True:
            repost = False
        if original_ref and original_content is None:
            warnings.append("An embedded original post could not be resolved")
        post = LinkedInPost(
            **content.model_dump(),
            is_repost=repost,
            original_post_url=original_content.url if original_content else None,
            original_post=original_content,
        )
        posts.setdefault(post.id, post)
    selected = list(posts.values())[:limit]
    if any(post.is_repost is None for post in selected):
        warnings.append("is_repost=null means the response did not establish repost status")
    return selected, len(posts) > limit, list(dict.fromkeys(warnings))
