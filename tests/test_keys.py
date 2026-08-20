"""`ma keys`: what the AWS CLI says, what lands in the file, what never prints."""
import pytest

from multiagent import cli, keys
from multiagent.types import ConfigError

ACCESS = "AKIAEXAMPLE0000"
SECRET = "wJalrXUtnFEMI-DO-NOT-PRINT"  # noqa: S105 - fake value, test fixture
TOKEN = "FQoGZXIvYXdzE-session-token"  # noqa: S105 - fake value, test fixture
EXPIRY = "2026-08-20T21:34:56+00:00"

SHORT_TERM = {
    "Version": 1,
    "AccessKeyId": ACCESS,
    "SecretAccessKey": SECRET,
    "SessionToken": TOKEN,
    "Expiration": EXPIRY,
}
LONG_TERM = {"Version": 1, "AccessKeyId": ACCESS, "SecretAccessKey": SECRET}


class _Completed:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def stub_aws(monkeypatch, *, result=None, raises=None, calls=None):
    """Stand in for the `aws` binary, recording the argv it was invoked with."""
    import json

    completed = result if result is not None else _Completed(stdout=json.dumps(SHORT_TERM))

    def fake_run(argv, *args, **kwargs):
        if calls is not None:
            calls.append(list(argv))
        if raises is not None:
            raise raises
        return completed

    monkeypatch.setattr(keys.subprocess, "run", fake_run)


# --- fetch ----------------------------------------------------------------


def test_fetch_asks_the_cli_for_the_named_profile(monkeypatch):
    calls = []
    stub_aws(monkeypatch, calls=calls)
    assert keys.fetch("aws-gov")["SessionToken"] == TOKEN
    assert calls == [
        ["aws", "configure", "export-credentials",
         "--profile", "aws-gov", "--format", "process"]
    ]


def test_fetch_failure_carries_the_stderr(monkeypatch):
    stub_aws(
        monkeypatch,
        result=_Completed(255, stderr="The config profile (aws-gov) could not be found\n"),
    )
    with pytest.raises(ConfigError) as exc:
        keys.fetch("aws-gov")
    assert "could not be found" in str(exc.value)
    assert "aws-gov" in str(exc.value)


def test_fetch_without_the_aws_binary_explains_itself(monkeypatch):
    stub_aws(monkeypatch, raises=FileNotFoundError(2, "No such file or directory", "aws"))
    with pytest.raises(ConfigError) as exc:
        keys.fetch("aws-gov")
    assert "install awscli" in str(exc.value)


def test_fetch_rejects_unusable_output(monkeypatch):
    stub_aws(monkeypatch, result=_Completed(stdout="not json at all"))
    with pytest.raises(ConfigError):
        keys.fetch("aws-gov")

    import json

    stub_aws(monkeypatch, result=_Completed(stdout=json.dumps({"Version": 1})))
    with pytest.raises(ConfigError) as exc:
        keys.fetch("aws-gov")
    assert "AccessKeyId" in str(exc.value)


# --- write_credential -----------------------------------------------------


def test_write_short_term_credential(tmp_path):
    path = keys.write_credential("aws-gov", SHORT_TERM, tmp_path)
    assert path == tmp_path / "aws-gov.env"
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.read_text().splitlines() == [
        f"# expires: {EXPIRY}",
        f"AWS_ACCESS_KEY_ID={ACCESS}",
        f"AWS_SECRET_ACCESS_KEY={SECRET}",
        f"AWS_SESSION_TOKEN={TOKEN}",
    ]


def test_write_long_term_credential_has_no_expiry_or_session_token(tmp_path):
    path = keys.write_credential("aws-home", LONG_TERM, tmp_path)
    assert path.read_text().splitlines() == [
        f"AWS_ACCESS_KEY_ID={ACCESS}",
        f"AWS_SECRET_ACCESS_KEY={SECRET}",
    ]


def test_write_creates_the_directory_and_tightens_a_loose_file(tmp_path):
    cred_dir = tmp_path / "creds" / "nested"
    path = keys.write_credential("aws-gov", LONG_TERM, cred_dir)
    assert path.exists()

    path.chmod(0o644)  # e.g. a file some other tool left behind
    keys.write_credential("aws-gov", SHORT_TERM, cred_dir)
    assert path.stat().st_mode & 0o777 == 0o600


def test_refresh_replaces_rather_than_appends(tmp_path):
    keys.write_credential("aws-gov", SHORT_TERM, tmp_path)
    path = keys.write_credential("aws-gov", LONG_TERM, tmp_path)
    assert "# expires:" not in path.read_text()
    assert path.read_text().count("AWS_ACCESS_KEY_ID=") == 1


def test_no_code_path_prints_a_secret(monkeypatch, tmp_path, capsys):
    stub_aws(monkeypatch)
    creds = keys.fetch("aws-gov")
    keys.write_credential("aws-gov", creds, tmp_path)
    captured = capsys.readouterr()
    for value in (SECRET, TOKEN, ACCESS):
        assert value not in captured.out and value not in captured.err


# --- the `ma keys` command ------------------------------------------------


def test_cli_keys_writes_the_file_and_reports_the_expiry(monkeypatch, tmp_path, capsys):
    calls = []
    stub_aws(monkeypatch, calls=calls)
    code = cli.main(["keys", "aws-gov", "--cred-dir", str(tmp_path)])
    out = capsys.readouterr().out
    assert code == 0
    assert calls[0][calls[0].index("--profile") + 1] == "aws-gov"  # profile defaults to the name
    assert str(tmp_path / "aws-gov.env") in out
    assert EXPIRY in out
    assert SECRET not in out and TOKEN not in out
    assert (tmp_path / "aws-gov.env").read_text().startswith("# expires:")


def test_cli_keys_profile_may_differ_from_the_credential_name(monkeypatch, tmp_path, capsys):
    calls = []
    stub_aws(monkeypatch, calls=calls)
    code = cli.main(
        ["keys", "work-projA-gov", "--profile", "projA-gov", "--cred-dir", str(tmp_path)]
    )
    assert code == 0
    assert calls[0][calls[0].index("--profile") + 1] == "projA-gov"
    assert (tmp_path / "work-projA-gov.env").exists()
    capsys.readouterr()


def test_cli_keys_says_long_term_when_there_is_no_expiry(monkeypatch, tmp_path, capsys):
    import json

    stub_aws(monkeypatch, result=_Completed(stdout=json.dumps(LONG_TERM)))
    code = cli.main(["keys", "aws-home", "--cred-dir", str(tmp_path)])
    out = capsys.readouterr().out
    assert code == 0
    assert "long-term keys (no expiry)" in out


def test_cli_keys_failure_exits_two_with_stderr(monkeypatch, tmp_path, capsys):
    stub_aws(monkeypatch, result=_Completed(1, stderr="Error loading SSO Token: expired\n"))
    code = cli.main(["keys", "aws-gov", "--cred-dir", str(tmp_path)])
    captured = capsys.readouterr()
    assert code == 2
    assert "expired" in captured.err
    assert captured.out == ""
    assert not (tmp_path / "aws-gov.env").exists()
