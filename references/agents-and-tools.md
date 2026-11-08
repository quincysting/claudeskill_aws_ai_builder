# Agents and tools

Lab tags in brackets name the lab that showed each rule: [8] is SimuLearn lab 8, [FDE1-3] is FDE1 lab 3, [FDE2-2] is FDE2 session 2. Their notes are under `D:\AI\AWSAI\` when that folder is on the machine; the rules stand on their own without it.

[FDE2-8] marks guidance from a practice exam (FDE2 session 8), kept only where the AWS or Strands docs in section 13 agree. A rule with no tag comes from those docs or from the aws-core skill.

For API detail, load `aws-core:amazon-bedrock` and read `agentcore-runtime.md`, `agentcore-gateway.md`, `agentcore-memory-observability.md`, `agents-and-action-groups.md` and `sdk-converse-api-python.md`. Other topics live in their own files: identity, JWT, token exchange and Cedar in `agent-security-and-identity.md`; tracing, evaluations and cost in `agent-evals-and-observability.md`; which store holds which agent data in `data-for-agents.md`; `guardrails.md`, `rag-and-vectors.md` and `models-and-prompts.md`.

## Contents

1. Pick the platform
2. The Converse tool loop
3. Tool contracts and tool overload
4. AgentCore Runtime
5. AgentCore Gateway
6. AgentCore Memory
7. Code Interpreter and Browser
8. Strands Agents
9. Protocols: MCP, A2A and AG-UI
10. Multi-agent patterns
11. Shared state and idempotency
12. Human approval
13. Doc sources

## 1. Pick the platform

| Need | Pick |
|---|---|
| One managed agent with action groups, a knowledge base and aliases, and no framework code | Bedrock Agents |
| Any framework or model, long sessions, MCP tools, your own control of identity, memory and observability | AgentCore: Runtime plus the services you need, each usable on its own |
| Agent code where the model picks the next step, with graphs when a path must be fixed | Strands Agents, hosted on AgentCore Runtime as in [8] and [FDE1-3] |
| A fixed, audited sequence with a model call in one step | Plain code or a state machine that calls Converse. [FDE2-2] kept the pipeline rule-based and called the model once, as a one-word judge |

- Existing Bedrock Agents can stay. AWS recommends AgentCore when you need another framework or model, or finer control of identity, memory and observability.
- Bedrock Agents keep the old configuration until you run `prepare-agent`, after any change to action groups, instructions, knowledge bases or the model. Action group parameters arrive as strings, so parse JSON values yourself.
- For Bedrock Agents multi-agent work, use the built-in supervisor and collaborator setup. A supervisor told to "send messages" to sub-agents invents an action group and loops on it.

## 2. The Converse tool loop

`stopReason` `tool_use` means: run the requested tools and call again (`models-and-prompts.md` covers the other stop reasons). Cap the calls, as Strands caps its own loops (section 8), and send failures back to the model:

```python
MAX_CALLS = 8  # a model that keeps asking for tools stops here

def run(client, model_id, system, text, tools, tool_config):
    messages = [{"role": "user", "content": [{"text": text}]}]
    for _ in range(MAX_CALLS):
        resp = client.converse(modelId=model_id, system=[{"text": system}], messages=messages,
                               toolConfig=tool_config, inferenceConfig={"maxTokens": 1000, "temperature": 0})
        msg = resp["output"]["message"]
        messages.append(msg)  # the assistant turn, with its toolUse blocks
        if resp["stopReason"] != "tool_use":
            return msg, resp["stopReason"]
        results = []
        for use in (b["toolUse"] for b in msg["content"] if "toolUse" in b):
            try:
                fn = tools[use["name"]]
                block, status = {"json": fn(**use["input"])}, "success"  # fn validates its own arguments
            except (KeyError, TypeError, ValueError) as e:
                block, status = {"text": f"tool error: {e}"}, "error"
            results.append({"toolResult": {"toolUseId": use["toolUseId"], "content": [block], "status": status}})
        messages.append({"role": "user", "content": results})  # one result per toolUseId
    raise RuntimeError(f"no final answer after {MAX_CALLS} model calls")
