# AWS AI services next to Bedrock

Lab tags in brackets name the lab that showed each rule: [4] is SimuLearn lab 4, [FDE1-5] is FDE1 lab 5, [FDE2-1] is FDE2 session 1. Their notes are under `D:\AI\AWSAI\` when that folder is on the machine; the rules stand on their own without it.

Contents: pick the service before the model, Region and feature checks, async jobs, size and page limits, moderation that fails closed, Textract, Transcribe, Polly and Translate, Personalize, Kendra or Bedrock Knowledge Bases, Lex, IAM and service roles, cost traps, doc sources. The Transcribe S3 trigger rules are in `rag-and-vectors.md`, guardrail rules in `guardrails.md`, and the API front end in `serverless-apps.md`. Untagged rules come from the docs listed at the end. For API detail, load `aws-core:aws-sdk-python-usage` or the service docs.

## Pick the service before the model

| Job | Service | Why a service instead of an LLM | Lab |
|---|---|---|---|
| Find and redact PII in text | Comprehend `DetectPiiEntities` | Typed entities with offsets, so code does the redaction | [4] |
| Score toxicity against your own threshold | Comprehend `DetectToxicContent` | A 0 to 1 score per label | [4] |
| Sort images into unsafe-content categories | Rekognition `DetectModerationLabels` | Fast, consistent categories with 0 to 100 confidence | [4] |
| Explain a decision, or apply rules specific to your site | A multimodal model (Nova 2 Lite in the lab) | A verdict and a reason in words | [4] |
| One managed policy across apps | Bedrock Guardrails `ApplyGuardrail` | No model call; `NONE` or `GUARDRAIL_INTERVENED` | [4] |
| Text, forms, tables and set questions from scans and PDFs | Textract | Blocks with confidence and position | |
| Speech to text | Transcribe | Per-word timings, speaker labels, PII redaction | [11] |
| Text to speech | Polly | SSML control, with SSML tags not billed | |
| Translation that keeps product names fixed | Translate with custom terminology | A terminology file per request | |
| Recommendations from interaction history | Personalize | Trained on your users' events; the LLM only writes around the picks | [13] |
| Search over enterprise connectors that carry document permissions | Kendra GenAI index, also usable as a Bedrock KB retriever | 40+ connectors, permissions through metadata filtering | |
| Dialogs with fixed intents and slots | Lex V2, with `AMAZON.QnAIntent` for open questions | Intents resolve the same way every time; Bedrock only where you add it | |

- Combine them and decide in code. Lab 4 checked text with Comprehend, and images with Rekognition, Nova and a guardrail, then combined the results with a fail-closed rule [4].

## Region and feature checks

- Check each service's endpoints page for your Region before you design; the AWS MCP server's regional availability tool answers the same question. Comprehend's guidelines page lists 13 Regions: US East (Ohio, N. Virginia), US West (Oregon), Asia Pacific (Mumbai, Seoul, Singapore, Sydney, Tokyo), Canada (Central), Europe (Frankfurt, Ireland, London) and AWS GovCloud (US-West).
- Features retire. Comprehend topic modeling, event detection and prompt safety classification are closed to new customers from 2026-04-30.
- Languages differ by operation. Toxicity detection is English only, and the `DetectPiiEntities` description names English and Spanish. Textract reads handwriting in English only, and its Forms analysis covers English, Spanish, Italian, Portuguese, French and German.
- Lex's generative features (`AMAZON.QnAIntent`, assisted slot resolution) must be turned on per bot locale, and assisted slot resolution only covers listed built-in slot types.

## Async jobs: start, finish, collect

Most of these services have a sync call for small inputs and a start and get pair for large ones. Four rules apply to all of them.

1. Start from an S3 event: decode the key, filter by prefix, act only on ObjectCreated, and write output elsewhere (the rules are in `rag-and-vectors.md`) [11].
2. Make the start idempotent, because S3 can deliver an event twice and Lambda retries async invocations [11]:
   - Transcribe job names are unique per account, and a repeat gets `ConflictException`. Name the job from the key and ETag. Treat a conflict as "already started" unless that job FAILED; then delete it and start again, or the file stays blocked [11].
   - Textract takes a `ClientRequestToken` (1 to 64 characters of `[a-zA-Z0-9-_]`). The same token and input within 7 days return the same JobId with no second notification. The same token with changed input raises an idempotent-parameter-mismatch error.
   - Personalize import job names are unique per account and Region. Look a job up by exact name with the paginated list call and skip FAILED ones. The lab's substring match could reuse a FAILED import [13].
3. Learn about completion from an event. Don't poll:
   - Transcribe sends `Transcribe Job State Change` to EventBridge with `TranscriptionJobName` and `COMPLETED` or `FAILED`, plus `FailureReason` on failure. Only `StartTranscriptionJob` sends it.
   - Textract and Rekognition Video publish to the SNS topic in `NotificationChannel`. The topic must be in the same Region, and its name must start with `AmazonTextract` or `AmazonRekognition` when the role uses the AWS managed service role policy. Textract throttles repeated Get calls, so subscribe a queue or a function to the topic.
   - Personalize imports and training have no such event in these labs. Poll `describe_*` with a timeout, and raise on `CREATE FAILED` with its `failureReason` [13].
4. Collect every page. Textract's Get calls return 1,000 blocks by default and continue with `NextToken`. Results are kept for 7 days unless you set `OutputConfig`.

```json
{"source": ["aws.transcribe"], "detail-type": ["Transcribe Job State Change"],
 "detail": {"TranscriptionJobStatus": ["COMPLETED", "FAILED"]}}
