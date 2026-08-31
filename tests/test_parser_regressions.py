import json

import pytest
from pydantic import ValidationError

from fortross.linkedin.flight import FlightDocument, extract_rsc_payload, flight_rows
from fortross.linkedin.parser import _embedded_string, parse_profile_documents
from fortross.models import LinkedInProfile


def page(rows, title="Example Person"):
    stream = "".join(f"{key}:{json.dumps(value)}\n" for key, value in rows.items())
    return (
        f"<title>{title} | LinkedIn</title><script>window.__como_rehydration__ = "
        + json.dumps([stream])
        + ";</script>"
    )


def element(tag, children, **props):
    return ["$", tag, None, {**props, "children": children}]


def test_nested_json_strings_do_not_swallow_neighbor_fields():
    obj = {
        "firstName": "Example",
        "lastName": "Person",
        "vanityName": "example-person",
        "requestMetadata": {"actions": [{"value": "Not a name"}]},
    }
    source = "<script>window.__como_rehydration__ = " + json.dumps([json.dumps(obj)]) + ";</script>"
    assert _embedded_string(source, "firstName") == "Example"
    assert _embedded_string(source, "lastName") == "Person"
    assert _embedded_string(source, "vanityName") == "example-person"


def test_decoded_escapes_and_unicode_are_preserved():
    value = 'Engineer "Platform" \\ Systems ☕'
    source = '<script type="application/json">' + json.dumps({"headline": value}) + "</script>"
    assert _embedded_string(source, "headline") == value


def test_action_metadata_is_not_a_profile_source():
    source = page(
        {
            "0": {
                "actions": [{"firstName": "Wrong", "lastName": "Person"}],
                "onClick": {"headline": "Follow this person"},
            }
        }
    )
    assert _embedded_string(source, "firstName") is None
    assert _embedded_string(source, "headline") is None


def test_footer_and_unrelated_row_order_are_not_profile_fields():
    source = page(
        {
            "0": element("div", [element("h1", ["Example Person"])]),
            "1": element("footer", ["LinkedIn Corporation © 2026", "Visit our Help Center."]),
            "2": {
                "firstName": "Other",
                "lastName": "Person",
                "publicIdentifier": "other-person",
                "headline": "Other person's headline",
            },
        }
    )
    profile = parse_profile_documents("example-person", {"profile": source})
    assert profile.public_identifier == "example-person"
    assert profile.name == "Example Person"
    assert profile.headline is None
    assert profile.location is None
    assert profile.first_name is None


def test_matching_profile_object_wins_over_other_people():
    source = page(
        {
            "0": {"firstName": "Other", "lastName": "Person", "headline": "Wrong headline"},
            "1": {
                "firstName": "Example",
                "lastName": "Person",
                "publicIdentifier": "example-person",
                "headline": 'Engineer "Platforms"',
                "geoLocationName": "Singapore",
            },
        }
    )
    profile = parse_profile_documents("example-person", {"profile": source})
    assert profile.first_name == "Example"
    assert profile.last_name == "Person"
    assert profile.headline == 'Engineer "Platforms"'
    assert profile.location == "Singapore"


def test_structural_header_fields_and_about_section():
    source = page(
        {
            "0": element(
                "main",
                [
                    element(
                        "section",
                        [
                            element("h1", ["Example Person"]),
                            element("p", ["Platform Engineer"], className="text-body-medium"),
                            element(
                                "span",
                                ["Singapore"],
                                className="text-body-small inline t-black--light",
                            ),
                        ],
                        componentKey="profile-topcard",
                    ),
                    element(
                        "section",
                        [element("h2", ["About"]), element("p", ["I build reliable systems."])],
                    ),
                    element("footer", ["Wrong headline", "Wrong location"]),
                ],
            )
        }
    )
    profile = parse_profile_documents("example-person", {"profile": source})
    assert profile.headline == "Platform Engineer"
    assert profile.location == "Singapore"
    assert profile.about == "I build reliable systems."


GROUPED = """<main><section><h2>Experience</h2><ul><li>
<a href="https://www.linkedin.com/company/example/">Example Labs</a><span>Full-time</span>
<ul><li><span>Senior Consultant</span><span>Jan 2025 - Present</span>
<p>- Manage fund implementation, including distributions, loan models,
tax calculations and reporting.</p></li>
<li><span>Consultant</span><span>Jul 2023 - Dec 2024</span>
<span>Teamwork, Portfolio Performance Analysis</span></li></ul></li></ul></section></main>"""


