# Agent evaluations, observability and cost

Lab tags in brackets name the lab that showed each rule: [20] is SimuLearn lab 20, [FDE1-4] is FDE1 lab 4, [FDE2-7] is FDE2 session 7. Their notes are under `D:\AI\AWSAI\` when that folder is on the machine; the rules stand on their own without it.

[FDE2-8] marks guidance from a practice exam that a doc source confirmed. Rules with no tag come from the docs listed in the last section. Runtime and multi-agent setup are in `agents-and-tools.md`, prompt caching and model choice in `models-and-prompts.md`, guardrail traces in `guardrails.md`, token and identity audit in `agent-security-and-identity.md`.

## Contents

1. What to score
2. Building an eval set
3. Custom LLM-as-a-judge evaluators
4. Running a batch evaluation from Lambda
5. Online evaluation
6. Getting traces into CloudWatch
7. Span design
8. Sampling, retention, and dev against audit settings
9. HIPAA-style handling of traces and logs
10. Cost attribution
11. Alarms worth having
12. Scripts
13. Sources for rules without a lab tag

## What to score

- Score against ground truth as well as on style. Reference-free judges (Helpfulness, Coherence) rate an answer on its own, so a fluent, invented account balance scored high on Helpfulness and 0.00 on Correctness with an expected answer. High reference-free next to low ground-truth means "sounds right, is wrong" [FDE2-4].
- Read every custom judge's scale before you trust it. The lab's inherited "response quality" rubric gave 1.0 to anything polite and on topic, so it could never fail a wrong answer [FDE2-4].
- Keep the dimensions you aren't tuning in the same batch. Making the agent concise lifted the conciseness judge to 1.00 while Correctness and Helpfulness fell to 0.60 [FDE2-4].
- Use code before a judge: a code-based evaluator (a Lambda function) checks schema validity, required fields or PII deterministically, and its scores land next to the judge scores.
- For actions, check the system of record, not the agent's reply (`agents-and-tools.md`, section 5).

Built-in evaluators, by level:

| Level | Evaluators |
|---|---|
| SESSION | GoalSuccessRate |
| TRACE | Helpfulness, Correctness, Faithfulness, ResponseRelevance, ContextRelevance, Conciseness, Coherence, InstructionFollowing, Refusal, Harmfulness, Stereotyping |
| TOOL_CALL | ToolSelectionAccuracy, ToolParameterAccuracy |

The set keeps growing. Newer AWS posts add trajectory evaluators (exact, in-order and any-order match, which need `expectedTrajectory`) and skill evaluators, and list them under TOOL_CALL, so check the console for the current set and levels. Built-in IDs look like `Builtin.Correctness` [FDE2-4].

## Building an eval set

- Mix answerable questions with ones the agent must refuse. For a refusal, the expected answer is the refusal plus the redirect [FDE2-4]:

```json
[{"prompt": "Does my plan include roaming?",
  "expected_response": "I can't see your account. Check the app or contact billing support."}]
```

- Ground truth can be an `expectedResponse` per turn (trace level), or `assertions` and an `expectedTrajectory` of tool names (session level) [FDE2-4].
- Size the set for the bar you must show. A pass rate over n sessions carries a Wilson 95% interval; even 10 of 10 has a lower bound of 0.72, so 10 sessions can't support a 0.80 bar. The all-pass lower bound is n / (n + z²), so you need n ≥ bar × z² / (1 − bar): 16 sessions for 0.80 [FDE2-4].
- Count every question you sent. A session that errored or produced no trace counts as failed, never dropped. Report the platform's average next to your rate, since it covers only the sessions it found [FDE2-4].
- Run on one prompt version. Record the prompt parameter's version before and after each call, and refuse a run that mixes versions [FDE2-4].
- Keep a held-out judge the team can't read (an explicit Deny on `GetEvaluator` for its ID), so nobody tunes answers to its wording [FDE2-4].
- Run each variant more than once and keep must-pass with must-fail cases, as in `models-and-prompts.md`.

## Custom LLM-as-a-judge evaluators

The instruction must use at least one placeholder, and each level allows a fixed set [FDE2-4]:

| Level | Placeholders | Ground-truth placeholders |
|---|---|---|
| TRACE | `{context}`, `{assistant_turn}` | `{expected_response}` |
| SESSION | `{context}`, `{available_tools}` | `{assertions}`, `{expected_tool_trajectory}`, `{actual_tool_trajectory}` |
| TOOL_CALL | `{context}`, `{available_tools}`, `{tool_turn}`, skill placeholders | none |

```json
{"llmAsAJudge": {
  "modelConfig": {"bedrockEvaluatorModelConfig": {"modelId": "us.anthropic.claude-sonnet-4-5-20250929-v1:0"}},
  "instructions": "You are a performance evaluator. Judge only length and focus: one to three short sentences, no headings, lists or sign-offs.\nContext: {context}\nResponse: {assistant_turn}",
  "ratingScale": {"numerical": [
    {"value": 0, "label": "Not concise", "definition": "Padding, lists, greetings or extras."},
    {"value": 0.5, "label": "Partly concise", "definition": "Answers, then adds material nobody asked for."},
    {"value": 1, "label": "Concise", "definition": "Answers in at most three short sentences, then stops."}]}}}
