# Retrieval, Knowledge Bases and vector stores

Lab tags in brackets name the lab that showed each rule: [16] is SimuLearn lab 16, [FDE1-3] is FDE1 lab 3, [FDE2-2] is FDE2 session 2. Their notes are under `D:\AI\AWSAI\` when that folder is on the machine; the rules stand on their own without it.

Contents:

- Retrieve, then Converse
- Managed and vector Knowledge Bases
- Testing grounding
- Scores mean different things in each store
- Hybrid search and rank fusion
- Access and audit
- Getting the data in, and chunking
- Queries
- Media sources (Transcribe)
- Console steps that tripped people

## Retrieve, then Converse

This works for managed and vector Knowledge Bases (the two types `scripts/kb_rag.py` handles). Managed KBs don't support RetrieveAndGenerate [16].

```python
import re

import boto3

agent = boto3.client("bedrock-agent-runtime")

def answer(kb_id: str, question: str, floor: float) -> str:
    hits = agent.retrieve(
        knowledgeBaseId=kb_id,
        retrievalQuery={"text": question},
        retrievalConfiguration={"vectorSearchConfiguration": {"numberOfResults": 5}},
    )["retrievalResults"]
    hits = [h for h in hits if h.get("score", 0) >= floor]   # floor measured for this KB
    if not hits:
        return "I don't know."                                # no model call
    texts = [re.sub(r"(?i)</?doc", lambda m: m.group().replace("<", "&lt;"), h["content"]["text"]) for h in hits]
    context = "\n\n".join(f"<doc id=\"{i}\">\n{t}\n</doc>" for i, t in enumerate(texts))   # a chunk can't close its fence
    text, stop = ask(MODEL_ID, GROUNDED_SYSTEM, f"{context}\n\nQuestion: {question}")  # see models-and-prompts.md
    if stop in ("max_tokens", "model_context_window_exceeded"):
        raise RuntimeError("answer cut off; raise maxTokens")   # never return a partial answer as a whole one
    return text
```

- A managed KB takes `managedSearchConfiguration` instead of `vectorSearchConfiguration`, and needs a recent botocore. `scripts/kb_rag.py` looks up the KB type and builds the right shape [16].
- Grounding comes from the prompt: answer only from the documents, and say "I don't know." otherwise [12, 16, 18].
- Test both ways: questions that must be answered and questions that must get "I don't know." An "I don't know" can also be a recall miss at a small k [16]. `kb_rag.py` retrieves 3 chunks unless you pass `-n`. It reports a cut-off answer as `truncated`, not as answered; raise `--max-tokens` if that happens.

```
python scripts/kb_rag.py --kb-id KBID --model us.amazon.nova-2-lite-v1:0 \
    --answerable "What is Amazon SageMaker Autopilot?" --unanswerable "What is the capital of France?"
