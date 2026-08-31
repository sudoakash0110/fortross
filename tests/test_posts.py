import json
from pathlib import Path

import pytest

from fortross.linkedin.errors import ParseError
from fortross.linkedin.posts import parse_posts_document

FIXTURE = Path(__file__).parent / "fixtures" / "posts.html"


def test_html_posts_and_reposts():
    posts, truncated, warnings = parse_posts_document(FIXTURE.read_text())
    assert len(posts) == 2
    assert not truncated
    assert posts[0].text == "Building a new project."
    assert posts[0].published_at.isoformat() == "2026-08-30T10:00:00+00:00"
    assert posts[0].is_repost is None
    assert posts[0].author.name == "Example Person"
    assert posts[0].media == ["https://media.licdn.com/example.png"]
    assert posts[1].is_repost is True
    assert posts[1].text == "Worth reading."
    assert posts[1].original_post_url.endswith("3333333333333333333/")
    assert warnings


def test_cap_dedup_and_empty_page():
    raw = FIXTURE.read_text()
    posts, truncated, _ = parse_posts_document(raw + raw, limit=1)
    assert len(posts) == 1
    assert truncated
    posts, truncated, _ = parse_posts_document("<main>No posts yet</main>")
    assert posts == []
    assert not truncated


def test_unknown_markup_is_not_reported_as_no_posts():
    with pytest.raises(ParseError):
        parse_posts_document("<html><main>App shell</main></html>")


def test_rsc_posts_with_references_and_cycles():
    element = [
        "$",
        "article",
        None,
        {
            "data-urn": "urn:li:activity:4444444444444444444",
            "children": ["$2", "$3"],
        },
    ]
    body = [
        "$",
        "div",
        None,
        {
            "className": "update-components-text",
            "children": ["Synthetic RSC post"],
        },
    ]
    stream = "1:" + json.dumps(element) + "\n2:" + json.dumps(body) + '\n3:"$3"\n'
    raw = "<script>window.__como_rehydration__ = " + json.dumps([stream]) + ";</script>"
    posts, truncated, _ = parse_posts_document(raw)
    assert len(posts) == 1
    assert posts[0].text == "Synthetic RSC post"
    assert not truncated


@pytest.mark.parametrize("limit", [0, 51])
def test_invalid_limits(limit):
    with pytest.raises(ValueError):
        parse_posts_document(FIXTURE.read_text(), limit)


def test_posts_support_sdui_component_keys_and_property_references():
    card = [
        "$",
        "$Lf",
        None,
        {
            "componentKey": "post-urn:li:activity:5555555555555555555",
            "children": "$2:props:children",
        },
    ]
    contents = [
        "$",
        "div",
        None,
        {
            "children": [
                ["$", "header", None, {"children": "Example Person reposted this"}],
                ["$", "div", None, {"componentKey": "post-commentary", "children": "$3"}],
            ]
        },
    ]
    message = "Synthetic café post\nwith another line."
    stream = (
        "0:"
        + json.dumps(card)
        + "\n2:"
        + json.dumps(contents)
        + "\n"
        + f"3:T{len(message.encode('utf-8')):x},{message}"
    )
    document = "<script>window.__como_rehydration__=" + json.dumps([stream]) + ";</script>"
    posts, _, _ = parse_posts_document(document)
    assert len(posts) == 1
    assert posts[0].text == message
    assert posts[0].is_repost is True


def test_post_decode_failure_reports_only_safe_counts():
    raw = (
        "<main>Private example name</main>"
        '<script>window.__como_rehydration__ = ["0:{}\\n"];</script>'
    )
    with pytest.raises(ParseError) as caught:
        parse_posts_document(raw)
    assert "decoded rows: 1" in str(caught.value)
    assert "activity links: 0" in str(caught.value)
    assert "Private example name" not in str(caught.value)


def test_activity_id_on_action_button_is_not_a_post():
    raw = '<button data-component-key="like-urn:li:activity:5555555555555555555">Like</button>'
    with pytest.raises(ParseError):
        parse_posts_document(raw)


def test_rsc_empty_feed_is_recognized():
    row = ["$", "main", None, {"children": "No posts yet"}]
    raw = (
        "<script>window.__como_rehydration__ = "
        + json.dumps(["0:" + json.dumps(row) + "\n"])
        + ";</script>"
    )
    posts, _, _ = parse_posts_document(raw)
    assert posts == []
