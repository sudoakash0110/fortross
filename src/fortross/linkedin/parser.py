import re
from datetime import date
from typing import Any

from bs4 import BeautifulSoup, NavigableString, Tag

from fortross.linkedin.flight import FlightDocument, extract_rsc_payload
from fortross.models import (
    Certification,
    DateRange,
    Education,
    Experience,
    Language,
    LinkedInProfile,
    ProfileImages,
)

__all__ = ["extract_rsc_payload", "parse_profile_documents"]
_DATE_RANGE = re.compile(
    r"(?P<start>(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)?\s*\d{4})"
    r"\s*[-–]\s*(?P<end>Present|(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)?\s*\d{4})",
    re.I,
)
_MONTHS = {
    name: index
    for index, name in enumerate(
        ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1
    )
}
_FILTERS = {
    "all",
    "industry knowledge",
    "tools & technologies",
    "interpersonal skills",
    "other skills",
}
_UI = {
    "resources",
    "contact info",
    "connect",
    "follow",
    "message",
    "more",
    "open to",
    "show more",
    "show less",
    "see more",
    "see less",
    "add profile section",
}
_EMPLOYMENT = {
    "full-time",
    "part-time",
    "self-employed",
    "freelance",
    "contract",
    "internship",
    "apprenticeship",
    "seasonal",
}


def _clean(value: str | None) -> str | None:
    return re.sub(r"\s+", " ", value).strip() or None if value else None


def _safe(value: Any, maximum: int = 500) -> str | None:
    if not isinstance(value, str):
        return None
    value = _clean(value)
    if not value or len(value) > maximum or value.startswith("$"):
        return None
    if any(
        marker in value for marker in ('"$type"', '"requestMetadata"', '"children":', "proto.sdui.")
    ):
        return None
    return value


def _embedded_string(source: str, *keys: str) -> str | None:
    """Compatibility helper: decode JSON first, never regex escaped field values."""
    document = FlightDocument(source)
    objects = list(document.objects())
    for key in keys:
        for obj in objects:
            if value := _safe(document.resolve(obj.get(key))):
                return value
    return None


class ProfileDocument:
    def __init__(self, source: str) -> None:
        self.flight = FlightDocument(source)
        self.views = [self.flight.html, self.flight.native_soup()]
        self.objects = list(self.flight.objects())
        for view in self.views:
            for tag in view.select("script, style, nav, footer, [role=tablist], [role=menu]"):
                tag.decompose()


def _parse_date(value: str) -> date | None:
    parts = value.strip().split()
    try:
        return date(int(parts[-1]), _MONTHS[parts[0][:3].lower()] if len(parts) > 1 else 1, 1)
    except (KeyError, ValueError, IndexError):
        return None


def _date_range(text: str) -> DateRange:
    match = _DATE_RANGE.search(text)
    if not match:
        return DateRange()
    current = match["end"].lower() == "present"
    return DateRange(
        start=_parse_date(match["start"]),
        end=None if current else _parse_date(match["end"]),
        current=current,
        display=match[0],
    )


def _section(view: BeautifulSoup, names: tuple[str, ...]) -> Tag | None:
    wanted = {name.casefold() for name in names}
    for tag in view.select("section[aria-label], [role=region][aria-label]"):
        if str(tag.get("aria-label", "")).casefold() in wanted:
            return tag
    for heading in view.select("h1, h2, h3, [role=heading]"):
        if heading.get_text(" ", strip=True).casefold() not in wanted:
            continue
        for parent in heading.parents:
            if not isinstance(parent, Tag):
                continue
            headings = parent.select("h1, h2, h3, [role=heading]")
            # Do not expand into another peer section just to find list items.
            if any(h is not heading and h.name == heading.name for h in headings):
                if parent.name in {"main", "body", "html"}:
                    break
            if parent.name == "section" or parent.select_one("li, [role=listitem]"):
                return parent
    return None


def _lines(item: Tag) -> list[str]:
    lines: list[str] = []
    for node in item.descendants:
        if not isinstance(node, NavigableString):
            continue
        skip = False
        for ancestor in node.parents:
            if ancestor is item:
                break
            if ancestor.name in {"button", "script", "style", "li"} or ancestor.get("role") in {
                "tab",
                "listitem",
            }:
                skip = True
                break
        value = _clean(str(node))
        if not skip and value and value.casefold() not in _UI and value not in lines:
            lines.append(value)
    return lines