```

- `numberOfResults` takes 1 to 100 for vector search. At k=3, "Studio vs Canvas pricing" got no Studio pricing chunk and the model declined. At k=5, the Autopilot question returned five chunks, scoring 0.955 down to 0.846 [16].
- Keep the template's shape fixed. With `"{context} and {question}"`, Mistral 7B turned an off-topic request into a story made of the retrieved FOMC minutes. Tags around context and question, plus the "I don't know" rule, made Mistral and Nova Pro both decline [12]. Fill it with `fill()` from `models-and-prompts.md`.
- Tell the model not to repeat the excerpts or their tags. Nova 2 Lite pasted two excerpt blocks into one answer. If you strip echoes, remove only complete blocks with no `<` inside; a looser pattern could delete a real answer [11].
- Return sources from your own retrieval as object keys and scores. Drop the bucket name, which carries the account ID [12].
- Cache the KB type per KB ID. Lab 16's app called GetKnowledgeBase on every question [16].

## Managed and vector Knowledge Bases

| | Managed KB | Vector KB |
|---|---|---|
| Vector store | Owned by Bedrock | Yours (OpenSearch Serverless in labs 12 and 16) |
| Chunking | Not selectable | `FIXED_SIZE`, `SEMANTIC`, `HIERARCHICAL` or `NONE`, fixed when the data source is created |
| Query APIs | Retrieve only | Retrieve and RetrieveAndGenerate |
| Retrieve config | `managedSearchConfiguration` | `vectorSearchConfiguration` |
| Data source type | `MANAGED_KNOWLEDGE_BASE_CONNECTOR`, with S3 settings in a JSON `connectorParameters` string | `S3`, with `s3Configuration` |
| Ingestion in lab 16 | IN_PROGRESS after 27 minutes; Retrieve already worked at 19 | 16 s (fixed-size) and 98 s (semantic) for the same CSV |

botocore 1.40.23 predates `managedSearchConfiguration`; the lab's 1.43.106 accepted it [16].

## Testing grounding

Cover each kind of question, not just the obvious ones. Lab 16's managed KB with Nova 2 Lite gave the expected reply to all of these [16]:

| Kind | Example | Expected |
|---|---|---|
| In scope | "Which instances work with managed spot training?" | The answer from the documents |
| Unrelated | "What is the capital of France?" | I don't know. |
| Domain-adjacent | "What is a decision tree in machine learning?" | I don't know., even though 3 chunks came back |
| Neighbouring service | "What is Amazon Bedrock?" against a SageMaker FAQ | I don't know. |
| Needs two chunks | "Studio vs Canvas pricing" | An answer; a decline here is a recall miss to fix in retrieval, not in the prompt |

`kb_rag.py` exits 0 when every question behaved, 1 for a refusal, an out-of-scope answer or a blank reply, and 2 for bad arguments or a failed call [16].

## Scores mean different things in each store

| Store | Score | Better is | Notes |
|---|---|---|---|
| FAISS library, `similarity_search_with_score` (L2 index) | squared L2 distance | lower | With unit vectors, cosine = 1 - d/2 [11] |
| OpenSearch `innerproduct` space ("Dot product" in the console) | 1 + dot when dot > 0, else 1 / (1 - dot) | higher | 1.52 means a cosine of about 0.52 with unit vectors [12] |
| OpenSearch `cosinesimil` space | (1 + cos) / 2 | higher | Ranges from 0 to 1 |
| OpenSearch `l2` space | 1 / (1 + squared L2) | higher | |
| Lab 16's managed KB | about 0.9 for everything | higher | In-scope and out-of-scope barely differed (0.92 to 0.96 against 0.88 to 0.95) [16] |
| Lab 17's `cosinesimil` index | (1 + cos) / 2 | higher | The top 3 sat within 0.02 of each other (0.57 to 0.61). They rank hits but don't show relevance [17] |
| RRF fused score | sum of 1 / (k + rank) | higher | With two lists and k = 60 the maximum is 2/61, about 0.033. Anything higher means a doc was counted twice in one list [FDE2-1] |

Measure a score floor (the minimum score you accept, `--min-score` in `kb_rag.py`) on known good and known bad questions for each index before you trust it. Lab 11's floor was cosine 0.3 (on-topic 0.518 to 0.824, off-topic 0.067 at most) [11].

- Lab 11's chatbot kept FAISS hits with a score of 0.9 or less. That is a distance, so it meant cosine 0.55 or more, and it refused off-topic questions only by luck of the units [11].
- Lab 16's two OpenSearch-backed KBs did separate the cases: in-scope top scores 1.71 to 1.86, out-of-scope 1.03 to 1.20, over six questions [16].
- A floor copied as a bare number accepts everything on an `innerproduct` index: a score of 0.5 there means a dot product of -1.

## Hybrid search and rank fusion

Keyword search (BM25) finds exact terms such as error codes, part numbers and names, which embeddings blur. Vector search finds paraphrases. Hybrid search runs both and merges the results. BM25 scores are unbounded while vector scores have their own range, so merge by rank, or normalize the scores first (aws-data-analytics:amazon-opensearch-service `vector-knn.md`).

One `bool`/`should` query that mixes `match` and `knn` returns raw hits with no fused score. In FDE2-1 the failure showed up as `KeyError: 'score'` in the retriever, but the cause was the search client, so fix it where the score should be made [FDE2-1].

| Where to fuse | Needs | Notes |
|---|---|---|
| Bedrock KB Retrieve with `overrideSearchType: "HYBRID"` | OpenSearch Serverless, RDS or Aurora PostgreSQL, or MongoDB Atlas, with a filterable text field | Bedrock merges for you. Lab 12's index had `AMAZON_BEDROCK_TEXT_CHUNK` as its filterable text field. The user guide lists these stores; the older SDK reference names only OpenSearch Serverless. See aws-core:amazon-bedrock `knowledge-bases-retrieval.md` |
| `hybrid` query with a `normalization-processor` search pipeline | OpenSearch 2.11 or later, or OpenSearch Serverless | Normalize with `min_max` or `l2`, combine with `arithmetic_mean`, `geometric_mean` or `harmonic_mean`, with optional weights |
| `hybrid` query with a `score-ranker-processor` (RRF) | OpenSearch 2.19 or later | `rank_constant` is 1 or more, default 60, plus optional weights that sum to 1.0. The OpenSearch Serverless docs list only the normalization processor |
| RRF in your own code (`scripts/rrf_fusion.py`) | Two queries | Works on any store, and across stores such as OpenSearch plus pgvector [FDE2-1] |

The aws-data-analytics `vector-knn.md` reference cites OpenSearch's benchmark: RRF scored 3.86% lower NDCG@10 than score normalization, and held steadier when score distributions shifted. Measure both on your own questions.

Server-side RRF on an OpenSearch 2.19 or later domain:

```json
PUT _search/pipeline/hybrid-rrf
{"phase_results_processors": [{"score-ranker-processor": {"combination": {"technique": "rrf", "rank_constant": 60}}}]}

