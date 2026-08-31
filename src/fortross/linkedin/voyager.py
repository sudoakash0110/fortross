"""Observed Voyager bootstrap contract. No network access or script execution."""

import json
import re
from urllib.parse import quote

from bs4 import BeautifulSoup

from fortross.linkedin.errors import ParseError

PROFILE_UPDATES_QUERY_ID = "voyagerFeedDashProfileUpdates.20c70fe0314184158516a7ec004c0408"
_PROFILE_URN = re.compile(r"urn:li:fsd_profile:[A-Za-z0-9_-]+")


def has_voyager_bootstrap(document: str) -> bool:
    return BeautifulSoup(document, "html.parser").select_one('code[id^="bpr-guid-"]') is not None


def profile_urn_from_bootstrap(document: str, slug: str) -> str:
    """Select only the requested member, never the signed-in viewer's /me record."""
    soup = BeautifulSoup(document, "html.parser")
    matches: set[str] = set()
    for block in soup.select('code[id^="bpr-guid-"]'):
        try:
            payload = json.loads(block.get_text())
        except (ValueError, RecursionError):
            continue
        if not isinstance(payload, dict):
            continue
        included = payload.get("included")
        if not isinstance(included, list):
            continue
        for record in included:
            if not isinstance(record, dict):
                continue
            identifier = record.get("publicIdentifier")
            urn = record.get("entityUrn")
            if (
                isinstance(identifier, str)
                and identifier.casefold() == slug.casefold()
                and isinstance(urn, str)
                and _PROFILE_URN.fullmatch(urn)
            ):
                matches.add(urn)
    if len(matches) != 1:
        raise ParseError(
            "Activity bootstrap did not identify one matching profile; no feed request made"
        )
    return matches.pop()


def first_feed_path(profile_urn: str, count: int, query_id: str = PROFILE_UPDATES_QUERY_ID) -> str:
    """One initial member-share-feed page, using the query observed in public JS.

    The query ID is versioned and may expire. Never probe alternatives automatically.
    Percent-encode the URN value inside the Rest.li variables expression.
    """
    if not _PROFILE_URN.fullmatch(profile_urn):
        raise ValueError("Invalid profile URN")
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 50:
        raise ValueError("Feed count must be between 1 and 50")
    if not re.fullmatch(r"voyagerFeedDashProfileUpdates\.[a-f0-9]{32}", query_id):
        raise ValueError("Invalid profile updates query ID")
    return (
        "/voyager/api/graphql?includeWebMetadata=true"
        f"&variables=(count:{count},start:0,profileUrn:{quote(profile_urn, safe='')})"
        f"&queryId={query_id}"
    )
