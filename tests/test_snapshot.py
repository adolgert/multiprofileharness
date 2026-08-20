import json

from multiagent import snapshot
from multiagent.types import ObservedModel, ProbeResult


def test_observed_state_keeps_status_and_sorted_ids():
    results = {
        "lab": ProbeResult(status="live", models=[ObservedModel("qwen"), ObservedModel("llama")]),
        "cloud": ProbeResult(status="down", error="refused"),
    }
    assert snapshot.observed_state(results) == {
        "lab": {"status": "live", "models": ["llama", "qwen"]},
        "cloud": {"status": "down", "models": []},
    }


def test_save_stamps_and_load_strips(tmp_path):
    path = tmp_path / "sub" / "snapshot.json"
    state = {"lab": {"status": "live", "models": ["llama"]}}
    snapshot.save(path, state)

    on_disk = json.loads(path.read_text())
    assert "recorded_at" in on_disk
    assert on_disk["lab"] == state["lab"]
    assert snapshot.load(path) == state


def test_save_does_not_mutate_caller_state(tmp_path):
    state = {"lab": {"status": "live", "models": []}}
    snapshot.save(tmp_path / "s.json", state)
    assert "recorded_at" not in state


def test_load_missing_file(tmp_path):
    assert snapshot.load(tmp_path / "nope.json") == {}


def test_load_corrupt_json(tmp_path):
    path = tmp_path / "s.json"
    path.write_text("{not json")
    assert snapshot.load(path) == {}


def test_load_non_object_json(tmp_path):
    path = tmp_path / "s.json"
    path.write_text("[1, 2]")
    assert snapshot.load(path) == {}


def live(*models):
    return {"status": "live", "models": list(models)}


def test_diff_first_observation():
    assert snapshot.diff({}, {"lab": live("llama")}) == {"lab": ["first observation"]}


def test_diff_model_added():
    prev = {"lab": live("llama")}
    assert snapshot.diff(prev, {"lab": live("llama", "qwen")}) == {"lab": ["new: qwen"]}


def test_diff_model_removed():
    prev = {"lab": live("llama", "qwen")}
    assert snapshot.diff(prev, {"lab": live("llama")}) == {"lab": ["gone: qwen"]}


def test_diff_add_and_remove_together():
    prev = {"lab": live("llama", "qwen")}
    assert snapshot.diff(prev, {"lab": live("llama", "mistral")}) == {
        "lab": ["new: mistral", "gone: qwen"]
    }


def test_diff_recovered_from_down():
    prev = {"lab": {"status": "down", "models": []}}
    assert snapshot.diff(prev, {"lab": live("llama")}) == {
        "lab": ["new: llama", "was down, now live"]
    }


def test_diff_went_down():
    prev = {"lab": live("llama")}
    current = {"lab": {"status": "down", "models": []}}
    assert snapshot.diff(prev, current) == {"lab": ["gone: llama", "went down (was live)"]}


def test_diff_unchanged_is_empty():
    prev = {"lab": live("llama", "qwen")}
    assert snapshot.diff(prev, dict(prev)) == {}


def test_diff_ignores_backend_only_in_prev():
    prev = {"lab": live("llama"), "gone_backend": live("x")}
    assert snapshot.diff(prev, {"lab": live("llama")}) == {}


def test_diff_static_backends_are_silent():
    prev = {"vendor": {"status": "static", "models": ["opus"]}}
    current = {"vendor": {"status": "static", "models": ["sonnet"]}}
    assert snapshot.diff(prev, current) == {}
    assert snapshot.diff({}, current) == {}