GET my-index/_search?search_pipeline=hybrid-rrf
{
  "size": 5,
  "_source": {"excludes": ["embedding"]},
  "query": {"hybrid": {"queries": [
    {"match": {"text": "error E1234"}},
    {"knn": {"embedding": {"vector": [0.01, -0.02], "k": 20}}}
  ]}}
}
```

The same in your own code, for Serverless or for two different stores:

```python
from rrf_fusion import rrf   # scripts/rrf_fusion.py, standard library only

def hybrid_search(client, index: str, text: str, vector: list, top_k: int = 5) -> list:
    n = max(top_k * 2, 10)                                   # over-fetch each list before fusing
    src = {"excludes": ["embedding"]}
    bm25 = client.search(index=index, body={"size": n, "_source": src, "query": {"match": {"text": text}}})
    knn = client.search(index=index, body={"size": n, "_source": src,
                                           "query": {"knn": {"embedding": {"vector": vector, "k": n}}}})
    lists = [[h["_source"] for h in r["hits"]["hits"]] for r in (bm25, knn)]
    return rrf(lists, k=60, top_k=top_k, key="chunk_id")     # each result gets a numeric "score"
```

- A doc scores 1 / (k + rank) in each list it appears in, with ranks starting at 1, and the scores are summed. FDE2-1 used k = 60 and over-fetched max(top_k × 2, 10) per list [FDE2-1].
- Fuse on your own chunk id, and count each chunk once per list at its best rank. FDE2-1's index held a chunk twice and the lab's `rrf()` added both copies: its top score was about 0.065, above the 2/61 ceiling. `scripts/rrf_fusion.py` returns 0.0328 for the same input [FDE2-1].
- Don't put a similarity floor on fused scores. They come from ranks only: 1/61, about 0.016, just means "first in one list". Apply any floor to the vector list before fusing.
- Benchmark stores like for like: the same queries, with `doc_count`, `p50_ms` and `relevance_at_k` for each store, and a check that `doc_count` is above 0. In FDE2-1, OpenSearch Serverless took about 174 ms at p50 (including the network hop) and pgvector about 5.5 ms, with the same relevance@k [FDE2-1].
- Write each chunk to every store in one ingest call, so they hold the same corpus [FDE2-1].
- On OpenSearch Serverless, neural queries take remote models only, and a new search pipeline can take up to 15 seconds to be found (AWS docs, serverless-configure-neural-search).

## Access and audit

- Retrieve and RetrieveAndGenerate are CloudTrail data events, so turn on data event logging if you need an audit trail.
- Use metadata filters for document-level access, so a user only retrieves what they may read.
- Guardrails don't filter the raw `retrievedReferences` in a response. Don't send them to the client unchecked.
- In agent and RAG chat, wrap the retrieved text in `guardContent` if the guardrail should check it (see `guardrails.md`).
- Filter syntax and the operators each store supports are in aws-core:amazon-bedrock `knowledge-bases-retrieval.md`. An operator a store doesn't support is ignored without an error, so test that a filter really excludes documents.
- The KB service role needs `aoss:APIAccessAll` on the collection, and the collection's data access policy must name that role (aws-core:amazon-bedrock `knowledge-bases-setup.md`).
- Transcribe output carries your account ID in `accountId`. Mask it before you share transcripts [11].

## Getting the data in, and chunking

- Retrieval only finds what you loaded. A sorted slice of the data left out the author the question was about. Sample randomly [17].
- Chunking decides recall. Fixed-size chunks (300 tokens, 20% overlap) kept an answer that semantic chunking cut off. The strategy is fixed per data source, so pick it with real questions [16].
- Titan Text Embeddings V2: 1024 dimensions by default (256 and 512 also offered), and up to 8,192 tokens or 50,000 characters of input, whichever comes first. `normalize` defaults to true, and with unit vectors dot product equals cosine [11, 12, 17].
- A KB with an S3 data source stays empty until a sync runs, and sync is manual. Call `start_ingestion_job` from an S3 event or a schedule when documents change [8, 12]. A custom data source skips the sync: `IngestKnowledgeBaseDocuments` indexes documents directly.
- Ingestion status can lag. Lab 16's managed KB said IN_PROGRESS for 27 minutes while Retrieve already worked, so probe readiness with a real Retrieve [16].
- With an existing OpenSearch index, the field names you give Bedrock must match the mapping, and the vector field must have the model's dimension and the right space type [12].
- Load in bulk with a deterministic `_id` (a hash of the source row). Per-document loading took 1,434 s, and a fixed ID makes a rerun safe [17]. That holds on a domain and on a NextGen Serverless vector collection. A Classic Serverless vector collection doesn't keep a client `_id`, so a re-seed indexes chunks again; dedupe by your own chunk id at query time [FDE2-1; AWS docs, serverless-vector-search].
- Check the file before loading it: lab 17's TSV had no header row (`header=None`), and mapping `pub_date` as text broke date ranges [17].
- Titan V2 embeds one text per call, so building an index one chunk at a time is slow (108 s for 163 chunks). Run calls in parallel, within your quota, for big loads [18].
- Only `FAISS.load_local(..., allow_dangerous_deserialization=True)` an index you built yourself, because it unpickles the file [18].

Create things in this order: the collection, then the vector index with the field names, then the KB. Wait for the KB to be ACTIVE before you add a data source [16; aws-core:amazon-bedrock `knowledge-bases-setup.md`]. The KB was Available about a minute after Create, and three PDFs synced in about 130 s [12]. Lab 12's pre-made index:

| Field | Value |
|---|---|
| Vector | `bedrock-knowledge-base-default-vector`: faiss, FP32, 1024 dimensions, `innerproduct`, M 16, ef_construction and ef_search 512 |
| Text | `AMAZON_BEDROCK_TEXT_CHUNK`, filterable |
| Metadata | `AMAZON_BEDROCK_METADATA`, not filterable |

Chunking is set when you create the data source (ranges from the botocore model: overlap 1 to 99, buffer 0 to 1, breakpoint 50 to 99) [16]:

```python
fixed = {"chunkingStrategy": "FIXED_SIZE",
         "fixedSizeChunkingConfiguration": {"maxTokens": 300, "overlapPercentage": 20}}