```

```python
import hashlib

from botocore.exceptions import ClientError

def start_transcription(transcribe, bucket: str, key: str, job_name: str, out_bucket: str) -> str:
    """job_name comes from the key and ETag (see rag-and-vectors.md), so a repeated event can't start twice."""
    request = dict(TranscriptionJobName=job_name, Media={"MediaFileUri": f"s3://{bucket}/{key}"},
                   MediaFormat=key.rsplit(".", 1)[-1].lower(),   # mp3, mp4, wav, flac, ogg, amr, webm or m4a
                   LanguageCode="en-US", OutputBucketName=out_bucket)
    try:
        transcribe.start_transcription_job(**request)
        return "started"
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConflictException":
            raise
    job = transcribe.get_transcription_job(TranscriptionJobName=job_name)["TranscriptionJob"]
    if job["TranscriptionJobStatus"] != "FAILED":
        return "exists"
    transcribe.delete_transcription_job(TranscriptionJobName=job_name)
    transcribe.start_transcription_job(**request)
    return "restarted"

def start_analysis(textract, key: str, etag: str) -> str:
    return textract.start_document_analysis(
        DocumentLocation={"S3Object": {"Bucket": "amzn-s3-demo-bucket", "Name": key}},
        FeatureTypes=["TABLES", "QUERIES"],
        QueriesConfig={"Queries": [{"Text": "What is the invoice total?", "Alias": "TOTAL"}]},
        ClientRequestToken=hashlib.sha256(f"{key}:{etag}".encode()).hexdigest(),   # 64 hex characters
        NotificationChannel={"SNSTopicArn": "arn:aws:sns:REGION:111122223333:AmazonTextractDone",
                             "RoleArn": "arn:aws:iam::111122223333:role/TextractPublish"},
    )["JobId"]
