# Serverless front ends, write APIs and waits

Lab tags in brackets name the lab that showed each rule: [16] is SimuLearn lab 16, [FDE1-3] is FDE1 lab 3, [FDE2-2] is FDE2 session 2. The lab notes aren't part of this skill; the rules stand on their own without them.

## Contents

- The usual shape, and the CORS-aware response helper
- Hardening list: twelve checks for a build or a review
- Request and response contract: status codes, bodies, the page's side
- CORS and gateway responses
- Who can call it: authorizers, claims, abuse limits, the role
- Logs and alarms
- Timeouts and size limits
- Static site and CDN
- APIs that replace the whole document
- Long-running operations
- Containers behind an ALB

## The usual shape

CloudFront and S3 page → API Gateway (REST, Lambda proxy) → Lambda → Bedrock. It appeared in labs 4, 5, 6, 7, 10, 12, 18 and 21, and the same gaps came up every time.

`scripts/summarize_api.py` is a working handler for this shape: input checks, one Converse call, CORS on every response, and 200/400/429/500/502 status codes. It maps every model failure other than throttling to 502. Add 503 and 504 cases from `models-and-prompts.md` if callers need to tell them apart.

```python
import json, os

def response(status: int, payload: dict) -> dict:
    # Proxy integration: the Lambda must send the CORS headers itself, errors included.
    headers = {"Content-Type": "application/json"}
    origin = os.environ.get("ALLOW_ORIGIN", "").strip()   # your site, never "*"
    if origin:   # unset: no CORS headers, so other origins can't read replies (fails closed)
        headers.update({"Access-Control-Allow-Origin": origin,
                        "Access-Control-Allow-Headers": "Content-Type,Authorization",
                        "Access-Control-Allow-Methods": "OPTIONS,POST"})
    return {"statusCode": status, "headers": headers, "body": json.dumps(payload)}  # an object, not json.dumps(str)
```

## Hardening list

Check each item when you build or review one of these apps:

1. Every method except OPTIONS uses an authorizer. Browsers send the preflight without the `Authorization` header, so an authorizer on OPTIONS breaks CORS. Lab 12 created a Cognito authorizer, then left every method at `AuthorizationType: NONE` [12].
2. CORS allows your origin only. OPTIONS gets a response, from the Lambda or a mock integration. API Gateway's own 4XX and 5XX gateway responses carry the CORS headers too [4, 5, 7, 10, 12, 18].
3. Bad input gets a 400 before any model call. Throttling gets 429 or 503, and an unusable model reply gets 502. Never send an error with HTTP 200, a bare 500, or a double-encoded body [5, 7, 10, 12, 18].
4. Logs hold metadata only (status, latency, error code, token counts). No prompts, rows, PII or caller IPs. Log groups have a retention period. API Gateway data tracing is off [4, 5, 6, 7, 9, 10, 11]. Lambda's default log format is plain text; level filtering needs `LoggingConfig` with `LogFormat=JSON` and an `ApplicationLogLevel`. In text format, set the Python logger's level yourself, or INFO lines can be missing [11].
5. The Lambda timeout sits just above the call budget, which itself stays under the API's integration timeout. That is 29 s by default for REST APIs. Regional and private REST APIs can raise it with a quota increase, which can lower the account throttle quota; edge-optimized APIs can't, and HTTP APIs stop at 30 s. The labs set 2 to 5 minutes, which let a stuck call run and bill long after the gateway had given up [5, 6, 7].
6. The server caps input size and chooses `maxTokens` [5].
7. The role allows only the models, tables and keys the function uses [6, 9, 20].
8. The page escapes model output, and CDN scripts have SRI hashes [4, 21].
9. S3 behind CloudFront uses OAC rather than OAI. Invalidate `/*` after each upload [21].
10. Secrets come from Secrets Manager, not plain env vars [9, 14].
11. Stage and method throttling, and usage plans with API keys (REST APIs), turn excess traffic into 429s before it costs model calls. Public REST APIs sit behind WAF.
12. Alarms on Lambda Errors, Throttles, Duration p99 and ConcurrentExecutions. X-Ray tracing on. Async invocations have a DLQ or on-failure destination.

## Request and response contract

