---
name: aws-ai-builder
description: Pitfall rules, a review checklist and tested Python helpers for generative and agentic AI on AWS, drawn from 22 AWS SimuLearn labs and 12 FDE microcredential labs. Covers Bedrock Converse, prompts and Guardrails; Knowledge Bases and hybrid RAG; agents and tools on AgentCore (Runtime, Gateway, Memory, Identity, Policy, Evaluations), Bedrock Agents, and Strands or MCP on AgentCore; OBO token exchange, JWT and Cedar for agents; tracing and cost per agent; DynamoDB, Aurora pgvector, Neptune, lakehouse and streaming data for agents; Comprehend, Rekognition, Textract, Transcribe, Polly, Translate, Personalize, Kendra and Lex; API Gateway and Lambda front ends; private VPCs; SageMaker fine-tuning and Step Functions. Use when designing, building, reviewing or debugging an AWS app or agent that calls an LLM, when a Bedrock or AgentCore call is cut off, times out or is denied, when testing a guardrail, knowledge base or agent eval, or in an AWS SimuLearn, FDE1 or FDE2 lab.
---

# AWS AI builder

These rules come from labs where the validator passed but the system was broken. They cover what goes wrong in practice. For API details, load `aws-core:amazon-bedrock` (its AgentCore references included), `aws-core:aws-serverless`, `aws-core:aws-iam`, `aws-core:aws-observability` or `aws-core:aws-containers`, or read the AWS docs. For general agent design, use `agent-architect` (workflow or agent, which pattern), `agent-threat-modeler` (threat model and red team), `agent-eval-designer` (eval plans) or `agentcore-deployer` (container builds and multi-environment deploys); this skill holds the AWS-specific pitfalls. In a SimuLearn or FDE lab, read `references/labs.md` before you start, and snapshot before you click Validate or End Exam Lab: labs tear down, or revoke credentials, minutes after a pass.

## Quick start

Eight rules that caught the most bugs:

1. Read `stopReason` on every Converse call. `max_tokens` means the reply was cut off, and the call still returns HTTP 200.
2. Treat model output as untrusted input: SQL, Cypher, HTML, labels, JSON, links, sums and tool arguments.
3. Budget timeouts from the outside in. A REST API's integration timeout is 29 s by default, so (connect + read) × attempts × sequential calls must stay under it.
4. Many update APIs replace the whole document (Lambda env vars, guardrails, gateway targets, agent runtimes). Read, merge, write, then wait for ready.
5. Test both sides: must-block and must-pass, answerable and unanswerable, permitted and denied.
6. Take identity from a verified token, never from the conversation or a tool argument. Each agent hop gets its own token for the next hop.
7. Put hard limits where every call passes (Cedar at the gateway, a read-only database role), not in the prompt.
8. Score agents against ground truth, with enough cases for the bar, and keep the dimensions you aren't tuning in the same run.

## Build a feature or an agent

1. Pick the pattern and open its reference (list below). For an agent, start at `references/agents-and-tools.md` section 1.
2. Start from a bundled script if one fits. Otherwise start from the reference snippet.
3. Write the two-sided test set (and, for an agent, the eval set) before tuning prompts, thresholds or policies.
4. Run each prompt variant at least 3 times and score it in code.
5. For an agent: inbound auth on every runtime and gateway, a token per hop, Cedar permits before ENFORCE, a guardrail, traces with the trace ID in the logs, and a cost ledger.
6. Before you ship, go through `references/review-checklist.md` for the parts you touched.

## Review an existing app or agent

Go through `references/review-checklist.md` section by section. Report each failure with the file and line, or the console setting. Scripts that check for you: `asl_lint.py` (Step Functions), `secure_sm_catalog.py audit-policy` (S3 or ECR endpoint policies), `cypher_guard.py` (generated Cypher), `policy_smoke_test.py` (Cedar allow and deny cases through a gateway), `create_custom_evaluator.py --check` (judge configs).

## Debug by symptom

