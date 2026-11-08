# aws-ai-builder

A Claude Code skill for building, reviewing and debugging generative and agentic AI on AWS. It covers:

- Bedrock: Converse, Guardrails and Knowledge Bases
- AgentCore: Runtime, Gateway, Memory, Identity, Policy and Evaluations
- Strands and MCP on AWS
- the AWS services these apps lean on: data stores, streaming, AI services, API Gateway and Lambda, private networking and SageMaker

The rules come from 22 AWS SimuLearn labs and 12 FDE microcredential labs. In many of those labs the validator passed while the system was still broken.

## Install

```bash
git clone https://github.com/quincysting/claudeskill_aws_ai_builder ~/.claude/skills/aws-ai-builder
```

Claude Code loads the skill when a task matches the description in `SKILL.md`, or when you ask for it by name.

## What's inside

- `SKILL.md`: eight rules, the build and review workflows, and a debug-by-symptom table
- `references/`: 13 files with snippets, covering:
  - one topic each, from models and guardrails to agent identity and data stores
  - a review checklist
  - lab notes
- `scripts/`: 14 Python helpers, each with its tests

## Tests

```bash
cd ~/.claude/skills/aws-ai-builder
python -m unittest discover -s scripts
```

- You need Python 3.10 or later.
- The tests make no AWS calls.
- Some use botocore, PyJWT, cryptography, moto, opentelemetry-sdk or mcp when they're installed, and skip without them.
- On 2026-11-08 there were 242 tests: 241 passed and 1 was skipped.

## How far it's checked

The facts in the references were checked against the AWS docs in three passes:

- The first two passes covered 151 claims.
- A third pass rechecked the 27 that neither pass could settle.
- Three of those are still unconfirmed, and the text labels them as lab observations.

The scripts were checked at different depths:

- `kb_rag.py` ran against live Knowledge Bases, and `asl_lint.py` against real state machine definitions.
- `test_real_libraries.py` runs four more scripts against moto's DynamoDB, PyJWT, and the OpenTelemetry and MCP SDKs.
- The rest are tested against fakes.

None of the FDE scripts has run against live AWS, so try them on a test account first.