| Case | Status | Body | Lab |
|---|---|---|---|
| Bad JSON, a JSON array, or missing, blank or oversized input | 400 | The reason; no model call | 5, 6, 10, 18 |
| A setting missing or still a placeholder | 500 | Names the setting, not its value | 10, 18 |
| Throttled or busy model | 429 or 503 | "Busy, try again shortly" | 6, 10 |
| Model timed out | 503 or 504 | A generic message | 7, 10 |
| Model failed, or the reply is empty or unparseable | 502 | A generic message | 10, 18 |
| Reply cut off | 200 | The text, plus `truncated: true` or the stop reason | 5, 7 |
| Guardrail blocked | 200 | `{"output": ..., "blocked": true}` | 15 |

- Serialise the body once, from a dict. Lab 18 returned `json.dumps(answer_string)`, so its page showed the raw quoted text with `\n` escapes [18]. A body that isn't a string at all gets a 502 from API Gateway (aws-core `api-gateway.md`).
- Catch every exception in the handler. An unhandled error reached lab 18's page as a bare 500 with `{"message": "Internal server error"}` and no hint of the cause [18].
- An error that still returns 200 is worse. Lab 12's missing function came back as 200 with `{"error": "name 'get_nova_llm' is not defined"}`, so API metrics and the page both reported success [12].
- Answer OPTIONS before reading the body: a preflight that reaches the function has no body, and lab 18's handler would have crashed on it [18]. Decode `isBase64Encoded` bodies or reject them with 400, and accept a body that a console test event passes already parsed [5, 18].
- Probe an API with bad requests before you trust it. Four bad requests found lab 5's 200-on-error and crash defects in seconds. Lab 6's API answered 500 to four cases and 200 to three that should all have been 400 [5, 6]. Send one good request too, because an API that answers 400 to everything passes a rejection-only probe [6].
- The page must check the status. Lab 7's page showed error JSON as if it were the answer, and lab 10's page only wrote failures to the console [7, 10]:

```js
const r = await fetch(API_URL, {method: 'POST',
  headers: {'Content-Type': 'application/json', Authorization: idToken},
  body: JSON.stringify({inputs: text})});
const data = await r.json().catch(() => ({}));
if (!r.ok) { statusLine.textContent = data.error || `Request failed (${r.status})`; return; }
output.textContent = data.summary;                       // textContent: model text is never markup
if (data.stop_reason === 'max_tokens') statusLine.textContent = 'The answer was cut off.';
```

- Read the reply with `response.json()`. Lab 18's page removed every `"` before calling `JSON.parse`, which breaks any JSON object [18].

## CORS and gateway responses

- API Gateway answers some requests itself: authorizer denials, throttling 429s, integration timeouts, and 403 "Missing Authentication Token" for a path that doesn't exist. Without CORS headers on those gateway responses, the browser shows a CORS error and hides the real status (aws-core `api-gateway.md`). In lab 4 "Missing Authentication Token" meant a wrong path, and in lab 6 a GET got API Gateway's 403 before the function ran [4, 6].

```yaml
Default4xx:
  Type: AWS::ApiGateway::GatewayResponse
  Properties:
    RestApiId: !Ref Api
    ResponseType: DEFAULT_4XX          # add a DEFAULT_5XX resource the same way
    ResponseParameters:
      gatewayresponse.header.Access-Control-Allow-Origin: "'https://app.example.com'"
      gatewayresponse.header.Access-Control-Allow-Headers: "'Content-Type,Authorization'"
```

- The inner single quotes mark static values. Redeploy the stage after any CORS change (aws-core `api-gateway.md`).
- `Access-Control-Allow-Origin` takes one origin, not a list, and `*` doesn't work for requests with credentials [18] (aws-core `api-gateway.md`).
- Binary media types set to `*/*` break OPTIONS with a 502 (aws-core `api-gateway.md`).
- With `ALLOW_ORIGIN` unset, the bundled handlers send no CORS headers, which blocks a page on another origin. Set it for any page served from CloudFront or Amplify [10, 18].
- A raw `AWS::ApiGateway::RestApi` needs, per path, an OPTIONS mock, a POST method, a Lambda permission and a shared deployment. A SAM `AWS::Serverless::Api` with `Events: Api` on each function produces the same in far less YAML [12].

## Who can call it

```yaml
# On every method except OPTIONS (lab 12 had NONE next to an unused authorizer):
AuthorizationType: COGNITO_USER_POOLS
AuthorizerId: !Ref ApiCognitoAuthorizer
```