```

- `status: "error"` tells the model the call failed, and Nova then tries to correct its call. Only Nova and Claude 3 and 4 accept `status`, so leave it out for other models.
- Validate arguments inside the tool. In [8] a partial name matched another customer through `LIKE '%name%'`, and only the model stood between an invented shoe ID and an order.
- Return "not found" as an ordinary result the model can explain. An empty result made the agent tell the customer the system had failed [8].
- `toolChoice` `any` forces some tool call, and `tool` forces one named tool. The `ToolChoice` reference names only Claude 3 and Nova for these, so test a newer model before relying on it. Use them when the reply must be structured arguments.
- A Prompt Management ARN passed as `modelId` brings its own configuration: don't send `system`, `inferenceConfig` or `toolConfig` with it, pin a prompt version, and expect your messages to be appended after the prompt's.

## 3. Tool contracts and tool overload

- Keep the schema and the handler in step. Property names and JSON types must match what the handler reads: a handler that reads `arguments["sql"]` needs `sql` declared, and an `isinstance(x, int)` check needs `"type": "integer"` [FDE1-2]. Lab 8's `schema_problems()` diffs a gateway schema against the handler, because a mismatch fails only at run time [8].
- Test a Lambda MCP server with two events before you attach an agent: `{"method": "tools/list"}` and `{"method": "tools/call", "params": {"name": "...", "arguments": {...}}}` [FDE1-2].
- Run tools as the same database user the agent uses. Admin credentials hide the permission errors you are trying to find [FDE1-2].
- Strands `@tool` builds the tool spec from type hints and the docstring, so the docstring is the description the model reads [8, FDE2-8].
- Bind who and where from the request, never from tool arguments. [FDE2-1]'s memory tools take the user from the verified token's `sub` and the session from a header the app sets, and the model sees only `append_turn(content)` and `get_session()`. A model-chosen session ID had been a cross-session read path, and in [8] saying a name was enough to order as that person. Return refusals as tool errors (`isError: true` in MCP); a "Rejected:" prefix inside a success result is easy to miss [FDE2-1].
- With many tools, turn on semantic search when you create the gateway (`protocolConfiguration={"mcp": {"searchType": "SEMANTIC"}}`). The agent calls the built-in `x_amz_bedrock_agentcore_search` tool with the task and gets back only the matching tools, instead of listing all of them [FDE2-8]. The other cure is fewer tools per agent: give each specialist only its own (section 10).

## 4. AgentCore Runtime

Deploy shape:

- Ship a zip of code plus dependencies (direct code deployment) or an ARM64 container image. Both run on arm64, so build native packages for arm64. A zip can be 250 MB zipped and 750 MB unzipped.
- Zip from inside the folder, so the entry point sits at the zip root. `zip -r x.zip folder/` failed with "entrypoint could not be found or accessed" [FDE2-2].
- Nothing is installed at deploy time. Vendor dependencies into the folder (`pip install boto3 -t .`). A missing package showed up only as a `GetAgentCard` error, and the `ModuleNotFoundError` was in CloudWatch [FDE2-2].
- Pick the Python version the packages were built for. Lab 8's zip held 3.13 arm64 packages while the console defaulted to 3.14 [8].
- Set `serverProtocol` explicitly; the default is HTTP [FDE2-2]. The containers listen on `0.0.0.0`:

| Protocol | Port | Path |
|---|---|---|
| HTTP | 8080 | POST `/invocations`, GET `/ping` |
| MCP | 8000 | `/mcp` |
| A2A | 9000 | `/`, with the agent card at `/.well-known/agent-card.json` |
| AGUI | 8080 | `/invocations` for SSE, `/ws` for WebSocket. The configured protocol tells it apart from HTTP |

Versions, endpoints and settings:

- Creating a runtime makes version 1 and a DEFAULT endpoint. Every update makes a new version and moves DEFAULT to it, so wait for READY before you test [8, FDE1-3].
- `UpdateAgentRuntime` takes the artifact, role, network and protocol settings on every call. Resend all of them, environment variables included [FDE2-2]. Batch changes: [FDE1-3] made one orchestrator update with the client ID, every environment variable and VPC mode, and saved several redeploys.
- A caller that must not move with DEFAULT invokes a named endpoint pinned to a version (the `qualifier`). [FDE2-2] pinned one for a peer agent, and [FDE2-8] put free, pro and enterprise tiers on one runtime with one endpoint each.
- Settings go in environment variables (`GATEWAY_URL`, `KNOWLEDGE_BASE_ID`, `MODEL_ID`, `MEMORY_ID`). With `GATEWAY_URL` empty the agent had no tools [FDE1-3]. Secrets stay out of them, because `get-agent-runtime` shows the values.

Sessions:

```python
import json, uuid