```

## Size and page limits

| Service and call | Limit |
|---|---|
| Comprehend `DetectToxicContent` | Up to 10 segments, 1 KB each, 10 KB in all; English only |
| Comprehend `DetectPiiEntities` | 100 KB of UTF-8 text |
| Comprehend entities, key phrases, language (sync) | 100 KB per document |
| Comprehend sentiment, targeted sentiment, syntax (sync), and batch calls | 5 KB per document; a batch takes 25 documents |
| Comprehend async jobs | 10 active jobs per operation; 1 MB per document for entities, key phrases, PII and language; 5 GB per job |
| Rekognition images | 5 MB as bytes, 15 MB from S3; JPEG or PNG; 80 px minimum, and 10,000 px maximum per side for `DetectModerationLabels` |
| Textract sync | 10 MB; PDF and TIFF 1 page; 15 queries per page |
| Textract async | JPEG and PNG 10 MB; PDF and TIFF 500 MB and 3,000 pages; 30 queries per page; no password-protected or XFA PDFs |
| Transcribe batch | 28,800 s (8 hours) and 2 GB per file; job names of 1 to 200 characters from `[0-9a-zA-Z._-]`, case sensitive; a default queue of 10,000 jobs when queueing is on |
| Polly `SynthesizeSpeech` | 6,000 characters, at most 3,000 billed |
| Polly `StartSpeechSynthesisTask` | 200,000 characters, at most 100,000 billed; writes to `OutputS3BucketName`, optional `SnsTopicArn` |
| Translate `TranslateText` | 10,000 bytes of UTF-8 |
| Translate batch | 20 MB per document, 1 MB of translatable text in it, 10 concurrent jobs |
| Personalize training data | At least 1,000 interactions and 25 users with 2 or more each; 50,000 from 1,000 users recommended |

- Split text yourself before any call with a byte limit, and count UTF-8 bytes. The lab's toxicity check sent one segment, so any text over 1 KB failed and went to review [4].
- Send multipage PDFs to the async Textract calls; the sync calls take one page.
- The AWS CLI can't pass image bytes to Rekognition. Use an S3 object, or call it from an SDK.

## Moderation that fails closed

Give every check one of three results and combine them so that "couldn't check" never counts as clean [4]:

```python
FLAG, PASS, UNKNOWN = "flag", "pass", "unknown"

def decide(checks: dict[str, dict]) -> str:
    results = [c["result"] for c in checks.values()]
    if FLAG in results:
        return "BLOCK"
    if results and all(r == PASS for r in results):
        return "ALLOW"
    return "REVIEW"                       # an error, an unclear verdict or no checks at all

def run_all(checks: dict) -> dict:
    out = {}
    for name, check in checks.items():
        try:
            out[name] = check()
        except Exception as err:          # one moderator failing must not stop the others
            out[name] = {"result": UNKNOWN, "error": type(err).__name__}
    return {"decision": decide(out), "checks": out}
```

- The lab's `any_flagged` read a failed moderator as "not flagged", so if every moderator failed, the result still looked clean. An unconfigured guardrail was skipped the same way [4].
- Comprehend scores run from 0 to 1, though the lab called them percentages. At 0.1, "bright sunny day" was flagged (INSULT 0.16); at 0.2 it passed. Tune a threshold per label on samples you labelled yourself, since a false INSULT and a false VIOLENCE_OR_THREAT don't cost the same [4].
- Rekognition returns labels at 50 or more confidence unless you set `MinConfidence`. Each label has a `ParentName` and a `TaxonomyLevel` from 1 to 3, so you can decide per category.
- Count a model verdict only when the whole first line is a clear Safe or Unsafe. "I can't say whether this is unsafe" is unknown [4].
- A guardrail's `NONE` means nothing matched the filters that are on. Image filtering is set per category; in the lab only Sexual and Violence had it [4]. See `guardrails.md`.
- Return a redacted copy (`Reach me at [PHONE].`) and the PII types without their values. Live redaction showed `EndOffset` one past the entity's last character, where the docs describe it as the last character; check emoji text before relying on offsets [4].
- Decide whether PII blocks. In the lab it didn't affect the decision, so a friendly message full of PII passed [4].
- Use exit codes a crash can't fake: 0 ALLOW, 10 BLOCK, 11 REVIEW, and anything else means moderation didn't finish. The text subcommand has no `-h`, because text starting with `-h` would print help and exit 0 [4].
- Don't log request bodies. The lab's logs held the raw caption, a phone number, an address and the caller's IP, and API Gateway data tracing was on [4].

## Textract

- Use the sync calls for a single page of 10 MB or less in a request path, and the Start and Get pairs (text detection, document analysis, expense analysis, lending analysis) for anything else.
- A query's `QUERY` block carries its `Alias` and an `ANSWER` relationship to a `QUERY_RESULT` block. A query with no answer has no result. Report it as "not found", because an empty string reads like an answer:

```python
blocks = [b for page in pages for b in page["Blocks"]]          # every AnalyzeDocument or Get page
by_id = {b["Id"]: b for b in blocks}
answers = {b["Query"]["Alias"]: [by_id[i]["Text"] for r in b.get("Relationships", [])
                                 if r["Type"] == "ANSWER" for i in r["Ids"]]
           for b in blocks if b["BlockType"] == "QUERY"}           # [] means Textract found no answer
