"""Invented records matching the observed contract, never authenticated captures."""

import json

import pytest

from fortross.linkedin.errors import ParseError
from fortross.linkedin.feed import parse_posts_feed

UPDATE_TYPE = "com.linkedin.voyager.dash.feed.Update"


def update(identifier, text="A synthetic post", **overrides):
    record = {
        "$type": UPDATE_TYPE,
        "entityUrn": f"urn:li:fsd_update:(urn:li:activity:{identifier},MEMBER_SHARES)",
        "metadata": {"backendUrn": f"urn:li:activity:{identifier}", "rootShare": True},
        "actor": {
            "name": {"text": "Example Person"},
            "navigationContext": {
                "actionTarget": "https://www.linkedin.com/in/example/?tracking=x"
            },
            "subDescription": {"text": "2d • Edited"},
            "image": {"imageUrl": {"url": "https://media.licdn.com/avatar.png"}},
        },
        "commentary": {"text": {"text": text}},
        "resharedUpdate": None,
    }
    record.update(overrides)
    return record


def feed_payload():
    original = update("33333", "Original author's text")
    original["actor"]["name"]["text"] = "Another Person"
    original["content"] = {
        "articleComponent": {
            "smallImage": {
                "attributes": [
                    {
                        "detailData": {
                            "vectorImage": {
                                "rootUrl": "https://media.licdn.com/synthetic/",
                                "artifacts": [
                                    {
                                        "width": 100,
                                        "fileIdentifyingUrlPathSegment": "small.jpg?s=public",
                                    },
                                    {
                                        "width": 800,
                                        "fileIdentifyingUrlPathSegment": "large.jpg?s=public",
                                    },
                                ],
                            }
                        }
                    }
                ],
            }
        }
    }
    first = update(
        "11111",
        "Line one\nLine two",
        content={
            "celebrationComponent": {
                "image": {
                    "attributes": [
                        {
                            "detailData": {
                                "imageUrl": {"url": "https://media.licdn.com/celebration.png"}
                            }
                        }
                    ]
                }
            }
        },
    )
    repost = update("22222", None, **{"*resharedUpdate": original["entityUrn"]})
    repost["metadata"]["rootShare"] = False
    return {
        "data": {
            "data": {
                "feedDashProfileUpdatesByMemberShareFeed": {
                    "*elements": [first["entityUrn"], repost["entityUrn"]],
                    "paging": {"count": 2, "start": 0, "total": 0},
                    "metadata": {"paginationToken": "do-not-follow"},
                }
            }
        },
        # Intentionally not feed order; embedded original must not become a third post.
        "included": [original, repost, first],
    }


def test_order_originals_reposts_and_media():
    posts, truncated, warnings = parse_posts_feed(json.dumps(feed_payload()))
    assert [p.id for p in posts] == ["11111", "22222"]
    assert posts[0].is_repost is False
    assert posts[0].text == "Line one\nLine two"
    assert posts[0].media == ["https://media.licdn.com/celebration.png"]
    assert posts[0].published_at is None
    assert posts[0].published_at_display == "2d • Edited"
    assert posts[0].author.url == "https://www.linkedin.com/in/example/"
    repost = posts[1]
    assert repost.is_repost is True and repost.text is None
    assert repost.author.name == "Example Person"
    assert repost.original_post.id == "33333"
    assert repost.original_post.text == "Original author's text"
    assert repost.original_post.author.name == "Another Person"
    assert repost.original_post.media == ["https://media.licdn.com/synthetic/large.jpg?s=public"]
    assert repost.original_post_url == repost.original_post.url
    assert not truncated and warnings


def test_limit_and_dedup():
    payload = feed_payload()
    collection = payload["data"]["data"]["feedDashProfileUpdatesByMemberShareFeed"]
    collection["*elements"] *= 2
    posts, truncated, _ = parse_posts_feed(json.dumps(payload), 1)
    assert [p.id for p in posts] == ["11111"] and truncated


def test_explicit_empty_collection_is_success():
    payload = feed_payload()
    payload["data"]["data"]["feedDashProfileUpdatesByMemberShareFeed"]["*elements"] = []
    posts, truncated, _ = parse_posts_feed(json.dumps(payload))
    assert posts == [] and not truncated


@pytest.mark.parametrize("payload", ["not json", "[]", "{}", '{"errors":[{"message":"private"}]}'])
def test_unknown_shape_and_graphql_errors_fail_closed(payload):
    with pytest.raises(ParseError) as exc:
        parse_posts_feed(payload)
    assert "private" not in str(exc.value)


def test_nested_graphql_errors_fail_even_with_collection():
    payload = feed_payload()
    payload["data"]["errors"] = [{"message": "secret"}]
    with pytest.raises(ParseError):
        parse_posts_feed(json.dumps(payload))


def test_missing_feed_reference_fails_instead_of_empty_success():
    payload = feed_payload()
    payload["included"] = []
    with pytest.raises(ParseError, match="unresolved"):
        parse_posts_feed(json.dumps(payload))


def test_missing_original_is_warning_not_wrong_text():
    payload = feed_payload()
    payload["included"] = payload["included"][1:]
    posts, _, warnings = parse_posts_feed(json.dumps(payload))
    assert posts[1].is_repost is True and posts[1].original_post is None
    assert posts[1].text is None
    assert any("original post" in w for w in warnings)


def test_inline_original_supported_without_recursive_expansion():
    payload = feed_payload()
    original, repost, _ = payload["included"]
    del repost["*resharedUpdate"]
    repost["resharedUpdate"] = original
    original["*resharedUpdate"] = repost["entityUrn"]
    posts, _, _ = parse_posts_feed(json.dumps(payload))
    assert posts[1].original_post.id == "33333"


def test_conflicting_records_fail_closed():
    payload = feed_payload()
    payload["included"].append({**payload["included"][0], "actor": {}})
    with pytest.raises(ParseError, match="conflicting"):
        parse_posts_feed(json.dumps(payload))


def test_unsupported_update_and_unsafe_urls():
    payload = feed_payload()
    first = payload["included"][-1]
    first["actor"]["navigationContext"]["actionTarget"] = "javascript:alert(1)"
    first["content"] = {"imageUrl": {"url": "file:///private"}}
    posts, _, _ = parse_posts_feed(json.dumps(payload))
    assert posts[0].author.url is None and posts[0].media == []
    first["$type"] = "unknown"
    with pytest.raises(ParseError):
        parse_posts_feed(json.dumps(payload))


def test_plain_repost_retains_distinct_outer_and_original_text():
    payload = feed_payload()
    payload["included"][1]["commentary"] = {"text": {"text": "Worth reading"}}
    posts, _, _ = parse_posts_feed(json.dumps(payload))
    assert posts[1].text == "Worth reading"
    assert posts[1].original_post.text == "Original author's text"
