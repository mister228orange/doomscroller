# Scoring pipeline

The scorer reads the archive, sends each unread post through a rubric of editable markdown
and writes two things in one transaction: a `post_scores` row and the updated
`channel_states` trajectory. It never talks to Telegram — it needs `DATABASE_URL` and an
OpenAI-compatible model endpoint, nothing else.

```bash
doomscroller score                    # one tick
doomscroller score --loop             # the scheduler (compose: `scorer` service)
doomscroller score --dry-run          # assemble prompts, call nothing, write nothing
doomscroller score --check            # rubric + database + endpoint, no spending
```

## Where the rubric lives

`scoring/` is nine markdown files, not Python constants. `prompts.load_modules` reads all
of them, concatenates the parts in a fixed order and hashes the result into
`prompt_version` (first 12 hex chars of SHA-256), which every score row stores.

| File | Goes into | Purpose |
|---|---|---|
| `00_system.md` | system prompt | role, input, boundaries (contains `{{TEXT}}`-style pointers) |
| `01_axes.md` | system prompt | the six axes I / V / U / P / S / D |
| `02_classification.md` | system prompt | primary spheres + secondary tags |
| `03_rules.md` | system prompt | rules 1–15 |
| `04_formulas.md` | system prompt | KPD / Eff |
| `05_anchors.md` | system prompt | anchors A1–A10 with `LOCATOR:` pointers |
| `06_procedure.md` | **user prompt** | procedure + answer format (the task) |
| `07_selfcheck.md` | system prompt | self-check rules and glossary |
| `08_channel_context.md` | system prompt | how to read the channel-state block |
| `09_summary.md` | separate call | channel-memory update task |

The split matters: `06` is the task, so it sits immediately before the data in the user
turn, where models follow it most reliably, and the system prompt stays byte-identical
across calls so a backend with prompt caching can reuse it.

`06_procedure.md` is *also* editable, but it is the file that defines the JSON contract —
change it only together with `rubric.finalize`.

The search order is `SCORER_RUBRIC_DIR` (a contract — if set and wrong, loading fails),
`<repo>/scoring`, the wheel's copy `doomscroller/rubric`, then `./scoring`. The wheel
ships the rubric via hatchling `force-include`.

## Prompt assembly

`prompts.render_prompt` builds `(system, user)` for one post:

* **System** — modules `00`, `01`, `02`, `03`, `04`, `05`, `07`, `08` joined with blank
  lines. Placeholders in `00_system.md` are replaced with *pointers* (`TEXT` →
  `<в блоке ДАННЫЕ ниже>`), never with copies of the data, so the text appears exactly
  once in the prompt.
* **User** — `06_procedure.md`, then `# ВЕРСИЯ МЕТОДИКИ` + the hash, then the data block:

```
# ДАННЫЕ

TEXT:
<<<
<post text, capped at SCORER_MAX_CHARS with an explicit truncation note>
>>>

CONTEXT:
— не передан

GENRE:
— не передан (определи сам)

CHANNEL_STATE:
PROFILE: ...
TREND: I 6.1 | V 5.8 | ... | КПД 6.42 (окно 5, постов 12)
LOCAL_ANCHORS:
- p_4321 «...» [TECH] I 7.0 | ... → КПД 6.90
TAG_DIST: TECH 4, ECON 2
SUMMARY: ...
EVENTS:
- 2026-11-01T12:00:00Z peak: ...
```

`render_channel_state` produces that block from a `state_snapshot`: profile, EWMA trend
from `means`, the last five local anchors, the tag distribution, the rolling summary and
the last three trajectory events. The `GENRE` slot is either the `--genre` override or
"determine it yourself"; `CONTEXT` is accepted by the renderer but not currently filled
by the agent.

## Selection: what counts as "unread"

`agent.iter_pending_posts` streams messages in `message_id` order (no OFFSET, so it stays
linear on a huge chat) and yields a post when `needs_scoring` says so:

1. no `post_scores` row;
2. `status='failed'` and `attempts < SCORER_MAX_ATTEMPTS`;
3. the message was edited after it was scored (`rescore_edited`, on by default);
4. OCR text was written after it was scored.