session_id = str(uuid.uuid4())  # 33 to 256 characters; a uuid4 has 36. One per user conversation
resp = agentcore.invoke_agent_runtime(
    agentRuntimeArn="arn:aws:bedrock-agentcore:REGION:111122223333:runtime/agent-EXAMPLE",
    qualifier="prod", runtimeSessionId=session_id, payload=json.dumps({"prompt": text}).encode())
streamed = "text/event-stream" in resp.get("contentType", "")  # streamed replies arrive as data: lines [8]
```

- Each session ID gets its own microVM with isolated CPU, memory and filesystem. It stops after 15 minutes idle or 8 hours in total by default (`lifecycleConfiguration`: `idleRuntimeSessionTimeout`, `maxLifetime`). The next call with that ID gets new compute with nothing in memory, so keep state that must last in AgentCore Memory or a database. Only a configured session storage mount survives a stop.
- A call that arrives while its session is being started or stopped gets 409 `RetryableConflictException`. Retry it with a short backoff.
- For background work after the reply, answer `HealthyBusy` on `/ping` so the session counts as active.
- CPU is billed for active use, so model and tool I/O waits cost no CPU, but memory is billed while the session lives. A shorter idle timeout cuts that cost and adds cold starts.
- A new Strands `Agent` per request means no conversation memory. Every prompt in [8] repeated the customer's name for that reason.

Moving from EKS:

- Runtime takes over per-session isolation, spiky load without provisioning for the peak, and long multi-step sessions [FDE2-8].
- You change: ARM64 images, the protocol contract above, pod-local state moved to Memory or a database, and an inbound authorizer on every production runtime.
- In a VPC, create interface endpoints for `bedrock-agentcore`, `bedrock-agentcore.gateway`, `bedrock-runtime` and `logs`, plus the S3 gateway endpoint. Services without one (Cognito in that lab) need a NAT path, and AgentCore offers subnets only in its supported AZs [FDE1-3].

## 5. AgentCore Gateway

A gateway serves tools from many targets as MCP behind one endpoint, with one inbound authorizer and outbound credentials per target. The gateway's protocol is always MCP; OpenAPI, Lambda and the rest are target types [FDE2-8].

- Target types in the CDK README: Lambda, OpenAPI, Smithy model, API Gateway REST API stage and MCP server. The API's target configuration also has `http` and `inference` variants, so check the current list. Lambda targets authenticate with the gateway's IAM role only. OpenAPI targets need an API key or OAuth credential provider. Smithy targets cover built-in AWS service models, and the gateway role needs the AWS actions those tools call.
- Create the credential provider in AgentCore Identity first, then the target that references it. In the other order the target fails with "credential provider not found" [FDE2-8].
- The API key provider stores the key itself, in a Secrets Manager secret it manages. Don't create that secret yourself, rotate with `update-api-key-credential-provider`, and pass the key from an environment variable, never as a literal on the command line.

```python
ctl = boto3.client("bedrock-agentcore-control")
key_arn = ctl.create_api_key_credential_provider(
    name="tracking-api-key", apiKey=os.environ["TRACKING_API_KEY"])["credentialProviderArn"]