```

- Watch for `INVALID_REQUEST_PARAMETERS` in query output: too many queries on a page, or a query on a page that doesn't exist.
- `AnalyzeID` reads only US passports and US driver's licenses.

## Transcribe

- Batch jobs read from S3. Boto3 doesn't support streaming transcription; use the async Python SDK (`aws-sdk-transcribe-streaming`), which needs `transcribe:StartStreamTranscription`. A streaming connection can stay open up to 8 hours.
- Take `MediaFormat` from the file extension. The lab hard-coded `mp4`, which happened to work for one mp3 [11].
- The text is at `results.transcripts[0].transcript`. The JSON also carries your account ID, so mask it before sharing [11].
- With a customer bucket, Transcribe writes an empty `.write_access_check_file.temp` there. Read only `*.json`, and skip empty transcripts from silent media [11].
- `ContentRedaction` needs both `RedactionOutput` and `RedactionType`; without `PiiEntityTypes` it redacts every PII type. `OutputKey` changes the output name, which defaults to the job name.
- Treat transcripts as untrusted text: anyone can speak "ignore your instructions" into a recording [11].

## Polly and Translate

- Use `SynthesizeSpeech` for short text in a request path, and `StartSpeechSynthesisTask` for long text, which writes the audio to S3. SSML tags don't count as billed characters.
- Split long text for `TranslateText` at paragraph boundaries under 10,000 bytes, or run a batch job on documents in S3.
- A custom terminology file keeps names fixed: one file per request, terms of up to 200 bytes, files of up to 20 MB.

## Personalize

- A domain dataset group (VIDEO_ON_DEMAND or ECOMMERCE) gives use-case recommenders with no recipe tuning; a custom group uses solutions, solution versions and campaigns. A domain group can still hold custom solutions.
- The lab's "Top picks for you" recommender trained for 46 minutes on 1,006 interactions, 6 over the minimum, so its NDCG@10 of 0.378 shows only that the pipeline works [13].
- Domain recommenders require `EVENT_TYPE`. Mark multi-value columns such as genres `"categorical": true`. Columns were matched by header name, so CSV column order didn't matter [13].
- Find or create every resource by exact name with the paginated `list_*` calls. The ARN layout shows the uniqueness scope: dataset groups and import jobs are unique per account and Region, and a group has one dataset of each type. Job names stop at 63 characters [13].
- Imports took about 300 s whatever the size (3 rows took 301 s). Wait for ACTIVE with a timeout, and raise on `CREATE FAILED`, `INACTIVE` or a `DELETE` state [13].
- Ask for `enableMetadataWithRecommendations` to get `TITLE` and `GENRES` back; the keys arrived lower-case [13].
- Recommenders retrain fully every 7 days, and "Top picks for you" updates every two hours to include new items.
- The user's favourite genre and the picks disagreed, and the model invented a link between them. Filter recommendations on `GENRES`, or drop the genre line when no pick matches [13].

```python
import boto3

personalize = boto3.client("personalize")

def find_by_name(list_fn, items_key: str, name: str, **kw) -> dict | None:
    token = None
    while True:
        page = list_fn(**kw, **({"nextToken": token} if token else {}))
        hit = next((s for s in page.get(items_key, []) if s.get("name") == name), None)
        if hit or not page.get("nextToken"):
            return hit
        token = page["nextToken"]

group = find_by_name(personalize.list_dataset_groups, "datasetGroups", "marketing")
arn = group["datasetGroupArn"] if group else personalize.create_dataset_group(
    name="marketing", domain="VIDEO_ON_DEMAND")["datasetGroupArn"]
