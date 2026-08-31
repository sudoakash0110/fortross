class LinkedInError(Exception):
    code = "linkedin_error"
    status_code = 502


class LinkedInDisabledError(LinkedInError):
    code = "linkedin_live_disabled"
    status_code = 503


class ProfileNotAllowedError(LinkedInError):
    code = "profile_not_allowed"
    status_code = 403


class LinkedInAuthError(LinkedInError):
    code = "linkedin_auth_failed"
    status_code = 503


class LinkedInChallengeError(LinkedInError):
    code = "linkedin_challenge_detected"
    status_code = 503


class LinkedInRateLimitError(LinkedInError):
    code = "linkedin_rate_limited"
    status_code = 503


class CircuitOpenError(LinkedInError):
    code = "upstream_circuit_open"
    status_code = 503


class ProfileNotFoundError(LinkedInError):
    code = "profile_not_found"
    status_code = 404


class ParseError(LinkedInError):
    code = "profile_parse_failed"
    status_code = 502


class LinkedInUpstreamError(LinkedInError):
    code = "linkedin_upstream_failed"
    status_code = 502


class LinkedInSafetyError(LinkedInError):
    code = "linkedin_safety_limit"
    status_code = 503


class LinkedInConfigurationError(LinkedInError):
    code = "linkedin_configuration_incomplete"
    status_code = 503


class SessionAuthError(LinkedInError):
    code = "invalid_session"
    status_code = 401


class SessionCapacityError(LinkedInError):
    code = "session_capacity_reached"
    status_code = 503
