# Data stores, lakehouse and streams for agents

Lab tags in brackets name the lab that showed each rule: [4] is SimuLearn lab 4, [FDE1-5] is FDE1 lab 5, [FDE2-1] is FDE2 session 1. Their notes are under `D:\AI\AWSAI\` when that folder is on the machine; the rules stand on their own without it.

Contents: which store holds what, DynamoDB for agent memory, Aurora PostgreSQL and pgvector, graph stores, guarding generated Cypher, guarding text-to-SQL, database tools for agents, lakehouse for AI, streaming data for agents, doc sources. Hybrid search and RRF fusion are in `rag-and-vectors.md`, tool contracts in `agents-and-tools.md`, and caller identity in `agent-security-and-identity.md`. Untagged rules come from the docs and skills listed at the end.

## Which store holds what

| Agent data | Store | Why | Lab |
|---|---|---|---|
| Conversation turns | DynamoDB, PK `<user>#<session>`, SK turn number | Ordered key and range reads, no joins | [FDE2-1, FDE1-2] |
| Agent checkpoints and the agent registry | DynamoDB, PK `request_id` (or `agent_name`), versioned conditional writes (`scripts/dynamodb_state.py`) | One item per request, optimistic locking | [FDE2-2] |
| Source-of-record rows, governed metrics, SQL next to vectors | Aurora PostgreSQL with pgvector | Filters, joins and vector search in one query | [FDE1-2, FDE2-1] |
| Ontology, entity links, metric bindings | Neptune (openCypher) or Neo4j | Relationship traversal | [FDE2-1] |
| Chunks for keyword plus vector search | OpenSearch Serverless (k-NN and BM25) | Managed hybrid search | [FDE2-1] |
| Semantic cache, hot session state | ElastiCache (Valkey); MemoryDB when it must survive a restart | In memory, lowest latency | |
| Documents, media, transcripts, evidence, lake tables | S3 | Cheap, and an event source for pipelines | [FDE1-1, FDE2-1, 11] |
| Large vector sets queried now and then | S3 Vectors | Low cost, sub-second queries | |

- Give each agent role its own IAM role scoped to its own store, and prove the boundary with `aws iam simulate-principal-policy`: the owner gets `allowed`, every other role `implicitDeny`. An inline policy with a harmless name gave the vector role read access to the memory table. Read each policy's JSON, not its name [FDE2-1].
- Write a document to both vector stores in one dual-ingest call. Keep only the structure in the graph (`(:Chunk)-[:MENTIONS]->(:Entity)`), not the vector [FDE2-1].
- Benchmark stores on one query set and report `doc_count`, `p50_ms` and `relevance_at_k` for each. In that session pgvector answered at a p50 of about 5.5 ms and OpenSearch Serverless at about 174 ms (network hop included), with the same relevance [FDE2-1].
- Before you add a store, check whether the one that already holds the data has vector search. The AWS vector database decision guide (updated 2026-10-30) covers Aurora PostgreSQL, DocumentDB, DynamoDB, ElastiCache, MemoryDB, Neptune Analytics, OpenSearch managed clusters, OpenSearch Serverless and S3 Vectors, plus Bedrock Knowledge Bases on top of them. Confirm the feature in your Region before you design around it.
- S3 Vectors fixes the dimension, the distance metric (cosine or euclidean only) and the non-filterable metadata keys when you create the index, and takes at most 500 vectors per PutVectors call. For hundreds or thousands of sustained queries per second, use OpenSearch.

## DynamoDB for agent memory

- Query returns items in ascending sort-key order. Reading the history with `ScanIndexForward=False` silently returned it newest first, so code that took the last item as the newest turn broke "next turn = last + 1" [FDE2-1]. Read history ascending. Descending order is right only with `Limit=1`, to fetch the last turn, as the snippet below does.
- Take the user and session from the authenticated request, never from tool arguments. Key on `<user>#<session>`, refuse session IDs that contain `#`, and give the model only `append_turn(content)` and `get_session()` [FDE2-1].
- Claim the next turn ID with a create-only put. Read-then-increment with a plain put let two writers share an ID and overwrite each other [FDE2-1]:

```python
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

def append_turn(table, key: str, role: str, content: str, attempts: int = 5) -> int:
    for _ in range(attempts):
        last = table.query(KeyConditionExpression=Key("session_id").eq(key),
                           ScanIndexForward=False, Limit=1, ConsistentRead=True)["Items"]
        turn = int(last[0]["turn_id"]) + 1 if last else 1
        try:
            table.put_item(Item={"session_id": key, "turn_id": turn, "role": role, "content": content},
                           ConditionExpression="attribute_not_exists(turn_id)")
            return turn
        except ClientError as e:
            if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise                       # throttling is not a conflict
    raise RuntimeError("no free turn id")   # surface as a tool error; nothing was overwritten
```

- Read history oldest first on purpose, page on `LastEvaluatedKey` (one Query returns at most 1 MB), and treat a new session as `[]` [FDE2-1].
- Seed demo data with explicit turn IDs, so the seed doesn't depend on the read code you're fixing [FDE2-1].
- LangGraph's `DynamoDBSaver` (`langgraph-checkpoint-aws`) expects partition key `PK` (S), sort key `SK` (S) and TTL attribute `ttl`, whatever a `KEY_SCHEMA` env var claims. A key schema can't change after creation, so create a new table and repoint the setting. Set the Region override to the table's Region [FDE1-2].
- TTL deletes expired items within a few days, not at the expiry time. Filter expired items out of reads.

## Aurora PostgreSQL and pgvector

- The distance operator must match the index operator class, or the planner scans the whole table [FDE1-2]:

| Index opclass | Operator |
|---|---|
| `vector_cosine_ops` | `<=>` |
| `vector_l2_ops` | `<->` |
| `vector_ip_ops` | `<#>` |

```sql
CREATE TABLE document_embeddings (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  content text, metadata jsonb,
  embedding vector(1024));                         -- must match the model: Titan Text Embeddings V2 is 1024
CREATE INDEX ON document_embeddings USING hnsw (embedding vector_cosine_ops);
CREATE INDEX ON products (category, price);        -- relational filters need their own B-tree, equality first
ANALYZE products;
SELECT id FROM products WHERE category = $1 AND price < $2
ORDER BY embedding <=> $3 LIMIT 5;                 -- driver parameters; the same operator as the opclass
```

Run `EXPLAIN` on that query with real values and check that the plan has no Seq Scan [FDE1-2].

- Call Aurora from Lambda through the RDS Data API with a Secrets Manager ARN, never a password env var. Keep one secret per privilege level (admin, agent read-only, KB writer), check that the cluster ARN and database name exist, and set `SSL_MODE=require` [FDE1-2].
- Give the agent a pre-aggregated view (`GRANT SELECT ON customer_summary TO agent_readonly`), so it reads one row per group instead of scanning the table [FDE1-2].
- A Bedrock KB on Aurora maps the vector, text, metadata and primary key fields to your columns, and its secret's user needs write access to the table. A sync can report Complete with Scanned = 0, which means the S3 prefix is wrong. Check that Added is above 0, or run `SELECT COUNT(*)` [FDE1-2].
- Use one data source per S3 prefix, and include only the prefixes the agent may see [FDE1-2].
- Compute governed metrics from an allow-list of parameterised SQL in a read-only transaction. The metric's template on the graph node is documentation only. Resolve a question to a metric through a vector index with a threshold (0.75 there), so an off-domain question matches nothing and the assistant declines [FDE2-1]:

```python
GOVERNED_METRIC_SQL = {"loss_ratio": "SELECT claims / premium FROM ledger WHERE line_of_business = %s"}

def compute_metric(cur, name: str, lob: str):
    sql = GOVERNED_METRIC_SQL.get(name)
    if sql is None:
        raise ValueError(f"unknown or unapproved metric: {name}")
    cur.execute("SET TRANSACTION READ ONLY")
    cur.execute(sql, (lob,))          # a bound parameter, never formatted into the string
    return cur.fetchone()[0]
```