ctl.create_gateway_target(
    gatewayIdentifier="gw-EXAMPLE", name="tracking",
    targetConfiguration={"mcp": {"openApiSchema": {"s3": {"uri": "s3://amzn-s3-demo-bucket/tracking.json"}}}},
    credentialProviderConfigurations=[{"credentialProviderType": "API_KEY", "credentialProvider": {
        "apiKeyCredentialProvider": {"providerArn": key_arn, "credentialParameterName": "x-api-key",
                                     "credentialLocation": "HEADER"}}}])  # or QUERY_PARAMETER
```

- Inbound auth is IAM, with a SigV4-signed MCP client as in [8], or a JWT authorizer as in [FDE1-3]. Add a new client ID to `allowedClients` on every gateway and runtime that should accept it [FDE1-3].
- A Lambda target's event holds only the tool arguments. The tool name is in `context.client_context.custom["bedrockAgentCoreToolName"]`, and the handler returns plain JSON [8].
- Tool names are `<target>___<tool>`, and lab 8's handler stripped the prefix with `split("___", 1)[-1]` [8]. The tool-naming page agrees; the CDK README and one AWS blog show `__`, so check the name in a real call.
- A target that points at the wrong Lambda still shows Ready and answers with the wrong data. Check every target's ARN, and keep the existing target naming, because checks often match on names [FDE1-3].
- Update a target from a deep copy of the current one: change one field and send the whole config back, because `update_gateway_target` drops optional settings you leave out [8]. Keep boto3 recent, because an old one drops settings it doesn't know [8]. Poll until the status is READY [8]; the CLI lists READY, while aws-core's gateway procedure says ACTIVE. An update is refused while the target is `CREATE_PENDING_AUTH`, `UPDATE_PENDING_AUTH` or `SYNCHRONIZE_PENDING_AUTH`.
- The console's Lambda ARN autocomplete can join two values. Read the field back before you save [FDE1-3].
- Prove a change in the system of record (the database row, the order table), not from the agent's reply [8].

## 6. AgentCore Memory

- Short-term memory is events. Each `CreateEvent` names a `memoryId`, an `actorId` (whose memory) and a `sessionId` (which conversation). Conversational events (USER, ASSISTANT, TOOL) feed long-term extraction; blob events, such as checkpoints, don't.
- Agent code writes the events, through the SDK or a framework session manager given the memory ID (`MEMORY_ID` in [FDE1-3]). aws-core's memory reference says Runtime passes session IDs to Memory for you; the AgentCore guide has the agent write events. Check that events arrive.
- Long-term strategies (semantic, user preference, summary) extract records asynchronously, a minute or more after the events. Test by stating a preference, waiting, then asking in a new session. The memory page's observability panel tells "not wired up" from "extraction still pending" [FDE1-3].
- Each strategy's namespace must match exactly what the agent's session manager reads and writes [FDE1-3]. Start and end it with `/`, so `/actors/alice/` can't prefix-match another actor. Use `namespaceTemplates`; `namespaces` is the legacy field.

```python
ctl.create_memory(name="support_memory", eventExpiryDuration=30, memoryStrategies=[
    {"semanticMemoryStrategy": {"name": "facts", "namespaceTemplates": ["/app/user/{actorId}/facts/"]}},
    {"userPreferenceMemoryStrategy": {"name": "prefs", "namespaceTemplates": ["/app/user/{actorId}/prefs/"]}},
    {"summaryMemoryStrategy": {"name": "summary",
                               "namespaceTemplates": ["/app/user/{actorId}/session/{sessionId}/summary/"]}}])
