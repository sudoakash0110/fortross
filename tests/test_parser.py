import json
from pathlib import Path

import pytest
from bs4 import BeautifulSoup, NavigableString

from fortross.linkedin.parser import extract_rsc_payload, parse_profile_documents

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.mark.parametrize("attribute", ["componentkey", "data-component-key"])
def test_sdui_experience_paragraph_heading_grouped_and_standalone_roles(attribute):
    source = (FIXTURES / "experience_sdui.html").read_text().replace("componentkey", attribute)
    profile = parse_profile_documents(
        "example-person", {"profile": "<h1>Example Person</h1>", "experience": source}
    )
    assert [role.title for role in profile.experience] == [
        "Senior Engineer",
        "Engineer",
        "Research Assistant",
    ]
    senior, engineer, intern = profile.experience
    assert senior.company == engineer.company == "Example Labs"
    assert senior.employment_type == engineer.employment_type == "Permanent"
    assert senior.location == engineer.location == "Paris, France · Hybrid"
    assert senior.date_range.current is True
    assert engineer.date_range.end.isoformat() == "2023-12-01"
    assert senior.description == "Built reliable systems.\nMentored engineers."
    assert engineer.description is None
    assert intern.company == "Second Labs"
    assert intern.company_url.endswith("/second-labs/")
    assert intern.employment_type == "Internship"
    assert intern.location == "Luxembourg"
    assert intern.description == "Analyzed data, improved reports, and documented results."
    assert profile.skills == []
    assert profile.education == []


def test_unrelated_component_with_experience_in_its_name_is_not_a_section():
    source = (
        (FIXTURES / "experience_sdui.html")
        .read_text()
        .replace(
            "com.linkedin.sdui.profile.card.refSyntheticExperienceDetailsSection",
            "unrelatedExperienceDetailsSection",
        )
    )
    profile = parse_profile_documents(
        "example-person", {"profile": "<h1>Example Person</h1>", "experience": source}
    )
    assert profile.experience == []


@pytest.mark.parametrize("attribute", ["componentkey", "data-component-key"])
def test_sdui_topcard_uses_h2_identity_and_structural_fields(attribute):
    source = (FIXTURES / "profile_sdui_topcard.html").read_text().replace("componentkey", attribute)
    profile = parse_profile_documents("example-person", {"profile": source}, False)
    assert profile.name == "Example Person"
    assert profile.headline == "Platform Engineer | Storage & Distributed Systems"
    assert profile.location == "Singapore"
    assert profile.images.profile.endswith("/photo.jpg")
    assert profile.images.background.endswith("/cover.jpg")
    assert profile.first_name is None  # Don't infer name parts from display text.
    assert profile.about is None
    assert not profile.experience


def test_sdui_missing_location_does_not_return_company_or_badges():
    source = (FIXTURES / "profile_sdui_topcard.html").read_text().replace("<p>Singapore</p>", "")
    profile = parse_profile_documents("example-person", {"profile": source})
    assert profile.headline == "Platform Engineer | Storage & Distributed Systems"
    assert profile.location is None


def test_sdui_missing_headline_does_not_return_company_or_pronouns():
    source = (
        (FIXTURES / "profile_sdui_topcard.html")
        .read_text()
        .replace("<p>Platform Engineer | Storage &amp; Distributed Systems</p>", "")
    )
    profile = parse_profile_documents("example-person", {"profile": source})
    # Without an explicit headline slot this variant is ambiguous; don't invent one.
    assert profile.headline is None
    assert profile.location == "Singapore"


def test_sdui_topcard_also_parses_when_only_present_in_flight_rows():
    soup = BeautifulSoup((FIXTURES / "profile_sdui_topcard.html").read_text(), "html.parser")

    def element(node):
        if isinstance(node, NavigableString):
            return str(node)
        props = dict(node.attrs)
        if "class" in props:
            props["className"] = " ".join(props.pop("class"))
        if "componentkey" in props:
            props["componentKey"] = props.pop("componentkey")
        props["children"] = [element(child) for child in node.children]
        return ["$", node.name, None, props]

    payload = "0:" + json.dumps(element(soup.body)) + "\n"
    source = "<script>window.__como_rehydration__ = " + json.dumps([payload]) + ";</script>"
    profile = parse_profile_documents("example-person", {"profile": source})
    assert profile.name == "Example Person"
    assert profile.headline == "Platform Engineer | Storage & Distributed Systems"
    assert profile.location == "Singapore"
    assert profile.images.background.endswith("/cover.jpg")


def test_parses_profile_and_sections() -> None:
    base = (FIXTURES / "profile.html").read_text()
    skills = (FIXTURES / "skills.html").read_text()
    profile = parse_profile_documents(
        "example-person",
        {"profile": base, "experience": base, "education": base, "skills": skills},
    )

    assert profile.name == "Example Person"
    assert profile.first_name == "Example"
    assert profile.headline == "Staff Engineer | Platforms"
    assert profile.location == "Singapore"
    assert profile.about == "I build reliable systems."
    assert profile.images.profile and "profile-displayphoto" in profile.images.profile
    assert profile.experience[0].title == "Staff Engineer"
    assert profile.experience[0].date_range.current is True
    assert profile.education[0].school == "Example University"
    assert profile.skills == ["Python", "Distributed Systems"]


def test_parses_current_rsc_hydration_format() -> None:
    base = (FIXTURES / "profile_rsc.html").read_text()
    experience = (FIXTURES / "experience_rsc.html").read_text()
    skills = (FIXTURES / "skills_rsc.html").read_text()

    assert extract_rsc_payload(base)
    profile = parse_profile_documents(
        "example-person",
        {"profile": base, "experience": experience, "skills": skills},
    )

    assert profile.name == "Example Person"
    assert profile.headline == "Staff Engineer | Platforms"
    # This fixture has only an unlabelled text sibling, not a location field.
    assert profile.location is None
    assert profile.experience[0].title == "Staff Engineer"
    assert profile.experience[0].company == "Example Labs"
    assert profile.experience[0].date_range.current is True
    assert profile.skills == ["Python", "Distributed Systems"]
