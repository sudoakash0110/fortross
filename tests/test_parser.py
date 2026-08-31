from pathlib import Path

from fortross.linkedin.parser import extract_rsc_payload, parse_profile_documents

FIXTURES = Path(__file__).parent / "fixtures"


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
