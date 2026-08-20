from datetime import datetime, timedelta, timezone

from multiagent.credentials import expires_at, parse_env_file, resolve, status
from multiagent.types import Backend

AWS = Backend(name="aws-gov", type="bedrock", credential="aws-gov-keys", region="us-gov-west-1")


def write_keys(tmp_path, expires=None):
    comment = f"# expires: {expires}\n" if expires else ""
    (tmp_path / "aws-gov-keys.env").write_text(
        f"{comment}AWS_ACCESS_KEY_ID=AKIA\nAWS_SECRET_ACCESS_KEY=shhh\n"
    )


def stamp(delta):
    return (datetime.now(timezone.utc) + delta).isoformat()


def test_parse_basic_and_edge_cases():
    text = """
# a comment
GEMINI_API_KEY=abc123

  ANTHROPIC_API_KEY = sk-ant-xyz
# BLANKED=nope
URL=https://host/path?a=1&b=2
EMPTY=
"""
    assert parse_env_file(text) == {
        "GEMINI_API_KEY": "abc123",
        "ANTHROPIC_API_KEY": "sk-ant-xyz",
        "URL": "https://host/path?a=1&b=2",
        "EMPTY": "",
    }


def test_parse_ignores_lines_without_equals():
    assert parse_env_file("junk\nK=v\n") == {"K": "v"}


def test_parse_empty_text():
    assert parse_env_file("") == {}


def test_resolve_reads_named_file(tmp_path):
    (tmp_path / "gemini-api-key.env").write_text("GEMINI_API_KEY=abc\n")
    assert resolve("gemini-api-key", tmp_path) == {"GEMINI_API_KEY": "abc"}


def test_resolve_missing_returns_none(tmp_path):
    assert resolve("nope", tmp_path) is None


def test_status_none_when_backend_needs_no_credential(tmp_path):
    assert status(Backend(name="home-ollama", type="ollama"), tmp_path) == "none"


def test_status_ok_and_missing(tmp_path):
    backend = Backend(name="gemini", type="gemini", credential="gemini-api-key")
    assert status(backend, tmp_path) == "missing"
    (tmp_path / "gemini-api-key.env").write_text("GEMINI_API_KEY=abc\n")
    assert status(backend, tmp_path) == "ok"


# --- expiry ---------------------------------------------------------------


def test_status_stale_when_expiry_has_passed(tmp_path):
    write_keys(tmp_path, stamp(timedelta(hours=-1)))
    assert status(AWS, tmp_path) == "stale"


def test_status_ok_while_expiry_is_ahead(tmp_path):
    write_keys(tmp_path, stamp(timedelta(hours=8)))
    assert status(AWS, tmp_path) == "ok"


def test_status_ok_without_an_expires_comment(tmp_path):
    write_keys(tmp_path)
    assert status(AWS, tmp_path) == "ok"


def test_expiry_parses_z_suffix_and_offsets(tmp_path):
    assert expires_at("# expires: 2026-08-20T12:00:00Z\nK=v\n") == datetime(
        2026, 8, 20, 12, tzinfo=timezone.utc
    )
    # Same instant, written as an offset: 08:00 in UTC-4.
    assert expires_at("# expires: 2026-08-20T08:00:00-04:00\n") == datetime(
        2026, 8, 20, 12, tzinfo=timezone.utc
    )
    # No offset at all is read as UTC, not as local time.
    assert expires_at("# expires: 2026-08-20T12:00:00\n") == datetime(
        2026, 8, 20, 12, tzinfo=timezone.utc
    )


def test_expiry_absent_or_unreadable_is_none(tmp_path):
    assert expires_at("K=v\n# a comment\n") is None
    assert expires_at("# expires: sometime tuesday\n") is None


def test_z_suffixed_past_expiry_is_stale(tmp_path):
    write_keys(tmp_path, "2020-01-01T00:00:00Z")
    assert status(AWS, tmp_path) == "stale"


def test_expired_credential_still_resolves(tmp_path):
    # Staleness is a warning, not a deletion: the values are still readable.
    write_keys(tmp_path, "2020-01-01T00:00:00Z")
    assert resolve("aws-gov-keys", tmp_path)["AWS_ACCESS_KEY_ID"] == "AKIA"