def _items(doc: ProfileDocument, names: tuple[str, ...], detail: bool) -> list[Tag]:
    result: list[Tag] = []
    seen: set[tuple[str, ...]] = set()
    for view in doc.views:
        section = _section(view, names)
        if section is None and detail and not view.select("h1, h2, h3, [role=heading]"):
            section = view.main
        if section is None:
            continue
        for tag in section.select("li, [role=listitem]"):
            # Group headers are handled via ancestry, not returned as extra records.
            if tag.select_one("li, [role=listitem]"):
                continue
            lines = _lines(tag)
            if not lines or lines[0].casefold() in _FILTERS:
                continue
            if tag.find_parent(["nav", "footer"]) or tag.get("role") == "tab":
                continue
            fingerprint = tuple(lines)
            if fingerprint not in seen:
                seen.add(fingerprint)
                result.append(tag)
    return result


def _link(item: Tag, marker: str) -> Tag | None:
    return next((tag for tag in item.find_all("a", href=True) if marker in str(tag["href"])), None)


def _company(
    item: Tag, lines: list[str], date_index: int
) -> tuple[str | None, str | None, str | None]:
    link = _link(item, "/company/")
    company = None
    employment = None
    # Prefer the company group outside the nested role list.
    group = item.find_parent("li") or item.find_parent(attrs={"role": "listitem"})
    if group is not None:
        group_lines = _lines(group)
        if group_lines:
            company = _safe(group_lines[0], 200)
            employment = next(
                (line for line in group_lines if line.casefold() in _EMPLOYMENT), None
            )
        link = _link(group, "/company/") or link
    if company is None and date_index > 1:
        candidate = lines[1]
        parts = [part.strip() for part in candidate.split("·")]
        if parts[0].casefold() not in _EMPLOYMENT:
            company = _safe(parts[0], 200)
        employment = next((part for part in parts if part.casefold() in _EMPLOYMENT), None)
    return company, str(link["href"]) if link else None, employment


def _experiences(doc: ProfileDocument, detail: bool) -> list[Experience]:
    records = []
    for item in _items(doc, ("Experience",), detail):
        lines = _lines(item)
        date_index = next((i for i, line in enumerate(lines) if _DATE_RANGE.search(line)), None)
        if date_index is None or date_index == 0 or not _safe(lines[0], 200):
            continue
        company, company_url, employment = _company(item, lines, date_index)
        location_tag = item.select_one(
            ".pv-entity__location, .experience-item__location, [data-field=location]"
        )
        location = _safe(location_tag.get_text(" ", strip=True), 150) if location_tag else None
        # Never classify arbitrary comma-containing prose as a location.
        description_lines = [
            line
            for line in lines[date_index + 1 :]
            if line != location and (len(line) > 80 or line.startswith(("- ", "• ")))
        ]
        records.append(
            Experience(
                title=lines[0],
                company=company,
                company_url=company_url,
                employment_type=employment,
                date_range=_date_range(lines[date_index]),
                location=location,
                description="\n".join(description_lines) or None,
            )
        )
    return records


def _education(doc: ProfileDocument, detail: bool) -> list[Education]:
    records = []
    for item in _items(doc, ("Education",), detail):
        lines = _lines(item)
        if not _safe(lines[0], 200):
            continue
        link = _link(item, "/school/") or _link(item, "/company/")
        date_line = next((line for line in lines if _DATE_RANGE.search(line)), "")
        records.append(
            Education(
                school=lines[0],
                degree=lines[1] if len(lines) > 1 and lines[1] != date_line else None,
                school_url=str(link["href"]) if link else None,
                date_range=_date_range(date_line),
            )
        )
    return records


def _skills(doc: ProfileDocument, detail: bool) -> list[str]:
    values = []
    for item in _items(doc, ("Skills",), detail):
        value = _safe(_lines(item)[0], 200)
        if (
            value
            and not re.search(r"endorse|^add |^edit |^show all", value, re.I)
            and value not in values
        ):
            values.append(value)
    return values


def _certifications(doc: ProfileDocument, detail: bool) -> list[Certification]:
    records = []
    for item in _items(doc, ("Licenses & certifications", "Licenses and certifications"), detail):
        lines = _lines(item)
        if not _safe(lines[0], 250):
            continue
        link = item.find("a", string=re.compile("Show credential", re.I), href=True)
        records.append(
            Certification(
                name=lines[0],
                issuer=lines[1] if len(lines) > 1 else None,
                issued=next((line for line in lines if line.lower().startswith("issued")), None),
                expires=next((line for line in lines if "expires" in line.lower()), None),
                credential_id=next(
                    (line for line in lines if "credential id" in line.lower()), None
                ),
                credential_url=str(link["href"]) if link else None,
            )
        )
    return records


def _languages(doc: ProfileDocument, detail: bool) -> list[Language]:
    return [
        Language(name=lines[0], proficiency=lines[1] if len(lines) > 1 else None)
        for item in _items(doc, ("Languages",), detail)
        if (lines := _lines(item)) and _safe(lines[0], 100)
    ]