```

- `eventExpiryDuration` (days, up to 365) is fixed on each event when it is written; raising it later doesn't extend older events. Long-term records never expire unless the strategy sets `recordExpiryDurationDays` (31 at least).
- Short-term ingestion and retrieval are billed per GB, with every event counted as at least 12 KB and at most 64 KB. Many one-line events cost more than a few events that each carry several turns, so batch turns into one event, at the price of a short delay before the newest turns are stored [FDE2-8]. The pricing page confirms per-GB billing; the exam's per-event prices are out of date. Long-term memory is billed per stored record and per retrieval request.
- For your own DynamoDB memory table (key layout, read order, claiming turn IDs, paging), see `data-for-agents.md` [FDE2-1].

## 7. Code Interpreter and Browser

- Code Interpreter runs code the agent writes, in its own sessions; Runtime runs the agent. "Run Python on lab results" maps to Code Interpreter [FDE2-8].
- A custom Code Interpreter sets the network mode (sandbox, public or VPC) and an execution role that bounds what the code can reach. Behind VPC endpoints, give that role the service permissions, or calls fail with AccessDenied or "Unable to locate credentials". Its sessions time out after 15 minutes by default; stop each one when you are done to stop its charges.
- Browser recording is a setting of a custom browser: `recording` with an S3 bucket and prefix, plus an execution role, at create time. Security Hub control BedrockAgentCore.6 flags custom browsers without it. Use recordings for the browsing itself and CloudTrail for control-plane calls [FDE2-8].
- Keep browsing and sensitive data apart: separate agents with their own roles, and a sandbox or VPC interpreter for the sensitive code. A page that injects instructions then reaches only the browser agent's permissions [FDE2-8].

## 8. Strands Agents

[8]'s agent on Runtime, shortened: tools come from the gateway through a SigV4-signed MCP client, plus local `@tool` functions.

```python
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from mcp_proxy_for_aws.client import aws_iam_streamablehttp_client
from strands import Agent
from strands.tools.mcp.mcp_client import MCPClient

app = BedrockAgentCoreApp()
gateway = MCPClient(lambda: aws_iam_streamablehttp_client(
    endpoint=GATEWAY_URL, aws_region="REGION", aws_service="bedrock-agentcore"))  # URL from an env var

@app.entrypoint
def invoke(payload):
    with gateway:
        agent = Agent(model=model, system_prompt=SYSTEM, tools=gateway.list_tools_sync() + [search_policies])
        return {"result": strip_thinking(str(agent(payload.get("prompt", ""))))}
```

- Agents as tools: pass specialist agents in `tools`, or use `.as_tool(name=..., description=...)` to set what the orchestrator reads. Each call resets the specialist's context unless you pass `preserve_context=True`. `delegate=True` returns the specialist's answer to the user unchanged and skips a second orchestrator call, but a delegated tool must be the only tool called that turn. Set `callback_handler=None` on specialists so their output isn't printed twice.
- Swarm: agents hand off to each other with no fixed path. The Python defaults are 20 handoffs, 20 iterations, 900 s in total and 300 s per agent. Ping-pong detection is off until you set both `repetitive_handoff_detection_window` and `repetitive_handoff_min_unique_agents`.
- Graph: edges fix the order, edges can carry conditions, and nodes can be agents, nested swarms or graphs, or remote A2A agents. A cycle needs `set_max_node_executions`, `set_execution_timeout` and an explicit entry point. Pick a graph when the path must be auditable [FDE2-8].

```python
from strands import Agent
from strands.multiagent import Swarm

billing = Agent(name="billing", description="Charges, refunds, invoices.", system_prompt="...", callback_handler=None)
network = Agent(name="network", description="Connectivity faults.", system_prompt="...", callback_handler=None)
router = Agent(system_prompt="Send each question to one specialist, or answer it yourself.",
               tools=[billing.as_tool(), network.as_tool(preserve_context=True)])
triage = Swarm([intake, diagnose], max_handoffs=6, execution_timeout=300.0,  # two more Agents like billing
               repetitive_handoff_detection_window=4, repetitive_handoff_min_unique_agents=2)
