# Review checklist for an AWS generative or agentic AI app

Each tagged item failed in at least one lab: [16] is SimuLearn lab 16, [FDE1-3] is FDE1 lab 3, [FDE2-2] is FDE2 session 2. Untagged items come from the AWS docs or the aws-core skills, and the reference named in each section has the source. Go through the sections that apply, and report each failing item with the file, line or setting.

Sections: model calls, prompts and output, guardrails, retrieval, API and front end, agents and tools, agent identity and policy, memory and shared state, evaluation and observability, data stores for agents, AI services, streaming, network and IAM, async work and pipelines, evaluation and cost.

## Model calls
- [ ] Every Converse response's `stopReason` is checked: `max_tokens` and `model_context_window_exceeded` reach the caller as a truncation flag or an error, and `tool_use` and guardrail stops are handled separately [5, 7, 11, 17, 18, 20]
- [ ] The server chooses `maxTokens`, and it is large enough for the real output [5, 11, 17]
- [ ] (connect + read) × attempts × sequential calls < the API's integration timeout (29 s by default for REST APIs), and < the Lambda timeout [9, 10, 12]
- [ ] Throttling maps to 429/503, model failures to 502, bad input to 400, and nothing returns an error with HTTP 200 [5, 7, 10, 12]
- [ ] The model ID comes from config, and a missing value or placeholder gives a clear error, never a silent fallback [5, 6, 18]
- [ ] The role allows exactly the models the code calls. For a cross-Region profile, that's the profile ARN plus the foundation model ARN in every destination Region [7, 9]
- [ ] Model invocation logging, if on, is encrypted, access-restricted and has retention, because it stores unmasked prompts

## Prompts and output
- [ ] Rules are in the system prompt, and user or retrieved text is tagged and escaped [6, 10, 12]
- [ ] The model only receives data it may repeat [6]
- [ ] There is an explicit "I don't know" path [6, 12, 16]
- [ ] Generated SQL is read-only and row-capped, and runs as a read-only role [3]
- [ ] Model text shown in a page is escaped [4, 6, 21]
- [ ] Parsers fail closed (REVIEW or 502), and never show a default answer [4, 10]
- [ ] Sums, ordering, links and sources are produced by code [9, 10, 11]
- [ ] `<thinking>` blocks are stripped from agent replies [8]

## Guardrails
- [ ] The guardrail is actually passed (`guardrailConfig`) or called (ApplyGuardrail), not just created [14]
- [ ] Production uses a numbered version, not DRAFT [4, 15, 20]
- [ ] Each prompt is evaluated once: ApplyGuardrail or `guardrailConfig`, not both [20]
- [ ] Only the newest turn (plus retrieved text placed in it) is in `guardContent`, tool output is screened with ApplyGuardrail, and blocked exchanges are dropped from history [14]
- [ ] A masking-only intervention (ANONYMIZED) is treated as an answer, not a block [20]
- [ ] Streaming with a guardrail uses `streamProcessingMode: "sync"` for sensitive content
- [ ] The trace is not logged [20]
- [ ] A must-block list and a must-pass list are run after every change [14, 15]
- [ ] Updates were read-merge-write, then waited for READY, then versioned [14, 15]

## Retrieval
- [ ] The score floor was measured on this index, not copied from another store [11, 12, 16]
- [ ] Below the floor, the code skips the model call [11, 16]
- [ ] Chunking was tested with real questions [16]
- [ ] The loaded data is a random sample or the full set, not a sorted slice [17]
- [ ] Negation is a filter (`must_not`), not left to the embedding [17]
- [ ] Ingestion runs when documents change, and readiness is probed with Retrieve [12, 16]
- [ ] There are answerable and unanswerable test sets [16]

## API and front end
- [ ] Every method except OPTIONS has an authorizer [12]
- [ ] CORS names your origin and allows the `Authorization` header, and the Lambda returns CORS headers on errors and OPTIONS. Gateway 4XX and 5XX responses carry them too [4, 5, 7, 10, 12, 18]
- [ ] Stage or method throttling (or a usage plan) is set, and public REST APIs have WAF
- [ ] Logs hold metadata only, log groups have retention, and API Gateway data tracing is off [4, 5, 6, 7, 9, 10, 11]
- [ ] Input size is capped before any model call [5, 18]
- [ ] CloudFront uses OAC, CDN scripts have SRI, and uploads are followed by an invalidation [21]
- [ ] Secrets are in Secrets Manager, not env vars [9, 14]