def _name(doc: ProfileDocument) -> str | None:
    for view in doc.views:
        heading = view.find("h1")
        if heading and (value := _safe(heading.get_text(" ", strip=True), 200)):
            return value
    title = doc.views[0].title
    if title:
        value = re.sub(r"\s*\|\s*LinkedIn.*$", "", title.get_text(" ", strip=True), flags=re.I)
        value = re.sub(r"^\(\d+\)\s*", "", value)
        if value.casefold() not in {"linkedin", "sign in", "login", "feed"}:
            return _safe(value, 200)
    return None


def _profile_object(doc: ProfileDocument, slug: str, name: str | None) -> dict[str, Any]:
    for obj in doc.objects:
        identity = doc.flight.resolve(obj.get("publicIdentifier", obj.get("vanityName")))
        if identity is not None and identity != slug:
            continue
        full_name = " ".join(
            str(doc.flight.resolve(obj.get(key, "")) or "") for key in ("firstName", "lastName")
        ).strip()
        if identity == slug or (name and full_name == name):
            if any(key in obj for key in ("firstName", "headline", "summary", "geoLocationName")):
                return obj
    return {}


def _header(doc: ProfileDocument) -> Tag | None:
    for view in doc.views:
        explicit = view.select_one(
            ".pv-top-card, [data-view-name*=profile-topcard], [data-component-key*=topcard], "
            "[data-component-key*=topCard], [data-component-key*=top-card]"
        )
        if explicit:
            return explicit
        h1 = view.find("h1")
        if h1:
            return h1.parent
    return None


def _header_values(header: Tag | None, name: str | None) -> tuple[str | None, str | None]:
    if header is None:
        return None, None
    headline = header.select_one(
        ".text-body-medium, .pv-text-details__left-panel .text-body-medium"
    )
    location = header.select_one(".text-body-small.inline.t-black--light, [data-field=location]")
    headline_text = _safe(headline.get_text(" ", strip=True)) if headline else None
    location_text = _safe(location.get_text(" ", strip=True), 200) if location else None
    # Only direct siblings of the actual name heading, never global Flight row ordering.
    heading = header.find("h1", recursive=False)
    if heading and heading.get_text(" ", strip=True) == name:
        siblings = []
        for sibling in heading.next_siblings:
            if not isinstance(sibling, Tag):
                continue
            if sibling.name not in {"div", "p", "span"} or sibling.select_one("button, ul, a, h2"):
                break
            value = _safe(sibling.get_text(" ", strip=True), 200)
            if value and value.casefold() not in _UI:
                siblings.append(value)
            if len(siblings) == 2:
                break
        if siblings:
            headline_text = headline_text or siblings[0]
        # Location must be explicit; generic sibling text is not reliable geography.
    return headline_text, location_text


def parse_profile_documents(
    slug: str,
    documents: dict[str, str],
    include_sections: bool = True,
    warnings: list[str] | None = None,
) -> LinkedInProfile:
    docs = {key: ProfileDocument(value) for key, value in documents.items()}
    base = docs["profile"]
    name = _name(base)
    obj = _profile_object(base, slug, name)

    def scalar(*keys: str, maximum: int = 500) -> str | None:
        return next(
            (value for key in keys if (value := _safe(base.flight.resolve(obj.get(key)), maximum))),
            None,
        )

    first, last = scalar("firstName", maximum=100), scalar("lastName", maximum=100)
    if not name:
        name = " ".join(value for value in (first, last) if value) or None
    if name and first and last and f"{first} {last}" != name:
        first = last = None
    header = _header(base)
    headline, location = _header_values(header, name)
    about = scalar("summary", "about", maximum=10000)
    if not about:
        for view in base.views:
            section = _section(view, ("About",))
            if section is not None:
                about = _safe(
                    " ".join(line for line in _lines(section) if line.casefold() != "about"), 10000
                )
                if about:
                    break
    image_urls = [str(img.get("src", "")) for img in header.find_all("img")] if header else []
    if picture := scalar("picture", maximum=2000):
        image_urls.append(picture)
    images = ProfileImages(
        profile=next((url for url in image_urls if "profile-displayphoto" in url), None),
        background=next((url for url in image_urls if "profile-background" in url), None),
    )
    parsers = {
        "experience": _experiences,
        "education": _education,
        "skills": _skills,
        "certifications": _certifications,
        "languages": _languages,
    }
    sections = {
        key: parser(docs.get(key, base), key in docs) if include_sections else []
        for key, parser in parsers.items()
    }
    if warnings is not None and any(doc.flight.truncated for doc in docs.values()):
        warnings.append("RSC traversal reached its safety bound; results may be incomplete")
    return LinkedInProfile(
        source_url=f"https://www.linkedin.com/in/{slug}/",
        public_identifier=slug,
        name=name,
        first_name=first,
        last_name=last,
        headline=scalar("headline") or headline,
        location=scalar("geoLocationName", "locationName", maximum=200) or location,
        about=about,
        images=images,
        **sections,
    )