```

- A judge with ground-truth placeholders works in on-demand (batch) evaluations only. Live traffic has no expected answer, and the service refuses it in an online config [FDE2-4].
- `ratingScale` takes exactly one of `numerical`, `categorical` or `multiDimensional`. Each entry has a `label` and a `definition`, plus a `value` for numerical scales. The aws-core reference's example writes `description`; the developer guide uses `definition`.
- When you start from a console template, rewrite its scale for your dimension. The lab kept a Faithfulness scale on a tone judge [FDE2-4].
- Pick the judge from a different model family than the agent; a judge scores its own family's answers high. The lab's Nova judge on a Nova agent fails this check [FDE2-4].
- Don't put output-format instructions in the prompt: the service appends its own request for a reason and then a score. Start with a binary scale when unsure, define every level, and give one to three worked examples.
- Creating an evaluator returns an ID `<name>-<10 chars>`; batches and online configs take the ID, not the name [FDE2-4].
- A slightly stricter in-house judge is a useful canary: the lab's own conciseness judge gave 0.75 where the hidden grader gave 1.00 [FDE2-4].

## Running a batch evaluation from Lambda

The console can't attach inline ground truth, so run `StartBatchEvaluation` from code [FDE2-4]. The working order:

1. Check the X-Ray trace segment destination is `CloudWatchLogs` and ACTIVE. While it is PENDING, spans are rejected with HTTP 400 and lost; on a fresh account that lasted up to about 9 minutes.
2. Fire one runtime session per question (IDs of 33 or more characters, such as `f"eval-{i:03d}-{uuid4().hex}"`), with a thread pool of 8 and adaptive retries. Read the response stream to the end, so the agent finishes and exports its spans.
3. Wait for each session's spans in the runtime log group `/aws/bedrock-agentcore/runtimes/<runtime-id>-DEFAULT`, in the `spans` stream only: other streams also mention session IDs. Use a deadline, not a fixed sleep, and rerun a traceless session once in a fresh session.
4. Submit the batch with the expected answers inline.

```python
ac.start_batch_evaluation(
    batchEvaluationName="concise_eval_3",            # letters, digits and _ only; unique per account
    evaluators=[{"evaluatorId": e} for e in ["Builtin.Correctness", "Builtin.Helpfulness", judge_id]],
    dataSourceConfig={"cloudWatchLogs": {
        "serviceNames": ["support_agent.DEFAULT"],
        "logGroupNames": [f"/aws/bedrock-agentcore/runtimes/{runtime_id}-DEFAULT"],
        "filterConfig": {"sessionIds": session_ids}}},
    evaluationMetadata={"sessionMetadata": [
        {"sessionId": sid, "groundTruth": {"inline": {"turns": [
            {"input": {"prompt": q}, "expectedResponse": {"text": expected}}]}}}
        for sid, q, expected in fired]})