```

## Kendra or Bedrock Knowledge Bases

- For new RAG, start with a Bedrock Knowledge Base (see `rag-and-vectors.md`). The Bedrock docs recommend the managed knowledge base for retrieval accuracy and a managed experience.
- Choose a Kendra GenAI Enterprise Edition index when documents already sit behind Kendra connectors, or when you need its 40+ connectors with user permissions carried as metadata filters. A Bedrock KB can use that index as a managed retriever without reindexing.
- Kendra bills for every provisioned index after the free trial, even an empty one that is never queried. Delete it to stop the charges.
- A Lex bot locale can't hold both `AMAZON.QnAIntent` and `AMAZON.KendraSearchIntent`.

## Lex

- Use Lex when the conversation is a set of intents with slots, and let `AMAZON.QnAIntent` answer the rest from a Bedrock KB, an OpenSearch index or a Kendra index. QnAIntent fires only for an utterance that matches no other intent, and never while a slot is being elicited.
- Each bot alias and language uses one Lambda function for all intents. A code hook can send separate success, failure and timeout responses.
- Assisted slot resolution asks a Bedrock model to resolve a value Lex couldn't, for a few built-in slot types only (`AMAZON.Date`, `AMAZON.Number`, `AMAZON.PhoneNumber`, `AMAZON.City` and others). Conversation logs show `interpretationSource` as `Bedrock` when it acted, so you can measure what it adds.

## IAM and service roles

- A service that reads your data or publishes for you needs a role you pass. Personalize import jobs take a `roleArn` that can read the bucket [13]. Textract's `NotificationChannel` takes a role that can publish to the topic, and the caller needs `iam:PassRole` with `iam:PassedToService` set to `textract.amazonaws.com`.
- Scope service-principal trust and resource policies with `aws:SourceArn` and `aws:SourceAccount`. S3 notifications need both, because bucket ARNs carry no account ID. The Lambda console adds both conditions when you add an S3 trigger; keep them if you write the policy yourself [11].
- The Transcribe retry path needs `transcribe:GetTranscriptionJob` and `transcribe:DeleteTranscriptionJob` as well as `StartTranscriptionJob` [11].
- For agent roles and least privilege in general, see `agent-security-and-identity.md`.

## Cost traps

- A Personalize recommender is provisioned at a minimum of 1 request per second and billed by the hour whether you call it or not. Stop or delete it when you're done [13]. A campaign's `minProvisionedTPS` (default 1) sets its minimum charge the same way.
- A Kendra index charges while it exists.
- Every moderator is a separate paid call; lab 4 made three per image [4]. Don't also run a guardrail twice on one prompt (see `guardrails.md`).
- The lab's moderation API had no authentication, and an unsigned POST from outside AWS got 200, so anyone could spend the account's Comprehend and Bedrock budget [4].
- After one failed Personalize import, every rerun of the lab's helpers started another full import until the lookup skipped FAILED jobs properly [13].
- Random job names make a repeated S3 event start a second, billed Transcribe job [11].

## Doc sources

- Comprehend: comprehend/latest/dg/guidelines-and-limits.html, trust-safety.html, APIReference/API_DetectPiiEntities.html, API_DetectToxicContent.html
- Rekognition: rekognition/latest/dg/limits.html, APIReference/API_ModerationLabel.html, CLI `detect-moderation-labels`, `StartContentModeration` NotificationChannel
- Textract: textract/latest/dg/limits-document.html, api-async.html, api-async-roles.html, queryresponse.html
- Transcribe: general/latest/gr/transcribe.html, transcribe/latest/dg/job-queueing.html, monitoring-events.html, getting-started-sdk.html, APIReference/API_StartTranscriptionJob.html; Transcribe FAQ (streaming length)
- Polly: polly/latest/dg/limits.html, `TextLengthExceededException`, `StartSpeechSynthesisTask` input
- Translate: translate/latest/dg/what-is-limits.html
- Personalize: personalize/latest/dg/readiness-checklist.html, interactions-datasets.html, data-prep-ds-group.html, creating-recommenders.html, campaigns.html
- Kendra: kendra/latest/dg/hiw-index-types.html, what-is-kendra.html; bedrock/latest/userguide/knowledge-base-build-kendra-genai-index.html
- Lex: lexv2/latest/dg/built-in-intent-qna.html, assisted-slot.html, lambda.html, paths-code-hook.html
- Skill: `aws-core:aws-messaging-and-streaming` (confused-deputy conditions)
