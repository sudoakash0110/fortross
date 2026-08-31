"""Best-effort first-page post cards; no requests or pagination in this module."""

import re
from datetime import datetime
from urllib.parse import urljoin

from bs4 import Tag

from fortross.linkedin.errors import ParseError
from fortross.linkedin.flight import FlightDocument
from fortross.models import LinkedInPost, PostAuthor

_ACTIVITY = re.compile(r"(?:urn:li:activity:|activity-)(\d{5,})")


def _text(tag: Tag | None) -> str | None:
    return tag.get_text(" ", strip=True) or None if tag is not None else None


def _id(value: object) -> str | None:
    match = _ACTIVITY.search(str(value))
    return match.group(1) if match else None


def _post_id(card: Tag) -> str | None:
    direct = (
        _id(card.get("data-urn")) or _id(card.get("data-id")) or _id(card.get("data-component-key"))
    )
    if direct:
        return direct
    if card.name == "article" or card.get("role") == "listitem":
        identifiers = {_id(link["href"]) for link in card.find_all("a", href=True)} - {None}
        if len(identifiers) == 1:
            return identifiers.pop()
    return None


def _timestamp(tag: Tag | None) -> datetime | None:
    if tag is None or not tag.get("datetime"):
        return None
    try:
        value = datetime.fromisoformat(str(tag["datetime"]).replace("Z", "+00:00"))
        return value if value.tzinfo else None
    except ValueError:
        return None


def _parse_card(card: Tag, identifier: str) -> LinkedInPost:
    body = card.select_one(
        ".update-components-text, .feed-shared-update-v2__description, "
        "[data-component-key*=commentary], [data-component-key*=postText]"
    )
    actor = card.select_one(".update-components-actor__name, .feed-shared-actor__name")
    actor_link = card.select_one(
        "a.update-components-actor__meta-link, a.feed-shared-actor__container-link"
    )
    header = _text(card.select_one(".update-components-header, header")) or ""
    reposted = bool(re.search(r"\b(reposted|reshared)\b", header, re.I))
    original = card.select_one(".update-components-reshared-content")
    original_id = _post_id(original) if original is not None else None
    if original is not None:
        reposted = True
        if original_id is None:
            original_id = next(
                (
                    value
                    for node in original.select("[data-urn], [data-id], article")
                    if (value := _post_id(node))
                ),
                None,
            )
        if original_id is None:
            original_id = next(
                (value for a in original.find_all("a", href=True) if (value := _id(a["href"]))),
                None,
            )
        # Do not mislabel the original author's text as the reposter's commentary.
        if body is not None and original in body.parents:
            body = None
    time_tag = card.find("time")
    media = list(
        dict.fromkeys(
            str(img["src"])
            for img in card.select(".update-components-image img[src], .feed-shared-image img[src]")
            if str(img["src"]).startswith("https://")
        )
    )
    return LinkedInPost(
        id=identifier,
        url=f"https://www.linkedin.com/feed/update/urn:li:activity:{identifier}/",
        text=_text(body),
        published_at=_timestamp(time_tag),
        published_at_display=_text(time_tag),
        is_repost=True if reposted else None,
        author=PostAuthor(
            name=_text(actor),
            url=urljoin("https://www.linkedin.com", str(actor_link["href"]))
            if actor_link
            else None,
        ),
        original_post_url=(
            f"https://www.linkedin.com/feed/update/urn:li:activity:{original_id}/"
            if original_id
            else None
        ),
        media=media,
    )


def parse_posts_document(
    document: str, limit: int = 50
) -> tuple[list[LinkedInPost], bool, list[str]]:
    if not 1 <= limit <= 50:
        raise ValueError("Posts limit must be between 1 and 50")
    decoded = FlightDocument(document)
    html_soup = decoded.html
    rsc_soup = decoded.native_soup()
    posts: dict[str, LinkedInPost] = {}
    for soup in (html_soup, rsc_soup):
        for card in soup.select(
            "[data-urn], [data-id], [data-component-key], [role=listitem], article"
        ):
            if card.find_parent(class_="update-components-reshared-content") is not None:
                continue
            # Original cards nested in a repost are not extra activities.
            if "update-components-reshared-content" in card.get("class", []):
                continue
            identifier = _post_id(card)
            if identifier is None:
                continue
            parsed = _parse_card(card, identifier)
            if parsed.text is None and parsed.author.name is None:
                continue
            if identifier not in posts:
                posts[identifier] = parsed
            else:
                existing = posts[identifier]
                for field in ("text", "published_at", "published_at_display", "original_post_url"):
                    if getattr(existing, field) is None:
                        setattr(existing, field, getattr(parsed, field))
                if parsed.is_repost is True:
                    existing.is_repost = True
                if existing.author.name is None:
                    existing.author = parsed.author
                existing.media = list(dict.fromkeys([*existing.media, *parsed.media]))
    if not posts and not re.search(
        r"\b(no posts yet|hasn't posted yet|has not posted yet)\b",
        html_soup.get_text(" ", strip=True) + " " + rsc_soup.get_text(" ", strip=True),
        re.I,
    ):
        link_count = sum(
            1
            for soup in (html_soup, rsc_soup)
            for link in soup.find_all("a", href=True)
            if _id(link["href"])
        )
        raise ParseError(
            "No recognizable post cards in the initial document. "
            f"Hydration present: {decoded.payload is not None}; decoded rows: {len(decoded.rows)}; "
            f"activity links: {link_count}. "
            "The feed may need another transport or parser mapping. No further requests were made."
        )
    warnings = [
        "Only posts in the initial document are considered; completeness and chronological order "
        "are not guaranteed"
    ]
    selected = list(posts.values())[:limit]
    if decoded.truncated:
        warnings.append("RSC traversal reached its safety bound; results may be incomplete")
    if any(post.is_repost is None for post in selected):
        warnings.append("is_repost=null means no conclusive repost marker was found")
    if any(post.text is None for post in selected):
        warnings.append("Some post text could not be extracted")
    return selected, len(posts) > limit, warnings