## Agents and tools (`agents-and-tools.md`)
- [ ] The caller's identity comes from the authorizer, not from the conversation [8]
- [ ] User and session IDs are bound from the verified request, never taken from tool arguments the model fills [FDE2-1]
- [ ] Lookups are exact-match, not `LIKE '%name%'` [8]
- [ ] Write tools are idempotent, with atomic stock or balance checks in the same statement [8]
- [ ] Connections are reused and closed [8, 9]
- [ ] The tool loop has a call cap, and tool failures go back to the model as error results, not as empty success [8]
- [ ] Each tool's schema matches what its handler reads (names and JSON types), and was tested with `tools/list` and `tools/call` events [8, FDE1-2]
- [ ] Refusals are tool errors (`isError: true`), not success text starting with "Rejected:" [FDE2-1]
- [ ] Each specialist agent has only its own tools; a gateway with many tools uses semantic search [FDE2-8]
- [ ] Runtime updates resend every setting, and tests wait for READY on the new version [8, FDE1-3, FDE2-2]
- [ ] Callers that must not move with DEFAULT invoke a named endpoint pinned to a version [FDE2-2]
- [ ] No secrets in runtime env vars, because `get-agent-runtime` shows them
- [ ] Every gateway target points at the right ARN; a wrong one still shows Ready [FDE1-3]
- [ ] Credential providers exist before the targets that use them [FDE2-8]
- [ ] Peer agents are found by capability and version, retries sit behind a circuit breaker per agent, and nothing is dropped when retries run out [FDE2-2]
- [ ] Payloads between agents carry only the fields the next agent needs, checked by value [FDE2-2]
- [ ] Risky tools pause for human approval, and an unreadable model verdict goes to a person, never to a default decision [FDE2-2]

## Agent identity and policy (`agent-security-and-identity.md`)
- [ ] Every production runtime and gateway has inbound auth; a gateway with none accepts calls from anyone with its URL [FDE1-4, FDE2-5]
- [ ] The user-ID token path (`GetWorkloadAccessTokenForUserId`) is closed with an explicit Deny once a JWT authorizer is in place [FDE1-4]
- [ ] JWT authorizers set audience, allowed clients, scopes and a custom claim that refuses ID tokens [FDE1-4, FDE2-7]
- [ ] `Authorization` is in `requestHeaderAllowlist` wherever the agent must exchange the token [FDE2-7]
- [ ] Each hop exchanges its token (OBO) for one addressed to the next hop; no agent forwards the token it received [FDE2-7]
- [ ] Each exchange is checked: same `sub`, `aud` equal to the next hop only, no scope wider than asked [FDE2-7]
- [ ] A replayed login token is refused at the downstream agent (tested) [FDE2-7]
- [ ] A hand-written JWT check pins the algorithm, verifies the signature before reading claims, and denies on a missing `exp`, wrong `iss` or `aud`, or an ID token [FDE2-7]
- [ ] Cedar permits for safe tools exist before the gateway is switched to ENFORCE [FDE2-5]
- [ ] Policies that read tags or fields guard them with `hasTag` or `has`, and `like` patterns match delimiters
- [ ] A rule on a caller-declared field is backed by a lookup in the tool or an interceptor [FDE2-5]
- [ ] Every policy reached ACTIVE; `statusReasons` were read for any CREATE_FAILED [FDE2-5]
- [ ] Free-text tool arguments pass a prompt-attack check (a guardrail condition or ApplyGuardrail) [FDE2-5]
- [ ] ApplyGuardrail masks PII on payloads crossing agent boundaries, and tokens don't carry identity claims the next hop doesn't need [FDE2-7, FDE2-8]
- [ ] Workload token calls are logged with CloudTrail data event selectors, and token fetch failures have an alarm [FDE1-4]
- [ ] The audit write fails closed: if it fails, the hop doesn't proceed [FDE2-7]

## Memory and shared state (`agents-and-tools.md`, `data-for-agents.md`)
- [ ] State that must survive a session stop lives in Memory or a database, not in the runtime process [8]
- [ ] Memory strategy namespaces match exactly what the agent reads and writes, and start and end with `/` [FDE1-3]
- [ ] Memory was tested by stating a fact, waiting for extraction, then asking in a new session [FDE1-3]
- [ ] Turn IDs are claimed with a create-only write; history is read oldest first and paged past 1 MB [FDE2-1]
- [ ] Shared state uses conditional, versioned writes and consistent reads, and a lost race reloads instead of overwriting [FDE2-2]
- [ ] State is encoded through its types (enums stay enums) with a schema version, and was tested across processes [FDE2-2]
- [ ] Input is checked by a guardrail before it is written to long-term memory