| Symptom | Usual cause | Read |
|---|---|---|
| Reply cut off mid-sentence, HTTP 200 | `maxTokens` too small, `stopReason` not checked | `models-and-prompts.md` |
| Lambda in a VPC hangs until timeout | No `bedrock-runtime` interface endpoint, or its security group blocks 443 | `network-and-iam.md` |
| AccessDenied for one model only | The role lacks that model's ARN, or a cross-Region profile's foundation model ARNs | `network-and-iam.md` |
| Guardrail has no effect | The app doesn't pass `guardrailConfig`, or uses an old version | `guardrails.md` |
| Every chat turn blocked after one bad turn | The whole history is re-checked, not just the newest turn in `guardContent` | `guardrails.md` |
| Answers with masked PII are discarded | `guardrail_intervened` treated as a block when the guardrail only masked | `guardrails.md` |
| A setting vanished after an update | The update request didn't carry the whole config | `serverless-apps.md`, `guardrails.md`, `agents-and-tools.md` |
| RAG answers "I don't know" to everything | Empty KB (no sync), floor copied from another store, or k too small | `rag-and-vectors.md` |
| RAG answers off-topic questions | No score floor, or no grounding rule in the prompt | `rag-and-vectors.md` |
| Agent loops on tools or never answers | No call cap, tool errors returned as empty success, or too many tools | `agents-and-tools.md` |
| Runtime says "entrypoint could not be found", or a module is missing | Zipped the folder instead of its contents, or dependencies not vendored | `agents-and-tools.md` |
| Agent forgets between turns or sessions | New agent per request, or Memory namespaces don't match | `agents-and-tools.md` |
| 401 or 403 at a runtime, gateway or API | Wrong layer: inbound JWT, outbound credential or upstream OAuth | `agent-security-and-identity.md` |
| Downstream agent accepts a forwarded user token | No per-hop OBO exchange, or `aud` not checked | `agent-security-and-identity.md` |
| Read-only tool blocked right after enabling policies | Cedar is default deny; no permit for that tool | `agent-security-and-identity.md` |
| Eval scores high but answers are wrong | Reference-free judges only, or a lenient custom scale | `agent-evals-and-observability.md` |
| Traces split or missing, logs not joined to traces | Two tracer providers, thread pools, or logging outside the span | `agent-evals-and-observability.md` |
| Moderation passes everything when a service fails | A failed moderator read as "not flagged" | `ai-services.md` |
| Firehose fails the whole batch | Transform response shape wrong | `data-for-agents.md` |
| Env var change wiped other settings | `UpdateFunctionConfiguration` replaces all variables | `serverless-apps.md` |
| Browser shows CORS errors | No CORS headers on errors, `Authorization` not allowed, or an authorizer on OPTIONS | `serverless-apps.md` |
| Step Functions succeeded, endpoint still Creating | CreateEndpoint has no `.sync`, so nothing polled | `training-and-pipelines.md` |
| Fine-tune "improved" but outputs got worse | Labels read by position, or the change is within noise | `training-and-pipelines.md` |

## Scripts

Python 3.10+ (the older five scripts also run on 3.9), standard library, with boto3 (or neo4j, for `cypher_guard.run_read`) imported only when a call needs it. Run the tests with `python -m unittest discover -s scripts`. Some tests need botocore; `test_obo.py` needs PyJWT and cryptography; `test_real_libraries.py` uses moto, PyJWT, opentelemetry-sdk and mcp. Each skips without them. Exit codes: 0 when the checks pass, 1 when a check fails, 2 for bad arguments, unreadable input or a failed call. `kb_rag.py` ran against three live Knowledge Bases, and `asl_lint.py` against real lab definitions. `test_real_libraries.py` runs `dynamodb_state` and the authorizer's audit writer against moto's DynamoDB, checks `verify_rs256` against PyJWT on 300 tampered tokens, and checks `genai_telemetry` and `policy_smoke_test` against the OpenTelemetry and MCP SDKs. The rest are tested against fakes only. None of the FDE scripts has run against live AWS, so try them on a test account first.