Additional filters, applied before yielding:

* `messages.deleted = false` — deleted posts are not scored;
* body must be non-empty after combining caption and OCR text;
* first-time sampling: `message_id % SCORER_SAMPLE_EVERY != 0` is skipped. The modulo is
  on the message id, so a post is either always in the sample or always out; raising the
  factor later reads the rest without re-reading the sample. `--force` and rescores
  ignore sampling entirely;
* one attempt per post per tick (`attempted` set); a failure is retried on the next tick.

A post shorter than `SCORER_MIN_CHARS` (default 120) is not sent to the model: it is
recorded as `skipped` with the character count in `detail`. A skipped post stays skipped
until it is edited (or `--force`).

`--limit` (and `SCORER_MAX_POSTS_PER_RUN`) bounds **model calls**, not examined posts: a
tick may walk past thousands of short archived one-liners without spending budget. The
scan is also capped at `max(1000, remaining × 50)` messages per channel to keep a tick
bounded.

## One post, step by step

`agent._score_channel`:

1. Load (or create) the channel state and render its snapshot.
2. Take the next pending post and build the prompt.
3. **Dry run** — append the prompt to the report and stop; nothing is written, not even a
   `ChannelState` row.
4. `llm.chat(system, user)` — see [LLM client](#llm-client).
5. `rubric.extract_json` (tolerates fences and prose around the object) and
   `rubric.finalize` — validation and the mechanical self-check (below).
6. `agent.store_score` — one transaction: upsert the `PostScore`, apply it to the state
   (or replay the whole trajectory when re-scoring) and refresh counters.
7. If `summary_every > 0` and the channel has at least that many unsummarised posts (or
   has no summary yet), run `refresh_memory` — one extra model call.

On an `LLMError` the post becomes `failed` with the error in `detail`, and the channel's
tick stops (the next tick retries). On a `RubricError` the post is `failed` but the tick
continues — one bad answer does not stop the run.

## Validation and the mechanical self-check

`rubric.finalize` accepts the model's JSON and enforces what the rubric says in prose:

| Rule | Enforcement |
|---|---|
| six axes required, numeric, 0–10 | missing/non-numeric → `RubricError`; out of range → clamped + violation |
| `primary` must be a known sphere (TECH/POWER/ECON/SOC/CULT/SCI/PERS) | otherwise `RubricError` |
| at most two secondary tags from the known set | unknown tags dropped, excess truncated |
| genre ceiling on I (`GENRE_I_CAP`, rule 12) | I clamped to the cap, violation recorded |
| folklore means no thesis (rule 9) | D forced to 0 |
| "legitimises" instead of a new correlation (rule 10) | V capped at 4 |
| distance from the nearest named anchor (self-check 6) | > 0.5 → `confidence` forced to `low` |
| `density_type` ∈ concentration/saturation | default `concentration` + violation |
| empty `main_thesis` | placeholder + violation |

Derived numbers are **recomputed, never trusted**:

```
KPD = (I + V + U + D) / 4
Eff = KPD / max(P, 1)          only when KPD > 5.0, else null
```

`Eff` is deliberately undefined for weak texts, and `channel_states.eff_mean` averages
only defined values (`eff_n` counts them).

Everything the model proposed is kept in `post_scores.raw`; every deviation from the
mechanics is kept in `selfcheck` (a JSON list). That makes "why does this row say 6.9"
answerable without re-running the model.

## Channel trajectory

`scoring/state.py` folds each new score into the channel projection. Order is
load-bearing: deviations are measured against the trend **before** the post, the means
update **after**, and `trend_position` is computed against the **new** mean.

| Quantity | Rule |
|---|---|
| `means` | per-axis EWMA, `new = (old×(window−1) + value)/window`; the first post seeds it |
| `deviation` on the score | `value − means_before`, per axis; flags `deviation:<axis>:±d` when abs > 1.0 |
| `deltas` on the state | `value − means_after` |
| `kpd_mean`, `eff_mean` | EWMA of KPD and of defined Eff values |
| `trend_position` | `above` / `below` when \|KPD − kpd_mean\| > 0.25, else `at` |
| `tag_distribution` | count per primary sphere |
| `local_anchors` | last five scored posts of the channel (id, title, scores, KPD) |
| peak | new channel-best KPD updates `kpd_max`; a `peak` event is emitted when it clears 7.5 |
| shift candidate | ≥ 2 axes moved by ≥ 1.5 from the trend, and ≥ 10 posts scored in total |
| shift event | 3 consecutive candidates → `shift` event, streak reset |

Shift detection is gated on `MIN_POSTS_FOR_SHIFT = 10`: with one or two posts, every
strong post would look like a shift against a one-score "trend".

**Re-scoring replays the trajectory.** If a post was already `scored` and is rescored
(e.g. edited upstream), `store_score` calls `recompute_state`, which resets the projection
and re-applies every scored row in message order. Incremental arithmetic would keep the
stale contribution doubled. Trajectory events are append-only and are not re-emitted.

Counters (`total_scored` / `total_skipped` / `total_failed`) are re-counted from
`post_scores` on every write (`refresh_counters`) rather than incremented by hand, so a
status change cannot drift.

## Channel memory

Every `SCORER_SUMMARY_EVERY` new posts (and once for the first post) the agent makes a
second, separate model call with `09_summary.md` as the system prompt and a user message
containing the channel profile, the previous summary, the scored total and the last ten
scored posts. The answer's JSON updates:

* `profile_description` — what the channel is about;
* `summary` — the rolling narrative;
* `primary_dominant` / `secondary_dominant`;
* `drift_note` — how the channel has changed.

It also appends a `summary` row to `channel_events`. Failures here are logged and
ignored: a memory refresh never fails a post.

## LLM client

`scoring/llm.ChatClient` is deliberately small: one base URL, one key, one model id, the
OpenAI `/chat/completions` shape. That covers NanoGPT (the default), DeepSeek direct and a
local Ollama.

* **Temperature 0** by default — a score should be reproducible, not sampled.
* **JSON mode** — `SCORER_JSON_MODE=auto|on|off`. In `auto`, `response_format:
  {"type":"json_object"}` is sent first; if the backend answers `400`, the client drops it,
  remembers, and retries the same call once. `on` forces it, `off` never sends it.
* **`reasoning_effort`** — sent only when `SCORER_REASONING_EFFORT` is set (none/low/high/
  max). Reasoning models can burn thousands of output tokens per post; `none` restores the
  plain regime and makes runs comparable.
* **`max_tokens`** — sent only when non-zero. On NanoGPT the per-request deposit is sized
  from it, so `SCORER_MAX_TOKENS=2000` is the fix for `402 Insufficient balance`.
* **Retries** — only for retryable failures (408/409/425/429 and 5xx, plus transport
  errors), with exponential backoff `0.5 × 2^n`. 401/402/403/404/413 are raised
  immediately with a status-specific hint: retrying a bad key just hides the message.
* **Empty answer** — if a reasoning model returns only `reasoning_content`, the client
  raises a specific `LLMError` instead of writing an empty score.

`doomscroller score --check` exercises rubric, database and endpoint without scoring a
post; `--list-models` queries the public model list (works without a key).

## OCR integration

Photo posts are read by a vision model (`doomscroller/ocr.py`, default
`deepseek/deepseek-v4.1-flash`) before scoring: English pass first, Russian pass only if
the first found nothing, and a textless image is stored as `no_text` and never re-read
unless `--force`. The scorer's body is `combined_body(caption, ocr_text)` — caption first,
then `[текст с изображения]` and the transcription, so the rubric can treat photographed
text as borrowed content (rule 11).

An OCR row written after a post was scored flips `needs_scoring` to true, so the post is
re-graded with the text it should have had.

## Anchors and calibration

`scoring/05_anchors.md` defines A1–A10 with reference scores and optional
`LOCATOR: channel#message_id` pointers.

```bash
doomscroller anchors                    # which anchors exist in this archive
doomscroller anchors --calibrate        # score them, diff against the reference
doomscroller anchors --calibrate --anchor A3 --json
```

`calibrate` scores each archived anchor with the **same rubric but no `CHANNEL_STATE`**:
an anchor is a global reference and must be judged absolutely, not relative to one
channel's trend. For each axis it reports `model − reference`; the worst absolute delta
above `0.5` marks the anchor as `drift`, otherwise `ok`. `--calibrate` writes nothing to
`post_scores` — it is a diagnostic.

## Cost and failure modes

| Symptom | Meaning / fix |
|---|---|
| `HTTP 402 Insufficient balance` | NanoGPT sizes a per-request deposit from `max_tokens`; set `SCORER_MAX_TOKENS=2000`. A ping can still succeed while scoring fails. |
| huge `completion_tokens`, slow runs | reasoning model; set `SCORER_REASONING_EFFORT=none`, keep `SCORER_TIMEOUT` around 90 for interactive runs |
| one post blocks a tick for 10 minutes | the hung-request case; lower `SCORER_TIMEOUT` and `SCORER_RETRIES` |
| every post `failed` with the same error | endpoint/key/model — run `score --check` |
| scores mix models and prompt versions | expected on old rows: check `post_scores.model` / `prompt_version`; the MCP `channel_states` and `connector_status` report what is actually in use |
| nothing is pending but the channel looks ungraded | the pipeline may simply be stopped — check `connector_status`: stale subscriptions with `last_error: null` mean "not running", not "failing" |

Config (see `.env.example` for the full annotated list):

| Variable | Default | Effect |
|---|---|---|
| `SCORER_BASE_URL` | `https://nano-gpt.com/api/v1` | OpenAI-compatible endpoint |
| `SCORER_API_KEY` / `NANOGPT_API_KEY` | — | key (either name) |
| `SCORER_MODEL` | `deepseek-chat` | pinned model id |
| `SCORER_RUBRIC_DIR` | `<repo>/scoring` | rubric location (contract if set) |
| `SCORER_SAMPLE_EVERY` | `1` | read 1 of every N first-time posts |
| `SCORER_MIN_CHARS` / `SCORER_MAX_CHARS` | `120` / `12000` | skip threshold / prompt cap |
| `SCORER_MAX_POSTS_PER_RUN` | `20` | model calls per tick |
| `SCORER_MAX_ATTEMPTS` | `3` | retries across ticks for failed posts |
| `SCORER_SUMMARY_EVERY` | `5` | memory refresh cadence; `0` disables |
| `SCORER_WINDOW` | `5` | EWMA window of the trend |
| `SCORER_INTERVAL` | `900` | seconds between ticks in `--loop` |
| `SCORER_TIMEOUT` / `SCORER_RETRIES` | `600` / `2` | one call's timeout / retries |
| `SCORER_JSON_MODE` | `auto` | `response_format` policy |
| `SCORER_MAX_TOKENS` | `0` (unset) | output cap; also sizes the provider deposit |
| `SCORER_REASONING_EFFORT` | unset | thinking budget pin |
| `SCORER_GENRE` | empty | force one genre for every post |

## Source map

| Topic | Code |
|---|---|
| selection, run loop, storage | [`doomscroller/scoring/agent.py`](../doomscroller/scoring/agent.py) |
| prompt assembly, rubric loading | [`doomscroller/scoring/prompts.py`](../doomscroller/scoring/prompts.py) |
| JSON extraction, validation, KPD/Eff | [`doomscroller/scoring/rubric.py`](../doomscroller/scoring/rubric.py) |
| trajectory, EWMA, shifts, peaks | [`doomscroller/scoring/state.py`](../doomscroller/scoring/state.py) |
| endpoint client, retries, diagnostics | [`doomscroller/scoring/llm.py`](../doomscroller/scoring/llm.py) |
| anchors, calibration | [`doomscroller/scoring/calibrate.py`](../doomscroller/scoring/calibrate.py) |
| OCR | [`doomscroller/ocr.py`](../doomscroller/ocr.py) |
| rubric text | [`scoring/`](../scoring) |
