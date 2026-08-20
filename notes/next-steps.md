# Next steps and operational knowledge

What someone (including future me) needs to pick this work up cold: the ranked
work queue with its context, facts about the environment that were learned the
hard way, and the verification playbook the code was proven with.

## Work queue, ranked

1. **Harness image.** `ma run -- pi` is not real until an image built
   `FROM multiagent` installs the agent harnesses (pi, aider, claude, codex).
   The template stanza is in docs/daily-workflow.md § Images. Blocked on one
   fact: how the existing personal image installs `pi`.
2. **Usage ledger in process topology.** `docker/custom_callbacks.py`
   hardcodes `/var/ma-usage/usage.jsonl` and ships only in the image, so
   `--topology process` strips the callback and has no ledger. Fix: make the
   path env-overridable (`MA_USAGE_PATH`), move the module into
   `src/multiagent/`, have the Dockerfile copy it from there, and stop
   stripping the callback in run.py.
3. **Sidecar proxy topology** (the gov-credential wall): proxy in its own
   container on a per-launch network; the agent container never holds
   provider secrets even in its process env. Designed in the arch note,
   unimplemented. Do before GovCloud keys are used in anger.
4. **Ledger model-name inconsistency.** LiteLLM's callback kwargs carry the
   served id for ollama routes (`qwen2.5:7b`) but the canonical name for
   hosted ones (`gemini-2.5-flash`). Normalize to canonical in the callback
   if per-model reporting starts to matter.
5. **Split the `context` fact.** It conflates native window, served window,
   and max input (arch note names this wart). vLLM's `max_model_len` is a
   total window; the catalog's `max_input_tokens` is input-only; ollama
   reports the GGUF build context. Three quantities, one key.
6. **Bedrock liveness check.** Static backends show `listed` on file
   existence alone; an optional `sts get-caller-identity` (or
   ListFoundationModels) check would make the onboarding table honest for
   the backends that cost money. Deferred for latency + aws-cli dependency.
7. **Correctness review rerun.** The `/code-review high src/` pass stalled
   mid-run and never reported; the five other review lenses landed (their
   fixes are commits 10ba233 and d89d3d5). Rerun against the current tree.
8. **Proxy wheel-set lockfile.** `build-offline-bundle.sh --with-proxy`
   resolves litellm's transitives at build time; two runs months apart can
   differ. Check a compiled lockfile into the repo if bit-identical bundles
   ever matter.

## Environment facts learned the hard way

- **Bedrock's Anthropic use-case form gate activates on first invocation.**
  The personal account invoked Claude successfully once, then the same call
  returned "Model use case details have not been submitted" minutes later.
  The form was submitted programmatically (`aws bedrock
  put-use-case-for-model-access`, base64 JSON form-data; `get-…` verifies)
  and access returned within ~15 minutes. Amazon-brand models (Nova) were
  never gated. A work account will likely need the same dance once per
  account.
- **Region reality:** the personal profile defaults to us-west-2; us-west-1
  has a reduced Bedrock catalog; the `us.` cross-region inference profiles
  (required for current Claude models) were confirmed in us-west-2.
- **litellm 1.97.0 breaks with fastapi ≥0.141** (imports a removed internal,
  `get_flat_dependant`) while its own constraint admits it. The working
  window 0.136–0.140 was found by bisection; pins live in docker/Dockerfile
  and are echoed by scripts/build-offline-bundle.sh, which warns on drift.
- **Gemini 2.5 through the proxy returns empty content when `max_tokens` is
  small** (~10): thinking tokens consume the budget, HTTP 200, `content:
  null`. Not a plumbing failure. Test calls need `max_tokens` ≥ ~100.
- **ollama serves an OpenAI-compatible `/v1/models` on the same port** as its
  native API — that facade is what exercises the vLLM-shaped adapter at home
  (backend `home-ollama-openai`, project `adapter-test`). It omits
  `max_model_len`; real vLLM includes it.
- **ollama `/api/show`** provides `capabilities` (tools/vision/embedding —
  real observations) and `model_info.*.context_length` — which is the GGUF
  build context, NOT the served `num_ctx`. This is why deployment overrides
  outrank observations in the merge.

## Verification playbook

The container smokes were run from a scratch workspace so the repo isn't the
mounted workspace. The probe script (recreate as needed, e.g. `smoke.py`):

```python
import json, os, sys, urllib.request
leaked = [v for v in os.environ if v.startswith(("AWS_", "MA_BEDROCK", "MA_GEMINI"))
          or v in ("GEMINI_API_KEY", "ANTHROPIC_API_KEY")]
print("leaked-provider-vars:", leaked or "none")
print("uid:", os.getuid(), "env-file-readable:", os.path.exists("/run/ma/env"))
for model in sys.argv[1:]:
    req = urllib.request.Request(
        os.environ["OPENAI_BASE_URL"] + "/chat/completions",
        data=json.dumps({"model": model, "messages": [{"role": "user",
            "content": "Say OK and nothing else."}], "max_tokens": 200}).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + os.environ["OPENAI_API_KEY"]})
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            print(model, "->", json.load(r)["choices"][0]["message"]["content"][:40])
    except urllib.error.HTTPError as e:
        print(model, "-> HTTP", e.code, e.read()[:100])
```

The checks that constitute "it works":

1. `uv run pytest` — 231 tests, sub-second.
2. `ma models --project bedrock-work` — live ollama rows with per-deployment
   $1/Mtok prices, `listed` Bedrock rows with catalog prices, the qwen
   context conflict line naming the winner.
3. From a scratch dir: `ma run --project bedrock-work -- python3 smoke.py
   claude-haiku-4-5 nova-lite gemini-2.5-flash` — expect: no leaked vars,
   uid 1000, `/run/ma/env` unreadable, both Bedrock models OK, gemini HTTP
   400 (policy wall).
4. Same with `--project gemini-work` reversed: gemini OK, Bedrock models 400.
5. `~/.local/state/multiagent/usage/<project>/usage.jsonl` — cost equals
   tokens × the rendered price (nova 9 tokens → $1.08e-06 is the worked
   example).
6. Second `ma models` run is quiet (no change notes); `ollama pull`/`rm`
   generates real ones.

## Standing decisions

- Repo is public (MIT implied open source); flip to private is one `gh` call.
- The `home`-style catch-all project was deliberately replaced by the
  gemini-work/bedrock-work split so no project spans both money sources.
- Nominal local price is $1/Mtok both directions so ledger cost reads as
  Mtok — and it lives at deployment scope on purpose; don't move it back to
  model entries.
- Commit messages carry no AI signature (user preference).