```

## 9. Protocols: MCP, A2A and AG-UI

| Protocol | Connects | Use it for |
|---|---|---|
| MCP | An agent to tools and data | Tools that any MCP client can call. Gateway serves every target this way |
| A2A | An agent to another agent | Agents from other teams or frameworks that discover each other through agent cards and exchange tasks |
| AG-UI | An agent to a user interface | Streaming agent events, shared state and approval prompts to a front end |

- Use each protocol for its own link: MCP has no agent discovery, task lifecycle or UI stream [FDE2-8].
- An agent card has no runtime ARN. A caller that invokes a peer gets the ARN and qualifier from configuration, and checks the card's capability and version [FDE2-2].
- On Runtime, A2A errors come back with a real HTTP status (400, 404, 409, 424, 429) and a JSON-RPC error body, so parse the body even on non-2xx: -32053 for throttling, -32055 for a runtime client error (HTTP 424), whose cause is in CloudWatch. Only access denied (HTTP 403) has no JSON-RPC body.
- With OAuth inbound auth, call `InvokeAgentRuntime` over HTTPS with the bearer token; the AWS SDK can't make that call.

## 10. Multi-agent patterns

- Split into several agents when the work has distinct domains, each with its own tools and permissions [FDE2-8].
- Supervisor, or agents as tools: an orchestrator routes each request to a specialist and combines the answers. Escalation to a person is one more tool [FDE2-8] (Strands docs).
- Orchestrator-mediated pipeline [FDE2-2]: an explicit state machine, a checkpoint after every phase, and a rollback to the first affected phase (new documents went back to extraction, not intake).
- Give each agent only the fields it needs, and check that before every call, peer calls included [FDE2-2].
- Discover agents by capability with a minimum version, never by hardcoded name. No match raises `AgentNotFoundError`, and only older versions raise `AgentVersionMismatchError` [FDE2-2].
- Retry with backoff behind one circuit breaker per agent, so one failing agent doesn't block the others. When retries run out, the request goes to manual review if the breaker is open and to a dead-letter table if it is closed. Nothing is dropped [FDE2-2].
- A direct peer call is an exception with fences [FDE2-2]:
  - only on one branch (insufficient evidence), and exactly one attempt;
  - the peer's ARN and qualifier from environment variables, with a malformed value failing the container at startup;
  - a payload of IDs only, checked by value, so notes and names can't ride along;
  - a status of exactly `not_needed`, `called` or `failed`, with the failure reason logged;
  - an audit entry marked `initiated_by: agent`;
  - deploy the callee first, then the caller with the callee's ARN.

```python
_ID = re.compile(r"[A-Za-z0-9_.-]{1,64}")

def scoped_payload(request_id: str, required: list[str], missing: list[str]) -> dict:
    for value in [request_id, *required, *missing]:
        if not isinstance(value, str) or not _ID.fullmatch(value):
            raise ValueError(f"IDs only; refused a {type(value).__name__} of length {len(str(value))}")  # no echo
    return {"request_id": request_id, "required_docs_list": required, "missing_documents": missing}
```

- Each Runtime container holds its own copy of shared types. A field added to the shared copy and not the container's surfaced as a `TypeError` in CloudWatch [FDE2-2].

## 11. Shared state and idempotency

- Write shared state with conditional, versioned writes. Create with `attribute_not_exists`, update only where `version = :expected` and store `version + 1`. A lost race means reload and decide again, never overwrite. Read with `ConsistentRead`, scans included. Only `ConditionalCheckFailedException` means another writer; throttling is a different error [FDE2-2].
- `scripts/dynamodb_state.py` does this for checkpoints (PK `request_id`) and an agent-card registry (PK `agent_name`). Run its tests with `python -m unittest discover -s scripts`.

```python
from dynamodb_state import CheckpointStore, StaleCheckpointError, dynamodb_table

store = CheckpointStore(dynamodb_table("CheckpointTable"), Checkpoint)  # Checkpoint: your dataclass
version = store.save(cp)                      # create; fails if the request already has one
cp, version = store.load("REQ-1")
cp.state = State.PEND
try:
    store.save(cp, expected_version=version)  # fails if anyone wrote since the load
except StaleCheckpointError:
    ...                                       # reload and decide again