def test_grouped_roles_inherit_company_without_parsing_descriptions_as_locations():
    profile = parse_profile_documents(
        "example-person",
        {
            "profile": "<h1>Example Person</h1>",
            "experience": GROUPED,
        },
    )
    assert len(profile.experience) == 2
    assert [item.title for item in profile.experience] == ["Senior Consultant", "Consultant"]
    assert all(item.company == "Example Labs" for item in profile.experience)
    assert all(item.employment_type == "Full-time" for item in profile.experience)
    assert all(item.company_url.endswith("/company/example/") for item in profile.experience)
    assert all(item.location is None for item in profile.experience)
    assert profile.experience[0].description.startswith("- Manage fund")


def test_skills_filters_are_never_returned_as_skills():
    skills = """<main><section><h1>Skills</h1><ul role="tablist">
    <li>All</li><li>Industry Knowledge</li><li>Tools &amp; Technologies</li></ul>
    <ul><li>Interpersonal Skills</li><li>Other Skills</li></ul>
    <ul><li><span>Python</span><span>20 endorsements</span></li></ul></section></main>"""
    profile = parse_profile_documents(
        "example-person",
        {
            "profile": "<h1>Example Person</h1>",
            "skills": skills,
        },
    )
    assert profile.skills == ["Python"]


def test_no_cross_section_rsc_fallback():
    source = page(
        {
            "0": element(
                "section",
                [
                    element("h2", ["Experience"]),
                    element("ul", [element("li", ["Engineer", "Example Labs", "2023 - Present"])]),
                ],
            )
        }
    )
    profile = parse_profile_documents("example-person", {"profile": source})
    assert len(profile.experience) == 1
    assert profile.skills == []
    assert profile.education == []


def test_role_listitem_rsc_education():
    source = page(
        {
            "0": element(
                "section",
                [
                    element("h2", ["Education"]),
                    element(
                        "div", ["Example University", "B.Tech", "2013 - 2017"], role="listitem"
                    ),
                ],
            )
        }
    )
    profile = parse_profile_documents(
        "example-person",
        {
            "profile": "<h1>Example Person</h1>",
            "education": source,
        },
    )
    assert profile.education[0].school == "Example University"


def test_rsc_text_rows_use_byte_lengths_and_can_contain_newlines():
    content = 'Café\nA quoted "value"'
    payload = (
        f"a:T{len(content.encode('utf-8')):x},{content}" + '\nb:["$","p",null,{"children":"$a"}]\n'
    )
    rows = flight_rows(payload)
    assert rows["a"] == content
    assert rows["b"][1] == "p"


def test_property_references_resolve_props_and_index_paths():
    source = page(
        {
            "0": element("section", ["$2:props:children:0", "$2:props:children:1"]),
            "2": element("div", [element("h1", ["Example Person"]), element("p", ["Engineer"])]),
        }
    )
    doc = FlightDocument(source)
    soup = doc.native_soup()
    assert soup.h1.get_text() == "Example Person"
    assert soup.p.get_text() == "Engineer"


def test_cycles_do_not_recurse_indefinitely():
    doc = FlightDocument(page({"0": element("div", ["$1"]), "1": element("div", ["$0"])}))
    assert len(str(doc.native_soup())) < 1000


def test_truncated_or_invalid_bootstrap_does_not_return_partial_values():
    assert extract_rsc_payload('<script>window.__como_rehydration__ = ["broken</script>') is None
    assert (
        _embedded_string('<script>window.__como_rehydration__ = ["broken</script>', "firstName")
        is None
    )


def test_scalar_contract_blocks_giant_metadata_values():
    with pytest.raises(ValidationError):
        LinkedInProfile(
            source_url="https://www.linkedin.com/in/example/",
            public_identifier='example"},"requestMetadata":{}',
        )
    source = page(
        {
            "0": {
                "publicIdentifier": "example-person",
                "firstName": "Example",
                "lastName": "Person",
                "headline": "x" * 501,
            }
        }
    )
    assert parse_profile_documents("example-person", {"profile": source}).headline is None
