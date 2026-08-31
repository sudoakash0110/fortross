import html
import json
from urllib.parse import parse_qs, urlsplit

import pytest

from fortross.linkedin.errors import ParseError
from fortross.linkedin.voyager import first_feed_path, profile_urn_from_bootstrap


def bootstrap(*records):
    payload = html.escape(json.dumps({"included": list(records)}))
    return f'<code id="bpr-guid-123">{payload}</code>'


TARGET = {"publicIdentifier": "example", "entityUrn": "urn:li:fsd_profile:synthetic-target"}


def test_bootstrap_matches_target_not_viewer():
    viewer = {"publicIdentifier": "viewer", "entityUrn": "urn:li:fsd_profile:synthetic-viewer"}
    assert profile_urn_from_bootstrap(bootstrap(viewer, TARGET), "EXAMPLE") == TARGET["entityUrn"]


def test_duplicate_same_profile_is_not_ambiguous():
    assert profile_urn_from_bootstrap(bootstrap(TARGET, TARGET), "example") == TARGET["entityUrn"]


@pytest.mark.parametrize(
    "document",
    [
        "<html></html>",
        bootstrap(TARGET),
        '<code id="bpr-guid-1">broken JSON</code>',
        '<code id="bpr-guid-1">[]</code>',
    ],
)
def test_missing_target_fails_closed(document):
    with pytest.raises(ParseError, match="no feed request"):
        profile_urn_from_bootstrap(document, "missing")


def test_conflicting_profile_ids_fail_closed():
    other = {**TARGET, "entityUrn": "urn:li:fsd_profile:other"}
    with pytest.raises(ParseError):
        profile_urn_from_bootstrap(bootstrap(TARGET, other), "example")


def test_initial_feed_query_has_no_cursor():
    path = first_feed_path(TARGET["entityUrn"], 5)
    query = parse_qs(urlsplit(path).query)
    assert urlsplit(path).path == "/voyager/api/graphql"
    assert query["variables"] == [
        "(count:5,start:0,profileUrn:urn:li:fsd_profile:synthetic-target)"
    ]
    assert "paginationToken" not in path


@pytest.mark.parametrize("count", [0, 51, True, 1.5])
def test_invalid_count(count):
    with pytest.raises(ValueError):
        first_feed_path(TARGET["entityUrn"], count)


def test_urn_cannot_inject_query_parameters():
    with pytest.raises(ValueError):
        first_feed_path("urn:li:fsd_profile:x)&other=y", 5)