```

- Batch names are unique per account. Number them `<base>_<n>` from a listing and take the next number on `ConflictException` [FDE2-4].
- The Lambda needs `bedrock-agentcore:InvokeAgentRuntime`, `ListBatchEvaluations`, `StartBatchEvaluation` and `GetBatchEvaluation`; `logs:FilterLogEvents` on the runtime log group and `logs:GetLogEvents` on the results group; `s3:GetObject` on the ground truth; and a 15-minute timeout [FDE2-4].
- Ten sessions fired in 5.7 s once spans were flowing, and the batch finished in about a minute, against the lab's estimate of 8 to 11 minutes [FDE2-4].
- Per-session scores are in the batch's output log stream. The lab read them as `session.id`, `gen_ai.evaluation.name` and `gen_ai.evaluation.score.value`, names the docs describe but didn't show in a full event. If every evaluator reports zero scored sessions, print one event and fix the keys [FDE2-4].
- If the lab folders are on this machine, `D:\AI\AWSAI\FDE2\session4\code\lab_lambda\run-evaluation\index.py` has all of this, with the honest denominator and Wilson intervals, tested against fakes.

## Online evaluation

- An online config samples live sessions (0.01% to 100%, default 10%) and scores them on a schedule. Each evaluation is a model call, so start at 5 to 10%.
- Set the session idle timeout (default 15 minutes) to your agent's usual session length, so a session isn't scored while it is still running.
- Results go to `/aws/bedrock-agentcore/evaluations/results/<config-id>`, and each score becomes a metric in `Bedrock-AgentCore/Evaluations`, by evaluator and config [FDE2-4]. Alarm on those metrics (see the last section).
- Only sessions with spans can be scored; a batch can only score sessions whose spans are already queryable [FDE2-4].

## Getting traces into CloudWatch

- Enable Transaction Search once per account and Region (`aws xray update-trace-segment-destination --destination CloudWatchLogs`). It takes about 10 minutes, and spans sent before it are not searchable.
- Transaction Search ingests every span into the `aws/spans` log group and indexes 1% as trace summaries by default. You search all spans; the percentage only limits trace summaries.
- On AgentCore Runtime, the runtime sets `AGENT_OBSERVABILITY_ENABLED` and the exporter defaults. Don't repeat the `OTEL_*` exporter variables. Set `AWS_GENAI_CONTENT_EXTRACTION_OPT_OUT=true` to keep prompt and response text on the spans, where agent traces and evaluators read it. Unset, the content moves to OpenTelemetry log events; it is not dropped either way.
- A custom container must start the agent under `opentelemetry-instrument`. The ADOT distribution registers the global tracer provider: don't create a second provider or exporter (with Strands, pass the distribution's provider into `StrandsTelemetry`). Then you only write `tracer.start_as_current_span(...)` [FDE2-6].
- The service name for an agent on Runtime is `<agent-runtime-name>.<endpoint-name>`, for example `support_agent.DEFAULT` [FDE2-4].

## Span design

| Span (lab name) | `gen_ai.operation.name` | Attributes that paid off [FDE2-6] |
|---|---|---|
| `agent.invoke` (root) | `invoke_agent` | agent name, conversation ID, customer tier, model, dispatch mode, grounded flag, prompt version |
| `tool.execute` per hop | `execute_tool` | tool name, prefix-churn flag |
| `context.retrieval` | `retrieval` | category, candidate count, latency |
| `llm.inference` per model call | `chat` | model, the four token counts, `cost.usd`, grounded flag on the answer step |

- Set the convention's attributes as well as your own: `gen_ai.operation.name`, `gen_ai.agent.name`, `gen_ai.conversation.id`, `gen_ai.tool.name`, `gen_ai.request.model`, `gen_ai.provider.name = "aws.bedrock"`.
- Token counts: Bedrock's `inputTokens` excludes cached tokens (a call with 1,898 cached tokens reported `inputTokens: 28`). The convention's `gen_ai.usage.input_tokens` includes them, with reads and writes in `gen_ai.usage.cache_read.input_tokens` and `gen_ai.usage.cache_creation.input_tokens` [FDE2-6]. `scripts/genai_telemetry.py` does the mapping.
- The GenAI conventions are still marked Development. The published registry (November 2026) names the cache write count `gen_ai.usage.cache_creation.input_tokens`, and some drafts say `cache_write`. Keep the names in one module (`genai_telemetry.py` does) and change them there.
- Log inside the span. `trace_id = format(span.get_span_context().trace_id, "032x")` is empty once the span has closed, and that one field is what joins Logs Insights to traces [FDE2-6].
- `ThreadPoolExecutor` doesn't copy `contextvars`, so spans made in pool threads start new traces. Submit with `pool.submit(contextvars.copy_context().run, fn, *args)`, one fresh copy per task [FDE2-6].
- Telemetry writes must never fail the request. Wrap ledger rows and metrics in `best_effort()`, count the drops, and log them as `{"telemetry_dropped": sink}` for an alarm [FDE2-6].
- Silent failures return HTTP 200. Retrieval returned 0 candidates and the recommender still answered confidently; only the grounded flag showed it. Join the failing turn to its retrieval by trace ID [FDE2-6]:

```
fields trace_id, agent, candidate_count, error
| filter (agent="supervisor" and error="ungrounded_recommendation") or (agent="retrieval" and candidate_count=0)
| stats sum(agent="supervisor") as failing_turns, sum(agent="retrieval") as empty_retrievals by trace_id
| filter failing_turns > 0 and empty_retrievals > 0
```

- A trace waterfall shows N+1 and serial calls: a staircase of GetItem subsegments became one `batch_get_item` (retry `UnprocessedKeys`, skip an empty key list, which raises ValidationException), and two independent hops ran in parallel [FDE2-6].
- In OTel, span attributes reach X-Ray as metadata. To filter on one in X-Ray, list it in `aws.xray.annotations`; only 50 annotations per trace are indexed.
- Strip `X-Amzn-Trace-Id` from untrusted requests, or callers can choose trace IDs and sampling. Mint your own trace ID for audit records [FDE2-7].

## Sampling, retention, and dev against audit settings

| Setting | Development | Production | Audit copy |
|---|---|---|---|
| Trace sampling | 100% [FDE2-8] | 100% unless volume forces less (see below) | Every trace you must keep |
| Content on spans | Full | Redacted (next section) | Redacted, or kept under tighter access |
| Retention | Short, about 7 days [FDE2-8] | Set on every log group, never indefinite | As long as policy requires, in a store you can lock |
| Readers | Developers | On-call | Auditors only |

- The CloudWatch guide recommends leaving `OTEL_TRACES_SAMPLER` unset, which traces every request, because metrics computed from spans (token usage) become wrong once you sample. If you must sample, set `parentbased_traceidratio` at the service where requests start; downstream services follow its decision.
- So take cost and token totals from a source that isn't sampled: the per-call ledger [FDE2-6] or `AWS/Bedrock` metrics. The exam's "production = sampled" holds only if you do this [FDE2-8].
- Keep fast metrics for operations, and send full traces of every reasoning step to a separate store for audit, with its own access and retention [FDE2-8]. X-Ray keeps trace data for 30 days and that can't be changed; the `aws/spans` log group's retention is yours to set.
- Set a retention period on every log group, including runtime, gateway vended logs and evaluation results.

## HIPAA-style handling of traces and logs

PHI reaches telemetry in more places than the prompt log:

| Where | What it holds |
|---|---|
| Spans in `aws/spans` | Prompts, responses and tool calls (GenAI content), plus any attribute you set |
| Gateway vended logs | MCP request and response bodies |
| Model invocation logs | Unmasked prompts, even when a guardrail masked the reply |
| Evaluation results | Scores with the judge's reasoning about those traces [FDE2-4] |
| Your own attributes | The lab put the raw user query on `retrieval.query` [FDE2-6] |

- Redact before data reaches the trace store: drop or hash content attributes in instrumentation, and sample errors and high-risk actions in full but routine traffic less [FDE2-8].
- Put a CloudWatch Logs data protection policy on `aws/spans` and the application log groups. Masking hides matches from readers without deleting them, and only principals with `logs:Unmask` see the originals.
- Encrypt log groups (spans, vended logs, evaluation results) with a customer managed KMS key, restrict who can read them, and set retention.
- Log a keyed hash of the subject, never `sub`, username or email [FDE2-7]. Don't log guardrail traces. Keep the trace off unless the guardrail masks, and then pop it before logging (`guardrails.md`).
- Encrypt the SNS topics that alarms notify, and check who subscribes.

## Cost attribution

- Compute cost per call with the cache split. For Claude models on Bedrock, cache reads cost about 10% of the input price and writes about 125% [FDE2-6]. Other models price the cache differently, so take all four prices per model from config:

```python
from genai_telemetry import usage_attributes, cost_usd      # scripts/genai_telemetry.py