```python
claims = ((event.get("requestContext") or {}).get("authorizer") or {}).get("claims") or {}
user_id = claims.get("sub")   # None: no sign-in, so answer generically or return 401
```

- Take the caller and their profile from the authorizer's claims, never from the body or a hard-coded record. Send the ID token, which carries identity claims. Users can change their own custom attributes unless you remove them from the app client's write attributes, so treat claim values as untrusted text [6].
- An open model endpoint is a free model on your bill. Labs 5, 6 and 7 had authorization NONE, no API key, no WAF and a stage limit of 10,000 requests per second, and lab 10's quiz API answered a request about pasta [5, 6, 7, 10]. Fix the task on the server (system prompt, allow-listed models, capped `maxTokens` and input), and name the route for what it does: lab 5's `/summarize` took any prompt [5, 12].
- Cap what one function can spend at once with reserved concurrency. Lab 10 proposed `aws lambda put-function-concurrency --function-name FUNCTION --reserved-concurrent-executions 5` (not run in the lab), because one request can cost two model calls when the handler retries [10].
- Use a TLS 1.2 security policy. Lab 5's API allowed `TLS_1_0` [5].
- Generate secret values and store them as JSON. Lab 12's secret started with the user pool client ID, which is public in the page's `main.js`, and was written as `str(dict)` [12].
- Lab 6's function role could rewrite its own code (`lambda:UpdateFunctionCode`) and call any Bedrock model and any SageMaker endpoint. A role for one model needs little more than this, plus writing its own logs [6]. For a cross-Region profile, see `network-and-iam.md`.

```json
{"Effect": "Allow", "Action": "bedrock:InvokeModel",
 "Resource": "arn:aws:bedrock:REGION::foundation-model/amazon.nova-pro-v1:0"}
```

## Logs and alarms

- What the labs logged: the whole API Gateway event (source IP, `X-Forwarded-For`, user agent, origin and raw prompt) in labs 5, 6, 7 and 10; a hard-coded persona in 7 of 8 model-input lines in lab 6; 30 customer rows in lab 9; a caption with a phone number and address in lab 4. Every one went to a log group that never expires [4, 5, 6, 7, 9, 10].
- Keep botocore's logger at INFO or above. At DEBUG it logs request bodies, prompts included [6].
- One metadata line per call is enough to debug with. Lab 10's, with example values:

```text
quiz faq=sample questions=4 attempts=1 stop_reason=end_turn input_tokens=600 output_tokens=320
```

```bash
aws apigateway update-stage --rest-api-id API_ID --stage-name prod \
  --patch-operations 'op=replace,path=/*/*/logging/dataTrace,value=false'
aws logs put-retention-policy --log-group-name /aws/lambda/FUNCTION --retention-in-days 30
aws lambda update-function-configuration --function-name FUNCTION \
  --logging-config LogFormat=JSON,ApplicationLogLevel=INFO
```

- The first two commands are lab 10's, not run in the lab [10]. The third turns on JSON logs with level filtering (Lambda developer guide). Sending only `--logging-config` leaves the env vars alone; the full replace applies when you send `Environment`.
- Alarm on a rate for silent failures. A count fires falsely as traffic grows and misses incidents in quiet hours. FDE2-6 alarmed on `100 * FILL(m1, 0) / m2 > 10` over 5 minutes, from two metric filters: bad turns and all turns [FDE2-6].
- An error returned with HTTP 200 never reaches a 5XX alarm, which is one more reason to return real codes [12].

## Timeouts and size limits

| Limit | Value | Lab or source |
|---|---|---|
| REST API integration timeout | 29 s by default; Regional and private APIs can go higher by quota increase | 5, aws-core `api-gateway.md` |
| HTTP API integration timeout | 30 s, fixed | aws-core `api-gateway.md` |
| API Gateway payload | 10 MB | aws-core `api-gateway.md` |
| Lambda synchronous request and response | 6 MB each | aws-core `lambda.md` |
| Lambda env vars | 4 KB in total | aws-core `lambda.md` |
| Lambda timeouts the labs used | 2 minutes (lab 5), 5 minutes (labs 6, 7, 10), 10 minutes (lab 21) | 5, 6, 7, 10, 21 |