semantic = {"chunkingStrategy": "SEMANTIC",
            "semanticChunkingConfiguration": {"maxTokens": 300, "bufferSize": 1, "breakpointPercentileThreshold": 95}}
agent.create_data_source(
    knowledgeBaseId=kb_id, name="fixed",
    dataSourceConfiguration={"type": "S3", "s3Configuration": {
        "bucketArn": "arn:aws:s3:::BUCKET", "inclusionPrefixes": ["docs/"]}},   # one prefix: the API limit
    vectorIngestionConfiguration={"chunkingConfiguration": fixed})
```

- On the spot-training question, fixed-size returned three on-topic chunks (1.826, 1.820, 1.815). Semantic returned one on-topic chunk that stopped before the answer row (1.857) and two unrelated ones (1.569, 1.477) [16].
- Read `statistics.numberOfDocumentsFailed` on the ingestion job even when it says COMPLETE [16].
- Give an FAQ CSV a header row, or split it into one document per question and answer. Without a header, the managed KB seemed to repeat row 1 in every chunk, so all the citation previews matched. The vector KBs cut chunks across rows and kept the raw quotes [16].
- For bulk embedding, give the Bedrock client `Config(retries={"max_attempts": 10, "mode": "adaptive"})` [17].
- Check bulk responses: raise when `errors` is true, and keep records JSON-safe (no NaN or `pd.NA`) [17].
- Serverless vector indexes refresh every 5 seconds on NextGen and every 60 on Classic, so a search straight after indexing can miss new documents (AWS docs, serverless-vector-search).

## Queries

Embeddings ignore negation, so filter "not by this author" explicitly [17]:

```json
{
  "size": 5,
  "query": {
    "bool": {
      "must": [{"knn": {"embedding": {"vector": [0.01, -0.02], "k": 5}}}],
      "must_not": [{"term": {"author.keyword": "Jane Austen"}}]
    }
  }
}
```

A `must_not` around a `knn` clause runs after the nearest neighbours are found, so you can get fewer than `size` hits. With the Lucene or Faiss engine, a `filter` inside the `knn` clause filters during the search instead.

```json
{"size": 5, "_source": {"excludes": ["embedding"]},
 "query": {"knn": {"embedding": {"vector": [0.01, -0.02], "k": 5,
   "filter": {"bool": {"must_not": [{"term": {"author.keyword": "Jane Austen"}}]}}}}}}