## Evaluation and observability (`agent-evals-and-observability.md`)
- [ ] The eval set scores correctness against ground truth, not only reference-free style judges [FDE2-4]
- [ ] Every custom judge's scale was read and tried on a known wrong answer [FDE2-4]
- [ ] Dimensions you aren't tuning stay in the same batch, so a gain on one doesn't hide a loss on another [FDE2-4]
- [ ] The set has refusal cases, is large enough for the bar (10 of 10 can't show 0.80), and counts errored sessions as failures [FDE2-4]
- [ ] Each run uses one prompt version, recorded before and after [FDE2-4]
- [ ] The judge comes from a different model family than the agent [FDE2-4]
- [ ] Only one tracer provider exists, and logs are written inside the span so they carry the trace ID [FDE2-4, FDE2-6]
- [ ] Thread-pool work copies the trace context [FDE2-6]
- [ ] Telemetry writes can't fail the request, and dropped writes are counted [FDE2-6]
- [ ] Raw user queries and PHI are not in span attributes or logs; subjects are keyed hashes [FDE2-6, FDE2-7]
- [ ] `X-Amzn-Trace-Id` from untrusted callers is stripped [FDE2-7]
- [ ] Cost and token totals come from an unsampled ledger or `AWS/Bedrock` metrics, with the cache read and write split [FDE2-6]
- [ ] Cached prompt prefixes are byte-identical across calls [FDE2-6]
- [ ] Alarms are on rates with a minimum-traffic guard, and count filters default to 0 [FDE1-4, FDE2-6]

## Data stores for agents (`data-for-agents.md`)
- [ ] Each agent role has its own IAM role scoped to its own store, proved with the policy simulator [FDE2-1]
- [ ] Database tools run as the same read-only user the agent uses, not admin [FDE1-2]
- [ ] Lambda reaches Aurora through the Data API with a secret ARN per privilege level, never a password env var [FDE1-2]
- [ ] pgvector distance operators match the index operator class [FDE1-2]
- [ ] Generated Cypher passes `scripts/cypher_guard.py` and runs as a read-only identity [FDE2-1]
- [ ] Neo4j MERGE keys have uniqueness constraints before seeding [FDE2-1]
- [ ] Governed metrics come from an allow-list of parameterised SQL, not from SQL the model writes [FDE2-1]
- [ ] Athena workgroups set a per-query scan limit [3]
- [ ] Lake Formation data cell filters list allowed columns only and have a SELECT grant [FDE1-1]
- [ ] Ingest jobs read only their own date's prefix and upsert, so reruns don't duplicate rows [FDE1-1]

## AI services (`ai-services.md`)
- [ ] Each moderator's failure counts as "not checked", never as "clean", and the combined decision fails closed [4]
- [ ] Text is split by UTF-8 bytes to each service's limit before the call [4]
- [ ] Comprehend thresholds were tuned per label on labelled samples [4]
- [ ] PII handling is decided (block, redact or allow), and responses carry PII types without values [4]
- [ ] Exit codes for moderation can't be faked by a crash (0 ALLOW, 10 BLOCK, 11 REVIEW) [4]
- [ ] Async job names are derived from the input (key and ETag), so a repeated event can't start a second job [11]
- [ ] Transcripts and extracted text are treated as untrusted input to the model [11]
- [ ] Service roles passed to Textract, Personalize and Transcribe are scoped, and service-principal trust uses `aws:SourceArn` and `aws:SourceAccount` [13]
- [ ] Moderation and AI service endpoints have authentication [4]

## Streaming (`data-for-agents.md`)
- [ ] Firehose transforms return one record per input with base64 data, and undated records fail instead of landing in a junk partition [FDE1-5]
- [ ] Stream event source mappings use bisect on error, a retry limit, a maximum record age and an on-failure destination [FDE1-5]
- [ ] `ReportBatchItemFailures` is turned on wherever the handler returns partial failures [FDE1-5]
- [ ] Glue column types match the JSON values the stream sends [FDE1-5]
- [ ] Resources the task names use the exact name given [FDE1-5]

## Network and IAM
- [ ] A private function has an endpoint (or a route) for every service it calls [9]
- [ ] Interface endpoint security groups allow 443, and private DNS is on [9]
- [ ] Endpoint policies are scoped, and were checked separately from the IAM simulator [9]
- [ ] `aws:SourceVpce` restrictions are Deny statements with an admin exemption [19]
- [ ] No wildcard actions on `*` resources in function roles [9, 20]

## Async work and pipelines
- [ ] Every wait checks a real readiness condition and has a timeout and a failure exit [2, 3, 13, 22]
- [ ] Step Functions loops have `TimeoutSeconds`, Choices have a Default, and CreateEndpoint is followed by a DescribeEndpoint poll (`scripts/asl_lint.py` checks for a later Wait state) [22]
- [ ] Poll loops repeat only on "still working" states and exit on everything else [22]
- [ ] After a nested `ResultPath`, later states read the nested path [22]
- [ ] Training runs as a training job, with per-epoch evaluation and early stopping [22]
- [ ] Endpoint waits have a deadline, and OutOfService or Failed ends the wait [FDE2-3]
- [ ] A Lambda in front of a SageMaker endpoint caps input, keeps timeouts under the API limit, and maps errors to status codes [FDE2-3]

## Evaluation and cost
- [ ] Claimed improvements are larger than their noise, and say which way the metric moved [21]
- [ ] Reward and classifier labels are read by name [21]
- [ ] Hourly resources (recommenders, notebooks, endpoints) have an owner and a stop plan [13, 22]