- Set the Lambda timeout from the call budget plus a few seconds, and the SDK timeouts below it, so a slow call fails with a named error instead of a generic timeout (aws-core `production.md`). Lab 9 fit 56 s of calls inside a 60 s function timeout [9].
- Count the cold start. With LangChain loaded, lab 7's function took about 1.3 s to start, and lab 12 kept 3 s of the 29 s for the cold start and the handler [7, 12].
- Work that can't finish in time belongs in a job: return an ID and let the page poll, or use Step Functions. Lab 10 capped quizzes at 6 questions for this reason [10]. For token streaming, REST APIs support response streaming through a proxy integration and HTTP APIs don't (aws-core `api-gateway.md`).

## Static site and CDN

- An S3 website endpoint serves HTTP only. Lab 1's diagram said https and the console gave an `http://` URL. Put CloudFront in front for HTTPS [1].
- The website endpoint needs public read, which takes two settings: Block Public Access must allow public policies, and the bucket policy must grant `s3:GetObject` on `bucket/*`. That makes every object public, so keep private data out of that bucket. Behind CloudFront with OAC, the bucket can stay private [1, 21].
- The index document is a suffix that applies in every folder: `docs/` serves `docs/waves.html` too [1].
- S3 has no rename. Copy to the new key, switch the website config, then delete the old key, so the root page never returns 404 in between. With versioning off, the delete is permanent [1].
- With the CachingOptimized policy, CloudFront can serve an old `index.html` until it expires. Lab 21's `/*` invalidation completed within a couple of minutes [21]. A wildcard path counts as one invalidation path, and the first 1,000 paths each month are free (CloudFront developer guide).
- Before the first upload, lab 21's distribution returned S3's `NoSuchKey` XML for the default root object [21].
- Check the API URL in the page before you upload it. Lab 21's stage URL without the `/results` route would have been accepted and failed only in the browser [21].
- Pin CDN scripts by hash, and render model text as text. Lab 21's table showed model output without `DataTable.render.text()`, and the stored summaries already held markup-like tokens such as `<pad>` and `</s>` [21].

```html
<script src="https://cdn.example.com/lib/1.2.3/lib.min.js"
        integrity="sha384-HASH_OF_THAT_EXACT_FILE" crossorigin="anonymous"></script>
```

- Amplify Hosting can deploy a built site from a zip, with no Git repository. The `zipUploadUrl` is presigned, so keep it out of notes and logs [12]:

```bash
DEPLOY=$(aws amplify create-deployment --app-id "$APP_ID" --branch-name main)
JOB_ID=$(jq -r .jobId <<<"$DEPLOY"); URL=$(jq -r .zipUploadUrl <<<"$DEPLOY")
(cd dist && zip -qr /tmp/site.zip .) && curl -sT /tmp/site.zip "$URL"
aws amplify start-deployment --app-id "$APP_ID" --branch-name main --job-id "$JOB_ID"
```

## APIs that replace the whole document

These calls replace everything you don't send. Read, merge, write, then wait:

| API | Trap | Lab |
|---|---|---|
| S3 `PutBucketPolicy`, `PutBucketWebsite` | Replaces the whole policy or website config | 1 |
| Lambda `UpdateFunctionConfiguration` | Replaces every env var. Pass `RevisionId`, then use the `function_updated` waiter | 5, 7 |
| Bedrock `update_guardrail` | Replaces every policy (see `guardrails.md`) | 14, 15 |
| AgentCore `update_gateway_target` | Drops optional settings you leave out | 8 |
| ECS `register_task_definition` | Rejects describe-only fields (`taskDefinitionArn`, `revision`, `status`, `requiresAttributes`, `compatibilities`, `registeredAt` and others). A changed `environment` value needs a new revision and `update_service(forceNewDeployment=True)` | 14 |

The Lambda docs state the full replace. For the others, the request takes the whole document and the labs saw settings disappear, but the docs don't spell it out.

For the ECS case, read the task definition, drop the describe-only fields, change the container's `environment`, register it, then `update_service(taskDefinition=new_arn, forceNewDeployment=True)`. If you have the SimuLearn lab 14 code, its `teaching_guardrail.py` has this as `roll_out_env`. Values that come from `environmentFiles` or `secrets` need only a forced new deployment, without a new revision.