```

Lab 17's helper returned `hits[1]` and skipped the best match. Read the lab's helpers before you reuse them [17].

- Leave the vector out of `_source`, or every hit carries 1,024 floats [17]. NextGen Serverless vector collections leave it out by default (AWS docs).
- Lab 17's 5-shard index still returned 3 hits at k=3 after a `must_not` removed two. Don't predict the candidate count from k; test with your shard count [17].
- Putting "something different" in the prompt instead of a filter let the model recommend a book that wasn't in the index. Filter the retrieval, then tell the model to choose only from the records given [17].
- A one-shot example's content leaked into the answer as if the reader knew it [17].

## Media sources (Transcribe)

- Decode S3 event keys with `urllib.parse.unquote_plus`, and take the media format from the file extension [11].
- Name the transcription job from the key and ETag, so a repeated event can't start a second job. Treat `ConflictException` as already started, unless that job FAILED [11].
- Filter the trigger by prefix, act only on ObjectCreated events, and write output to another bucket so the function can't trigger itself [11].

```python
import re
from urllib.parse import unquote_plus

MEDIA_FORMATS = {"mp3", "mp4", "wav", "flac", "ogg", "amr", "webm", "m4a"}

def job_name(key: str, etag: str) -> str:
    stem, _, ext = unquote_plus(key).rsplit("/", 1)[-1].rpartition(".")
    stem = re.sub(r"[^0-9A-Za-z._-]+", "-", stem).strip("-.") or "media"
    tag = re.sub(r"[^0-9A-Za-z]", "", etag)[:12] or "noetag"
    suffix = f"-{ext.lower()}-{tag}"
    return stem[: 200 - len(suffix)] + suffix   # Transcribe allows 1 to 200 of [0-9a-zA-Z._-]
```

- A file called `Team Call.mp3` arrives as `Team+Call.mp3`, and the lab always sent `MediaFormat="mp4"` [11].
- The retry path also needs `transcribe:GetTranscriptionJob` and `transcribe:DeleteTranscriptionJob` [11].
- Check settings before the first job: the lab's `OUTPUT_BUCKET` was the placeholder `TO_BE_CHANGED`, and async Lambda retries a failing event twice before dropping it [11].
- Log the key and the outcome only. Printing the whole event logs the uploader's IP address and principal ID [11].
- Read only `*.json` from the output bucket (Transcribe also writes an empty `.write_access_check_file.temp`), and skip empty transcripts [11].
- Chunk transcripts before embedding: 1,500 characters with 200 of overlap matched the lab notebook. Whole short clips (1 to 3 KB) fit, but an hour-long recording is past the input limit [11].
- The DIY upload was transcribed 11 seconds after Transcribe started writing [11].

## Console steps that tripped people

- The KB wizard's S3 picker filled `s3://BUCKET/rag_data/x`. Delete the stray part before Create [16].
- "Create Managed KB" moved under the cursor when a banner loaded. Look again before you click on a page that is still loading [16].
- Labels changed since the lab text: "Knowledge Base with vector store" is now Unstructured Vector Store KB, and "Retrieval only: data sources" is Standard retrieval only [12].
- The vector-store dropdown ignored clicks. Focus it, press Enter, ArrowDown to the option, then Enter [12].
- Transcribe rejected an output path typed as `s3://BUCKET/`; Browse S3 fills `s3://BUCKET` without the slash [11].
- To open Streamlit inside Studio, replace `/lab` in the JupyterLab URL with `/proxy/8501/<path>`, and keep the trailing slash [11, 16].
- The Lambda S3 trigger form keeps Add disabled until you tick the recursive-invocation acknowledgement [11].
- The OpenSearch access-policy box is an Ace editor that auto-closes brackets. Set it with `env.editor.setValue(text, 1)` and read it back [17].