| Script | What it does |
|---|---|
| `asl_lint.py` | Lints a state machine: unreachable states, loops without TimeoutSeconds, Choice without Default, CreateEndpoint with no later Wait |
| `guardrail_checks.py` | Must-block and must-pass prompts through ApplyGuardrail; `guardrail_blocked` tells a block from masking |
| `kb_rag.py` | Retrieve then Converse for managed and vector KBs; answerable, unanswerable and cut-off checks |
| `rrf_fusion.py` | Reciprocal rank fusion of keyword and vector result lists |
| `summarize_api.py` | A hardened API Gateway + Lambda + Converse handler (CORS from `ALLOW_ORIGIN`), and `check-model` |
| `secure_sm_catalog.py` | VPC endpoint checks, ECR endpoint gaps, endpoint-only Deny policies and a policy audit |
| `dynamodb_state.py` | Conditional, versioned agent checkpoints and a capability registry in DynamoDB |
| `cypher_guard.py` | Read-only guard for model-generated Cypher; exit 0 allowed, 1 rejected |
| `firehose_transform.py` | Firehose transform Lambda with the right response shape and date partition keys |
| `jwt_authorizer.py` | API Gateway Lambda authorizer: pinned RS256, signature before claims, fail-closed checks |
| `obo_exchange.py` | RFC 8693 token exchange per hop, with the `sub`, `aud` and scope checks |
| `policy_smoke_test.py` | Allow and deny cases through an AgentCore gateway (`fixtures/policy_cases.example.json`) |
| `genai_telemetry.py` | OTel GenAI usage attributes, cost per call with cache split, best-effort telemetry writes |
| `create_custom_evaluator.py` | Checks and creates AgentCore custom LLM judges |

## References

- `references/models-and-prompts.md`: Converse client and timeouts, stop reasons, error mapping, model swaps, prompt caching, prompt rules, untrusted output
- `references/guardrails.md`: ApplyGuardrail against `guardrailConfig`, multi-turn chat, masking against blocking, safe updates, versions and tiers, denied topics, logging
- `references/rag-and-vectors.md`: Retrieve then Converse, score meanings per store, hybrid search and RRF, ingestion, chunking, OpenSearch filters, Transcribe triggers
- `references/agents-and-tools.md`: picking a platform, the tool loop, AgentCore Runtime, Gateway, Memory, Code Interpreter and Browser, Strands, MCP, A2A and AG-UI, multi-agent patterns, shared state, human approval
- `references/agent-security-and-identity.md`: workload identity, JWT authorizers, outbound credentials, OBO exchange, validating JWTs, Cedar, OWASP agentic threats, PII between agents, MCP hardening, mandatory guardrails, audit
- `references/agent-evals-and-observability.md`: what to score, eval sets, custom judges, batch and online evaluation, tracing, sampling, HIPAA-style handling, cost attribution, alarms
- `references/data-for-agents.md`: which store holds what, DynamoDB memory, pgvector, graph stores and the Cypher guard, text-to-SQL guards, lakehouse, streaming
- `references/ai-services.md`: Comprehend, Rekognition, Textract, Transcribe, Polly, Translate, Personalize, Kendra and Lex: when to use each, async jobs, limits, fail-closed moderation, cost traps
- `references/serverless-apps.md`: the front-end hardening list, replace-the-whole-document APIs, readiness waits, ECS behind an ALB
- `references/network-and-iam.md`: Bedrock and SageMaker in private subnets, endpoints, `aws:SourceVpce` Deny policies, Service Catalog
- `references/training-and-pipelines.md`: LoRA and instruction fine-tuning, reward labels, early stopping, judging results, endpoints from Lambda, Step Functions for ML, cost
- `references/review-checklist.md`: the checklist for reviews
- `references/labs.md`: SimuLearn and FDE labs: snapshot first, planning, role limits, console and IDE traps, notebooks