- For S3 website config, keep `ErrorDocument` and `RoutingRules` from `get_bucket_website` when you change the index document [1].
- If a Lambda config read returns `Environment.Error` and no `Variables`, Lambda couldn't decrypt them. Stop there: merging into an empty map would delete them all [5].
- `describe_task_definition` returns tags only with `include=["TAGS"]`, so ask for them if the new revision must keep them [14].

## Long-running operations

"Created" and "Running" don't mean ready. Every wait needs a condition, a timeout and a failure exit:

| Operation | Ready when | Lab |
|---|---|---|
| EC2 user data | Your app answers. "Running" comes before user data finishes, and console status checks lag behind. Use `systemctl enable --now` so the service survives a reboot | 2 |
| Glue crawler | State READY and `LastCrawl.StartTime` later than your start; then check `LastCrawl.Status` | 3 |
| Lambda code update | `CodeSha256` matches your zip and `LastUpdateStatus` is Successful | 6 |
| Guardrail update | READY with `updatedAt` at least your update's | 14 |
| Personalize import or training | Status ACTIVE; imports took about 300 s regardless of size | 13 |
| KB ingestion | The job is COMPLETE, or a real Retrieve returns your documents | 16 |
| SageMaker endpoint | `DescribeEndpoint` returns InService. CreateEndpoint has no `.sync` | 22 |
| ECS service update | `services_stable` waiter. Lab 14's new task was registered about 90 s after the update | 14 |

Use `scripts/asl_lint.py` on Step Functions definitions. It flags poll loops without `TimeoutSeconds` and CreateEndpoint with no wait after it. `training-and-pipelines.md` has a definition that passes.

`update_function_code` returns while the update is still `InProgress`, so a test straight after it can run the old code [6]. Prove which package is live:

```python
import base64, hashlib, io, zipfile
from pathlib import Path

source = Path("lambda_function.py").read_bytes()    # bytes: Windows newline conversion can't change the hash
compile(source, "lambda_function.py", "exec")        # a syntax error stops here, before the upload
buf = io.BytesIO()
with zipfile.ZipFile(buf, "w") as z:
    info = zipfile.ZipInfo("lambda_function.py", date_time=(2020, 1, 1, 0, 0, 0))   # fixed time: same bytes, same hash
    info.external_attr = 0o644 << 16
    z.writestr(info, source)
package = buf.getvalue()
out = lam.update_function_code(FunctionName=name, ZipFile=package)
assert out["CodeSha256"] == base64.b64encode(hashlib.sha256(package).digest()).decode()
lam.get_waiter("function_updated").wait(FunctionName=name)
```

- The console's Code properties panel shows the same SHA-256, so a config-only change can be shown to leave the code alone [5, 6].
- After the upload, check that the function's handler setting names a module in the package you sent. Lab 6's deploy step failed when it didn't, but by then the old code was already replaced, so recovery meant deploying a matching file [6].

## Containers behind an ALB

- Health check defaults for instance and IP targets are a 30 s interval, a 5 s timeout, healthy threshold 5 and unhealthy threshold 2. A dead target is out in about 1 minute. A new target needs one passing check, and the healthy threshold applies when an unhealthy target recovers (150 s by default). The lab's settings took up to 5 minutes to notice a dead task. Set them on purpose, and set `healthCheckGracePeriodSeconds` on the service for slow starters [20].
- Fargate tasks in private subnets need the `logs` endpoint (plus ECR endpoints and the S3 gateway to pull images, and Secrets Manager or SSM endpoints for secrets). The task security group must allow traffic from the ALB's security group.
- A task in a public subnet with no NAT gateway or ECR endpoints needs a public IP to pull its image [20].
- Use separate task and execution roles, send container logs to CloudWatch, and pin the base image rather than `:latest` [20]. Lab 20's image built on full `python:3.9`, past end of life, and came to 545.6 MB after a 3 min 6 s build; its Dockerfile comment claimed a slim base. Use a supported slim base, pin `requirements.txt`, and deploy by digest or version tag [20].
- The ECS console preselects private subnets and the default security group. Check both [20]. It also pre-fills a generated service name and builds a CloudFormation stack for the service, ALB, listener and target group [20].
- Serve over HTTPS. The lab ALBs listened on HTTP only [14, 20]. Put an ACM certificate on a 443 listener, and Cognito or OIDC authentication on the listener [20].
- When the browser shows an error page for a plain-HTTP ALB, check with `curl` first. In labs 14 and 20 the app returned 200 while Chrome showed a warning or error page [14, 20].