attrs = usage_attributes(resp["usage"])
cost = cost_usd(attrs, PRICES_PER_1K)   # {"input", "output", "cache_read", "cache_write"} from config
```

- A cache prefix that changes on every call makes every call a cache write. A supervisor prepended a per-request tag to the cached prefix; once the prefix was byte-identical, the ranking agent went from 0 cache-read tokens to 40,015 per window, and cost per call fell about four times [FDE2-6].
- Write one ledger row per call (agent, model, tier, conversation, tokens, cost) for reconciliation, and one metric with AgentId, Model and CustomerTier dimensions for graphs. Graph it with `SEARCH('{Namespace,AgentId,Model,CustomerTier} MetricName="CostUSD"', 'Sum', 300)` [FDE2-6].
- Keep tenant IDs off metric dimensions; use a tier there. Put the tenant ID in the ledger and in Converse `requestMetadata`, which model invocation logs record for filtering:

```python
bedrock.converse(modelId=PROFILE_ARN, messages=msgs,
                 requestMetadata={"tenantId": tenant_id, "agentId": "ranking"})
```

- For Cost Explorer, call the model through an application inference profile tagged per team or product, and activate the tags as cost allocation tags (about 24 hours to appear). Each profile is tied to one model, so tag at team level, not per user.
- IAM principal attribution in CUR 2.0 charges the role that called Bedrock. An agent's runtime role serves every tenant, so that gives cost per agent, not per tenant.
- Tag AgentCore resources with the same keys. The Identity docs show workload identity tags used as cost allocation tags.

## Alarms worth having

| Alarm | Source | Tag |
|---|---|---|
| Silent-failure rate per tier: `IF(turns >= MIN_TURNS, 100*FILL(bad,0)/turns, 0) > 10` for 2 windows | Metric filters on turn logs | [FDE2-6] |
| Evaluation score below threshold for 3 periods | `Bedrock-AgentCore/Evaluations` | [FDE2-4] |
| Token fetch failures, no-dimension series | `WorkloadAccessTokenFetchFailures` | [FDE1-4] |
| Gateway throttling and slow tools | `Throttles`, `SystemErrors`, `Duration` p99 against `TargetExecutionTime` p99 | [FDE2-8] |
| Runtime errors and platform latency | `Throttles`, `SystemErrors`, `Latency`, `OverheadLatency` | |
| Cache hit share drop per agent | Ledger or log query of cache read against write tokens | [FDE2-6] |
| Telemetry or audit writes dropped | Filters on `telemetry_dropped` and `audit_write_failed` | [FDE2-6, FDE2-7] |
| Spend | AWS Budgets at 80% of the monthly Bedrock budget | |

- Alarm on a rate, not a count. A count false-fires when traffic grows and misses incidents in quiet hours. Add a minimum-turns guard so a window with two turns can't alarm [FDE2-6].
- A metric filter with value 1 and default 0 gives the series zeros, so the alarm doesn't sit in INSUFFICIENT_DATA [FDE1-4]. A filter with dimensions can't have a default value [FDE2-6].
- The console alarm wizard defaults the statistic to Average; type Sum for counts [FDE1-4].
- For gateway timeouts, compare `Duration` with `TargetExecutionTime`: the gap is time spent at the gateway, and `Throttles` spikes in busy hours mean quota. Retry with backoff in the agent, and ask for a quota increase [FDE2-8]. The exam's names `RequestDuration` and `InvokeToolThrottled` are not in the metrics docs.
- Namespaces are case-sensitive. Confirm the AgentCore one with `aws cloudwatch list-metrics --namespace "Bedrock-AgentCore"` before writing an alarm.

## Scripts

| Script | What it does | Example |
|---|---|---|
| `genai_telemetry.py` | Bedrock usage to OTel GenAI token attributes, cost with the cache split, `best_effort()` writes that count drops | see Cost attribution |
| `create_custom_evaluator.py` | Checks a judge config (placeholders per level, scale shape, name, judge family), then creates it | `python scripts/create_custom_evaluator.py --check judge.json --level TRACE` |

Both are standard library; `create_custom_evaluator.py` imports boto3 only for `--create`. Exit codes: 0 pass, 1 a check failed, 2 bad arguments or a failed AWS call. The installed botocore had no `CreateEvaluator` model, so the request shape isn't checked offline; try `--create` on a test account first.

## Sources for rules without a lab tag

- AgentCore developer guide: create evaluator (placeholders, ground-truth rule, rating scales, best practices), create online evaluation, runtime and gateway observability metrics, gateway provided logs.
- Amazon CloudWatch user guide: send AI agent telemetry (content extraction opt-out, sampling, AgentCore setup), enable transaction search, Transaction Search. re:Post Knowledge Center: masking sensitive data with CloudWatch Logs data protection.
- Amazon Bedrock user guide: application inference profiles (scaling considerations), Converse `requestMetadata`; Bedrock API reference `TokenUsage`; AgentCore identity tagging. AWS ML blogs: prompt caching with Anthropic models (token counts), evaluate any agent framework with AgentCore Evaluations, custom code-based evaluators, automated evaluation with GitHub Actions; Amazon Bedrock AgentCore FAQs.
- aws-core skill references: `agentcore-registry-evaluations.md`, `agentcore-memory-observability.md`, `cost-tracking.md`, `guardrails.md`, `aws-observability/references/tracing.md`.
- OpenTelemetry GenAI semantic conventions: the registry page on opentelemetry.io and `docs/registry/attributes/gen-ai.md` in open-telemetry/semantic-conventions-genai.