```

- Encode state through its type hints. Plain JSON turned an enum into the string "met", and enum comparisons silently failed after a resume. Store a schema version and refuse rows this code can't read [FDE2-2].
- Test across processes and threads. An in-memory dict passed a same-process test; the real test wrote in one process and read in another, and raced 8 writers for one winner [FDE2-2].
- The registry's `register()` is still a plain upsert, so a stale deployer can overwrite a newer card [FDE2-2].
- Write tools check and change in one statement (`UPDATE ... SET n = n - 1 WHERE id = %s AND n > 0`, then check `rowcount`). A duplicate check placed before the update let 8 of 8 overlapping orders through; after the update, as a locking read, it let one through [8].
- "Same item, customer and day" is only a guess at a retry, and it refuses a real second order. The fix is an idempotency key from the caller with a unique constraint; lab 8 didn't build it [8].

## 12. Human approval

- Strands: raise an interrupt from a `BeforeToolCallEvent` hook for the risky tool. The agent stops with `stop_reason == "interrupt"`, and you resume it with one `interruptResponse` per interrupt. A session manager keeps the interrupt so a person can answer later.

```python
from strands.hooks import BeforeToolCallEvent, HookProvider, HookRegistry

class ApproveRefunds(HookProvider):
    def register_hooks(self, registry: HookRegistry, **kwargs) -> None:
        registry.add_callback(BeforeToolCallEvent, self.check)

    def check(self, event: BeforeToolCallEvent) -> None:
        if event.tool_use["name"] == "issue_refund":
            answer = event.interrupt("refund-approval", reason={"input": event.tool_use["input"]})
            if answer != "approve":
                event.cancel_tool = "A reviewer declined this refund"

result = agent(prompt)
while result.stop_reason == "interrupt":
    result = agent([{"interruptResponse": {"interruptId": i.id, "response": ask_reviewer(i.reason)}}
                    for i in result.interrupts])
```

- Bedrock Agents: turn on user confirmation for actions that change data. A declined action becomes context, and the agent tries another way. With return of control (`RETURN_CONTROL`), your app runs the action and sends `returnControlInvocationResults` with the `invocationId` in `sessionState`; `inputText` is ignored on that call.
- A front end on AG-UI can carry the approval prompt: the agent pauses until the user answers.
- When a model verdict can't be parsed, ask once more, then send the case to a person. Never turn an unreadable or failed reply into a decision, least of all a denial [FDE2-2].
- Binding an approval to the exact action and its arguments is in `agent-security-and-identity.md`.

## 13. Doc sources

Read for this file, November 2026 (`https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/` is shortened to `devguide/`):

- Runtime: `devguide/runtime-sessions.html`, `runtime-how-it-works.html`, `runtime-lifecycle-settings.html`, `runtime-configure-invocation.html`, `runtime-http-protocol-contract.html`, `runtime-a2a.html`, `runtime-agui.html`, `runtime-get-started-code-deploy-node.html`, `harness-operations.html`; API reference `InvokeAgentRuntime` and `UpdateAgentRuntime`
- Gateway: `devguide/gateway-tool-naming.html`, `gateway-using-mcp-semantic-search.html`; the CDK `aws_bedrockagentcore` README; the CLI `update-gateway-target` page; the .NET `UpdateGatewayTarget` page; the AWS ML blog "Introducing Amazon Bedrock AgentCore Gateway"
- Memory: `devguide/memory-organization.html`, `memory-create-a-memory-store.html`, `using-memory-short-term.html`; boto3 `SemanticMemoryStrategyInput`; `https://aws.amazon.com/bedrock/agentcore/pricing/`; the AWS ML blog "Amazon Bedrock AgentCore Memory: Building context-aware agents"
- Tools: `devguide/code-interpreter-resource-session-management.html`, `agentcore-vpc.html`, `browser-session-recording.html`; Security Hub AgentCore controls
- Converse and Bedrock Agents: Nova `tool-use-results.html`; boto3 `ToolResultBlock`; the Bedrock Runtime `ToolChoice` reference; Bedrock `agents-userconfirmation.html`, `agents-returncontrol.html`, `agents-session-state.html`; `https://aws.amazon.com/bedrock/faqs/`
- Strands: `strandsagents.com/docs/user-guide/sdk/multi-agent/` pages `agents-as-tools`, `swarm` and `graph`, and `sdk/interrupts`