## Graph stores: Neptune openCypher and Neo4j

- An ontology lookup needs every hop: `(:Entity)-[:INSTANCE_OF]->(:OntologyClass)<-[:COMPUTED_OVER]-(:GovernedMetric)`. With no `INSTANCE_OF` links, every entity resolved to no metric and nothing raised an error. The fix was in the seed data, and editing the seed code changed nothing until a re-seed [FDE2-1].
- In Neo4j, create a uniqueness constraint for every MERGE key before seeding. MERGE without one is read-then-create, so two seeders both create the node [FDE2-1]:

```cypher
CREATE CONSTRAINT entity_id IF NOT EXISTS FOR (e:Entity) REQUIRE e.id IS UNIQUE;
MERGE (e:Entity {id: $id}) SET e.name = $name;
```

- A read session or read transaction blocks writes but still runs procedures such as `CALL dbms.components()` [FDE2-1].
- On Neptune, make IAM the read-only boundary: allow `neptune-db:ReadDataViaQuery` only. Neptune works out every action a query might perform before it runs it, so a query that could write is refused even when it wouldn't. `neptune-db:QueryLanguage` limits the role to one language:

```json
{"Version": "2012-10-17", "Statement": [{
  "Effect": "Allow",
  "Action": "neptune-db:ReadDataViaQuery",
  "Resource": "arn:aws:neptune-db:REGION:111122223333:cluster-EXAMPLERESOURCEID/*",
  "Condition": {"StringEquals": {"neptune-db:QueryLanguage": "OpenCypher"}}}]}
```

- Neptune uses the smaller of the cluster's `neptune_query_timeout` and a per-query `USING QUERY:TIMEOUTMILLISECONDS n` hint. Add the hint in your code after the guard has passed the query, since the guard only accepts queries that start with a read clause.

## Guarding LLM-generated Cypher

1. Prefer named traversals: the model picks a reviewed, parameterised query by name and fills in values. Two or three of them covered every real use, and a free-form tool next to untrusted content was rated P0 [FDE2-1].
2. Expose free-form Cypher only to staff-facing agents, and only behind `scripts/cypher_guard.py` plus a read-only database identity: Neptune IAM as above, or a Neo4j role with read privileges only and EXECUTE PROCEDURE and EXECUTE FUNCTIONS revoked [FDE2-1].
3. Refuse with a tool error. The guard raises `QueryRejected(reason, detail)`, which FastMCP returns with `isError: true`. A "Rejected:" prefix inside a success result is easy to miss [FDE2-1].

`check()` accepts one query that starts with MATCH, OPTIONAL MATCH, WITH or UNWIND, contains RETURN and ends in `LIMIT n` with n at most 50. It refuses `;`, comments, write and procedure clauses as whole words (`created_at` and `OFFSET` pass), plugin and user-defined functions outside the built-in namespaces (`point`, `vector.similarity`, `date`, `datetime`, `localdatetime`, `localtime`, `time`, `duration`), and any Unicode space, format or control character other than space, tab, CR and LF. `run_read()` uses a READ transaction with a 5 s timeout and pulls at most 50 records instead of fetching everything and slicing.

```
python scripts/cypher_guard.py "MATCH (c:OntologyClass) RETURN c.name LIMIT 10"   # exit 0 allowed, 1 rejected, 2 usage
```

What a review of the lab's guard found, now fixed and tested in the bundled copy:

- `INSERT` (Neo4j's GQL synonym for CREATE) wasn't on the deny list.
- Plugin and user-defined functions such as `genai.vector.encode(...)` or `gds.util.asNode(...)` run without CALL, and only `apoc`, `db` and `dbms` were blocked.
- Words were split on Python whitespace, so a separator Python doesn't split on (U+180E) could hide `LOAD CSV` if the database parser accepts it. That parser behaviour wasn't tested against a live database; the guard now refuses such characters.

Limits that remain: the scan over-blocks (a string literal containing `set`, a URL with `//`), LIMIT caps rows but not work, so an eager aggregate can still use the whole timeout, and any clause or function the lists miss gets through. The database identity is the control.

## Guarding text-to-SQL

- Build the prompt from the live Glue catalog with column types, and page through `get_tables`, so a large database doesn't lose tables from the prompt [3].
- Make two calls: schema plus question to SQL at temperature 0, then question, SQL and rows to an answer drawn only from those rows [3].
- Treat the SQL as untrusted. "Delete every car from the table" produced `DELETE FROM ...;` and the model didn't refuse [3]. Check in layers before Athena sees it:

```python
import re

IGNORED = re.compile(r"'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"|--[^\n]*|/\*.*?\*/", re.DOTALL)
WRITES = re.compile(r"\b(ALTER|CALL|CREATE|DEALLOCATE|DELETE|DROP|EXECUTE|GRANT|INSERT|MERGE|MSCK|OPTIMIZE"
                    r"|PREPARE|REPAIR|REVOKE|SET|TRUNCATE|UNLOAD|UPDATE|USE|VACUUM)\b", re.IGNORECASE)

def require_read_only(sql: str) -> str:
    visible = IGNORED.sub(" ", sql).strip().rstrip(";")     # strings, quoted names and comments can't hide a ;
    if ";" in visible or not re.match(r"(SELECT|WITH)\b", visible, re.IGNORECASE) or WRITES.search(visible):
        raise ValueError("refusing SQL that isn't one read-only SELECT")
    return sql
```

- Run the query as a role that can only read. The keyword check is a safety net that fails closed [3].
- Send at most 100 rows to the model and say when rows were left out, so an answer built on part of the data says so [3].
- Stop the Athena query when your timeout passes, because Athena bills by data scanned [3]. Set the workgroup's per-query data scanned limit as well: Athena cancels a query that goes over it and charges for what it scanned up to that point. Turn on `EnforceWorkGroupConfiguration` so the workgroup's result location and settings apply to every query.
- Check answers against ground truth you compute separately. A correct query still reported a 400 hp four-cylinder wagon, a data error that text-to-SQL won't question [3].

## Database tools for agents

- Each tool's `inputSchema` must match what the handler reads: the property names (`sql`, not `query_text`) and the JSON types (`integer` when the handler checks `isinstance(x, int)`) [FDE1-2].

```json
{"name": "explain_query", "description": "Show the execution plan for one read-only SQL query.",
 "inputSchema": {"type": "object", "properties": {"sql": {"type": "string", "description": "SQL to explain"}},
                 "required": ["sql"]}}
```

- Connect an MCP database server as the same database user the agent uses. Admin credentials hide the permission errors you're debugging [FDE1-2].
- Test a Lambda-hosted MCP server with `{"method": "tools/list"}` and `{"method": "tools/call", "params": {"name": "...", "arguments": {...}}}` events, and check that its table list names real tables, not placeholders [FDE1-2].
- Give the model the smallest signature that works, and bind scope from the request (see the DynamoDB section) [FDE2-1]. For the rest of the tool contract, see `agents-and-tools.md`.

## Lakehouse for AI: Glue, Iceberg, Athena and Lake Formation

- A crawler names its table from its prefix and the folder (`s3_` plus `cars-data` gave `s3_cars_data`). Wait for READY with a new `LastCrawl.StartTime`, then check `LastCrawl.Status` (see the waits table in `serverless-apps.md`) [3].
- Use lowercase names. Glue rejects mixed case with `GENERIC_INTERNAL_ERROR`, and S3 Tables namespaces and tables can't contain hyphens. S3 Tables permissions are `s3tables:*`, not `s3:*`.
- Glue column types must match the JSON values. A JSON `true` in an `int` column failed Firehose's Parquet conversion until the column became `boolean`. To change a type with `update_table`, rebuild `TableInput` from an allow-list of keys, because `get_table` returns read-only fields that `update_table` rejects [FDE1-5].
- Partition on low-cardinality columns that appear in WHERE clauses, two or three levels at most, never on IDs.
- Athena runs one statement per query. Switch to the workgroup that holds the result location first [FDE1-1].
- In Glue Spark, address Iceberg tables as `glue_catalog.db.tbl`. Merge-on-read writes small delete files where copy-on-write rewrites whole files on every MERGE or UPDATE [FDE1-1].
- Promoting `int` to `bigint` is a metadata change only. Partition evolution applies to data written afterwards, so re-run ingest after it [FDE1-1].
- If OPTIMIZE or VACUUM seems to do nothing, read the table properties first [FDE1-1]:

```sql
SHOW TBLPROPERTIES db.tbl;
ALTER TABLE db.tbl SET TBLPROPERTIES ('vacuum_min_snapshots_to_keep'='2', 'vacuum_max_snapshot_age_seconds'='60');
OPTIMIZE db.tbl REWRITE DATA USING BIN_PACK;
VACUUM db.tbl;                                     -- after the snapshot age has passed
SELECT snapshot_id, committed_at, operation FROM "db"."tbl$snapshots" ORDER BY committed_at;
```

- Repeated ingest runs duplicated rows because the job read every export prefix. Build the read path from the job's date argument, upsert with `MERGE INTO`, and clean up with `CALL glue_catalog.system.rollback_to_snapshot('db.tbl', <id>)`. Then compare `COUNT(*)` with `COUNT(DISTINCT id)` [FDE1-1].
- Lake Formation: assigning LF-Tags needs ASSOCIATE on those keys and values unless you created the tag, plus `lakeformation:AddLFTagsToResource` in IAM. The lab's admin role still got "missing ASSOCIATE on one or more LF-Tags" for tags it hadn't created. When a tag-based grant shows a table to the wrong role, retag the table and leave the grant alone [FDE1-1].
- A data cell filter's column list is an allow-list (untick PII columns), its row filter needs the data's exact casing, and it does nothing until you grant SELECT on the filter [FDE1-1].

## Streaming data for agents

- Kinesis Data Streams: a provisioned shard takes 1 MB/s or 1,000 records/s. Use on-demand mode for spiky traffic, key on a high-cardinality ID (device, user, session) to avoid a hot shard, and alarm on `WriteProvisionedThroughputExceeded` > 0. PutRecords takes 500 records per call; re-send only the entries that come back with an `ErrorCode` [FDE1-5].
- Firehose transforms must return `{"records": [{"recordId", "result", "data"}]}` with `data` base64 encoded, one entry per input record, or Firehose fails every record [FDE1-5]. `scripts/firehose_transform.py` does this, marks bad records ProcessingFailed one by one, and refuses to emit partition keys for a record with no valid date.
- The transform returns `recordId`, `result` (`Ok`, `Dropped` or `ProcessingFailed`) and base64 `data` for Kinesis Data Streams and Direct PUT sources. An MSK source uses `kafkaRecordValue` in place of `data` (Firehose's data-transformation-status-model page, whose prose spells it `KafkaRecordValue`), which `scripts/firehose_transform.py` doesn't handle.
- Firehose invokes the transform synchronously, so request and response are each capped at 6 MB. A failed invocation is retried 3 times by default, then the batch goes to the `processing-failed` prefix.
- Dynamic partitioning can only be turned on when you create the stream, and it requires an error output prefix. Bad JQ shows up in the stream's destination error logs [FDE1-5].
- Firehose into Iceberg appends every record until you set unique keys. A 60 s buffer made many tiny files; use 300 s or more [FDE1-1]. The lab notes say JSON to Parquet conversion needs a buffer of at least 64 MiB [FDE1-5], but the Firehose guide doesn't state that. The 64 MiB minimum in the Firehose API reference is the Parquet and ORC SerDe block size (`BlockSizeBytes`), so check the console or the API error before you rely on the buffer figure.
- Managed Flink: use event time, a 60 s checkpoint interval and `TRIM_HORIZON` as the start position. In that job, the time mode and start position were app-defined runtime properties, read by the job's own code. Stop, configure, then run without a snapshot, because restoring an old snapshot after a time-semantics change fails the job. `update-application` replaces the whole property group, so merge the existing keys [FDE1-5].
- MSK: set `retention.ms` to cover the business lifecycle with `cleanup.policy=delete`, since `compact` keeps only the latest value per key. Keep `compression.type=producer` at the cluster, and check topic-level overrides [FDE1-5].
- An MSK Connect S3 sink "running but never wrote a file" had `flush.size=1000000` and `rotate.interval.ms=-1`. Use a reachable `flush.size`, a rotate interval, `JsonConverter` with `schemas.enable=false`, and every topic you need [FDE1-5].
- MSK SASL/SCRAM secrets must be named with the `AmazonMSK_` prefix and encrypted with a customer managed KMS key. Scope every queue or topic policy that grants a service principal with `aws:SourceArn` and `aws:SourceAccount`.
- Glue Schema Registry holds Avro, JSON Schema and Protobuf schemas with compatibility modes, and integrates with MSK, Kinesis Data Streams, Managed Flink and Lambda. Use it as the contract between producers and the agents that consume the stream.

The alerting Lambda on a stream:

- One bad record blocks its shard: Lambda retries the batch until it expires while `IteratorAge` climbs. Fix it on the event source mapping with bisect on error, a retry limit, a maximum record age and an on-failure SQS destination [FDE1-5].
- Partial batch responses do nothing until `ReportBatchItemFailures` is in the mapping's `FunctionResponseTypes`. Return the first failed sequence number and stop, because Lambda retries every record after it [FDE1-5].
- Async invocation destinations never fire for stream triggers. The on-failure message holds the shard and sequence range, not the payload [FDE1-5].
- Publish to SNS with the score as a `Number` message attribute, and filter subscriptions on it (`{"score": [{"numeric": [">", 0.7]}]}`). A filter on the body needs `FilterPolicyScope=MessageBody` [FDE1-5].

```python
import base64, json, os

import boto3

sns = boto3.client("sns")

def handler(event, context):
    for record in event["Records"]:
        seq = record["kinesis"]["sequenceNumber"]
        try:
            body = json.loads(base64.b64decode(record["kinesis"]["data"]))
            score = float(body["score"])
            sns.publish(TopicArn=os.environ["TOPIC_ARN"], Message=json.dumps(body),
                        MessageAttributes={"score": {"DataType": "Number", "StringValue": str(score)}})
        except Exception:
            return {"batchItemFailures": [{"itemIdentifier": seq}]}   # stop: later records are retried anyway
    return {"batchItemFailures": []}
```

## Doc sources

- Choosing a vector database (decision guide): docs.aws.amazon.com/decision-guides/latest/decision-guides/vector-database-guide.html
- Skills: `aws-data-analytics:storing-and-querying-vectors`, `creating-data-lake-table` (best-practices.md), `querying-data-lake`, `amazon-opensearch-service`; `aws-core:aws-messaging-and-streaming`
- DynamoDB TTL and Query pagination: amazondynamodb/latest/developerguide/TTL.html; repost.aws/knowledge-center/dynamodb-implement-pagination
- Neptune: neptune/latest/userguide/iam-data-access-policies.html, iam-data-condition-keys.html, opencypher-query-hints-timeout-hint.html
- Athena: athena/latest/ug/workgroups-setting-control-limits-cloudwatch.html
- Firehose: firehose/latest/dev/data-transformation.html, data-transformation-failure-handling.html
- Lambda: lambda/latest/dg/services-kinesis-batchfailurereporting.html
- Glue Schema Registry: glue/latest/dg/schema-registry.html
- Neo4j Cypher cheat sheet (INSERT, EXECUTE FUNCTIONS): neo4j.com/docs/cypher-cheat-sheet/current
