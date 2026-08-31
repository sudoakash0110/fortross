"""Offline configuration check. Never contacts LinkedIn or the database."""

import argparse
import json

from pydantic import ValidationError

from fortross.settings import get_settings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--deployment", action="store_true", help="Check production live requirements"
    )
    args = parser.parse_args()
    try:
        settings = get_settings()
        if args.deployment:
            settings = settings.model_copy(update={"app_env": "production"})
    except (ValueError, ValidationError):
        # Validation error objects can retain input values; never serialize them.
        print(
            json.dumps(
                {
                    "configuration_valid": False,
                    "error": "Invalid server settings. Check README setup requirements.",
                }
            )
        )
        return 1
    issue = None
    try:
        settings.validate_server_configuration()
    except ValueError as exc:
        issue = str(exc)  # These messages contain field names only, not their values.
    result = {
        "configuration_valid": True,
        "live_enabled": settings.linkedin_live_enabled,
        "authentication_mode": "caller_cookie_sessions",
        "server_linkedin_credentials_loaded": bool(
            settings.linkedin_li_at or settings.linkedin_jsessionid
        ),
        "login_requires_api_key": False,
        "profile_access": settings.linkedin_profile_access,
        "allowlist_count": len(settings.allowed_profile_slugs),
        "persistent_safety_configured": bool(settings.turso_database_url),
        "safety_backend": (
            "turso"
            if settings.turso_database_url
            else "sqlite"
            if settings.safety_state_file
            else "memory"
        ),
        "state_warning": (
            "Local state can be lost on host restart, redeploy, or idle spin-down"
            if settings.safety_state_file and not settings.turso_database_url
            else "In-memory safety state resets on process restart"
            if not settings.turso_database_url
            else None
        ),
        "live_configuration_ready": issue is None,
        "issue": issue,
        "network_requests_made": 0,
        "note": "Each caller supplies their own cookie. No network requests were made.",
    }
    print(json.dumps(result, indent=2))
    return 1 if issue else 0


if __name__ == "__main__":
    raise SystemExit(main())
