# Fine-tuning, pipelines and evaluation

Lab tags in brackets name the lab that showed each rule: [16] is SimuLearn lab 16, [FDE1-3] is FDE1 lab 3, [FDE2-2] is FDE2 session 2. Their notes are under `D:\AI\AWSAI\` when that folder is on the machine; the rules stand on their own without it.

Contents:

- PEFT and LoRA
- Instruction fine-tuning a causal LLM
- Training that stops in time
- Rewards and classifiers
- Judging a result
- Serving a LoRA adapter on SageMaker
- Invoking the endpoint from Lambda
- Step Functions for ML
- CI/CD
- Cost

## PEFT and LoRA

```python
from peft import PeftModel
from transformers import AutoModelForSeq2SeqLM

base = AutoModelForSeq2SeqLM.from_pretrained("google/flan-t5-base")
policy = PeftModel.from_pretrained(base, "./adapter-dir/", is_trainable=True)  # default False loads it frozen
```

- Ship the adapter, not a copy of the model: 14 MB against a 990 MB base, with 1.41% of parameters trainable [21]. `PeftModel.from_pretrained` has no `lora_config` parameter (it is `config`), so passing `lora_config=` changes nothing and the saved `adapter_config.json` decides the setup [21].
- Pin trl, transformers and peft together, and keep the serving container's library versions close to the training ones [21, 22, FDE2-3].

| Run | Base | LoRA | Trainable | Artifact |
|---|---|---|---|---|
| Lab 21 | FLAN-T5-base | r 32 on `q` and `v` | 3.54M of 251M (1.41%) | 14 MB adapter |
| Lab 22 | Pythia-2.8B in 8-bit | r 256, alpha 512 | 83.9M of 2.86B (2.93%) | 785 MB `lora_model.tar.gz` |
| FDE2-3 | Dolly-v2-3B in 8-bit | r 256, alpha 512 | 83.9M of 2.86B (2.93%) | about 311 MB: adapter plus serving code |

## Instruction fine-tuning a causal LLM

Wrap each `instruction`, `response` row in one fixed template, make its markers special tokens, and compute the loss on the answer only [FDE2-3]:

```python
import numpy as np
from peft import LoraConfig, get_peft_model, prepare_model_for_kbit_training
from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig, DataCollatorForLanguageModeling

INSTRUCTION, RESPONSE, END = "### Instruction:", "### Response:\n", "### End"
INTRO = "Below is an instruction that describes a task. Write a response that appropriately completes the request."
PROMPT = f"{INTRO}\n{INSTRUCTION}\n{{instruction}}\n{RESPONSE}{{response}}\n{END}"

tok = AutoTokenizer.from_pretrained(BASE, padding_side="left")
tok.pad_token = tok.eos_token
tok.add_special_tokens({"additional_special_tokens": [END, INSTRUCTION, RESPONSE]})   # one id each
model = AutoModelForCausalLM.from_pretrained(BASE, device_map="auto",
                                             quantization_config=BitsAndBytesConfig(load_in_8bit=True))
model.resize_token_embeddings(len(tok))
model = prepare_model_for_kbit_training(model)   # older peft had prepare_model_for_int8_training, removed in 0.10.0
model = get_peft_model(model, LoraConfig(r=256, lora_alpha=512, lora_dropout=0.05, task_type="CAUSAL_LM"))

class CompletionOnly(DataCollatorForLanguageModeling):   # use with mlm=False
    def torch_call(self, examples):
        batch = super().torch_call(examples)
        marker = self.tokenizer.convert_tokens_to_ids(RESPONSE)
        for i in range(len(examples)):
            hits = np.where(batch["labels"][i] == marker)[0]
            if len(hits) == 0:
                raise RuntimeError("response marker missing; was it added as a special token?")
            batch["labels"][i, : hits[0] + 1] = -100    # no loss on the prompt
        return batch
```

- The special tokens do two jobs: the collator finds the answer's start by one token id, and inference stops at `### End` and slices out the answer. Raise on an empty instruction or response, so a blank answer never becomes training data [FDE2-3].
- Tokenize with `max_length=256` and drop rows that would be cut. In lab 22, 147 of 154 pairs fit, split 133 for training and 14 for evaluation [22, FDE2-3].
- The 8-bit 3B base took about 3 GB on a 16 GB T4, and training used about 5.9 GB at 75 to 85% GPU. Training took about 8 to 10 minutes there [FDE2-3]. Lab 22's 1,330 steps took 5 min 50 s on an ml.g5.4xlarge, at batch size 1 because the notebook defined a batch of 64 with gradient accumulation and passed neither [22].
- Shut down earlier kernels before you load the model, or the 8-bit load and training run out of GPU memory. Check with `nvidia-smi --query-gpu=memory.used,utilization.gpu --format=csv` [FDE2-3].
- Run long notebooks headless, one cell at a time as in the UI, so a browser hiccup can't stop them: `nohup jupyter nbconvert --to notebook --execute --ExecutePreprocessor.kernel_name=conda_pytorch --ExecutePreprocessor.timeout=-1 train.ipynb --output train.executed.ipynb &`. Pass the kernel name: FDE2-3's metadata said `conda_pytorch_p310` but the registered kernel was `conda_pytorch` (`jupyter kernelspec list`). `onnxruntime ... GPU device discovery failed` and `FutureWarning`s were harmless [FDE2-3].

## Training that stops in time

Small data overfits fast. Pythia-2.8B with LoRA rank 256 on 133 rows took validation loss from 2.096 to 3.555, and only the last weights were saved [22]. Evaluate every epoch and keep the best:

```python
from transformers import EarlyStoppingCallback, Trainer, TrainingArguments

args = TrainingArguments(output_dir="out", per_device_train_batch_size=8, num_train_epochs=10,
                         eval_strategy="epoch", save_strategy="epoch", save_total_limit=2,
                         load_best_model_at_end=True, metric_for_best_model="eval_loss")
trainer = Trainer(model=model, args=args, train_dataset=train, eval_dataset=val,
                  callbacks=[EarlyStoppingCallback(early_stopping_patience=2)])
```

Older transformers releases call it `evaluation_strategy`.

| Run | Training loss | Validation loss | Best point |
|---|---|---|---|
| Lab 22: 133 rows, 10 epochs | 2.166 to 0.007 | 2.096 at step 100, 3.555 at step 1,330 | Step 100; `save_steps=20000` kept no checkpoint [22] |
| FDE2-3 Practice: 58 rows | about 2.1 to 0.005 | about 2.1 to 3.5 | The first evaluation [FDE2-3] |
| FDE2-3 DIY: 10 epochs, evaluated every 100 steps | 2.145 to 0.025 | 2.238 at step 100, 3.572 at step 500 | Step 100 [FDE2-3] |

All three have the same shape: validation loss is lowest at the first evaluation and climbs while training loss falls. Read both curves before you package anything. The labs' r 256 is the rank that overfit; try a lower rank, more dropout or fewer epochs [FDE2-3].

## Rewards and classifiers

Read labels by name. A text-classification pipeline returns labels sorted by score, so position 0 changes from text to text. Lab 21's pipeline read `[0]` and rewarded toxic text [21].

```python
raw = clf(texts, top_k=None, function_to_apply="none")   # raw logits for every label

def label_score(scores: list[dict], label: str = "nothate") -> float:
    return next(float(s["score"]) for s in scores if s["label"] == label)
```

- Use raw logits as the PPO reward, and softmax probabilities only for reading. A compliment scored 4.65 on `nothate` and -4.18 on `hate` (P(hate) 0.00015); an insult scored -2.06 and 1.67 (P(hate) 0.977) [21].
- Lab 21's PPO ran 10 steps of 16 prompts at about 62 s a step on CPU. Mean returns were negative although scores were about +2, most likely because trl folds a per-token KL penalty into the reward. Ten steps is a demo, not training. The loop relies on trl 0.8's `step(queries, responses, scores)`, so keep `trl==0.8.0`, `transformers==4.47.0` and `peft==0.14.0` together [21].

## Judging a result

- Compare a change with its noise. Lab 21's 0.0136 gain sat inside a standard error of about 0.029 [21].
- Use a fixed evaluation set, several samples per prompt, and say which way the metric moved. Lab 21's "+37.6% improvement" was an increase in toxicity, which should read "37.59% higher (worse)" [21].
- Read the best and worst examples. Lab 21's top gain was a hallucinated summary [21].
- Check cheap ratios first. A length ratio of 7.98 made BLEU meaningless [15].
- Cosine similarity of answers measures topic overlap, not correctness [14].
- 100 samples per group give a hint, not a finding [15]. Personalize's minimum data proves the pipeline runs, not that the model is good [13].
- Read the evaluation loop too. Lab 21's broke on `i > num_samples`, so "10 samples" scored 11 prompts, and its `GenerationConfig` misspelt `top_k` as `tok_k`, so decoding used the default [21].

## Serving a LoRA adapter on SageMaker

Ship only the adapter and the serving code, tarred to S3: `adapter_model.bin` and `adapter_config.json`, `serving.properties`, a `requirements.txt` pinned to the training versions of transformers, peft and bitsandbytes, and a `model.py` whose `handle(inputs)` loads the 8-bit base and `PeftModel` once, then generates. `serving.properties` holds `engine=Python`, `option.entryPoint=model.py`, `option.adapter_checkpoint=<adapter folder>` and `option.adapter_name=<name>`. The base model downloads when the endpoint starts [FDE2-3]. Those keys are what the lab's DJL container used. `option.adapter_checkpoint` appears in neither the DJL docs nor the djl-serving source (searched 2026-11-08), and the current LMI docs serve adapters through `option.enable_lora` and inference components, so treat the two keys as lab-only and check them before you copy this.

```python
import boto3
from sagemaker import Model, image_uris
from sagemaker.djl_inference import DJLPredictor     # SageMaker Python SDK v2, as in the labs

image = image_uris.retrieve(framework="djl-deepspeed", version="0.22.1", region=region)   # the labs' image; see the LMI docs for current ones
model = Model(image_uri=image, model_data="s3://BUCKET/lora_model.tar.gz", role=role_arn,
              predictor_cls=DJLPredictor, name=name)
model.deploy(1, "ml.g4dn.2xlarge", endpoint_name=name, wait=False)
boto3.client("sagemaker").get_waiter("endpoint_in_service").wait(EndpointName=name)
```

- `deploy(wait=False)` runs CreateModel, CreateEndpointConfig and CreateEndpoint. The model is listed within seconds, long before the endpoint is InService [FDE2-3].
- The DIY endpoint on ml.g4dn.2xlarge was InService about 7.5 minutes after `deploy()`. Much more than 10 minutes usually means it failed, so read its CloudWatch logs [FDE2-3].
- The `endpoint_in_service` waiter polls every 30 s up to 120 times (60 minutes), and fails on Failed or a ValidationException (the botocore waiter model). `aws sagemaker wait endpoint-in-service` does the same from a shell [FDE2-3]. Don't poll with a bare `while status not in ("InService", "Failed")`. FDE2-3's deploy script did, so an endpoint that went OutOfService would loop forever, the trap lab 22's state machine had [FDE2-3, 22].
- Lab 22's serving `requirements.txt` pinned `transformers==4.28.1` and `peft==0.3.0` while training asked for `transformers>=4.38` and `peft>=0.7`, which can stop the adapter loading [22].
- In SimuLearn, FDE2-3's grader wanted two models, one from Practice and one from the DIY, and passed while the Practice endpoint was still Creating. Run the Practice deploy too [FDE2-3].

## Invoking the endpoint from Lambda

API Gateway (REST, `POST /generate`) calls a Lambda that reads `ENDPOINT_NAME` and calls `InvokeEndpoint` with `{"inputs": prompt}`. To serve a new model, change that variable; the code and the API stay the same [FDE2-3]. `UpdateFunctionConfiguration` replaces every variable, so merge first (see `models-and-prompts.md`) [5, 7].

The lab's function sent `Access-Control-Allow-Origin: *` and had no error handling [FDE2-3]. A version that follows `serverless-apps.md`:

```python
import json, logging, os

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError, ReadTimeoutError

from summarize_api import response   # scripts/summarize_api.py: CORS only for ALLOW_ORIGIN

smr = boto3.client("sagemaker-runtime", config=Config(          # stays under a REST API's 29 s
    connect_timeout=3, read_timeout=25, retries={"total_max_attempts": 1, "mode": "standard"}))
STATUS = {"ThrottlingException": 429, "ModelNotReadyException": 503, "ServiceUnavailable": 503}

def lambda_handler(event, context):
    try:
        prompt = str(json.loads(event.get("body") or "{}").get("prompt", "")).strip()
    except (ValueError, AttributeError):
        return response(400, {"error": "body must be a JSON object"})
    if not 0 < len(prompt) <= 4000:
        return response(400, {"error": "prompt must be 1 to 4000 characters"})
    try:
        out = smr.invoke_endpoint(EndpointName=os.environ["ENDPOINT_NAME"], ContentType="application/json",
                                  Body=json.dumps({"inputs": prompt}))      # the DJL handler reads "inputs"
        return response(200, {"output": json.loads(out["Body"].read())})
    except ReadTimeoutError:
        return response(504, {"error": "model timed out"})
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        logging.getLogger().warning("invoke_endpoint %s %s", code, e.response.get("OriginalStatusCode", ""))
        return response(STATUS.get(code, 502), {"error": "model call failed"})
    except ValueError:
        return response(502, {"error": "model reply was not JSON"})
```

| Error code | Meaning | Return |
|---|---|---|
| `ValidationError` | A bad request, or no such endpoint. The message can name the account | 502 |
| `ModelError` | The container returned 4xx or 5xx. Carries `OriginalStatusCode`, `OriginalMessage` and `LogStreamArn` | 502; log the status code and look in the log stream |
| `ModelNotReadyException` | A serverless variant still provisioning, or a multi-model endpoint still loading | 503, retry later |
| `ServiceUnavailable`, `InternalFailure` | Service side | 503 or 502 |
| `ReadTimeoutError` (botocore) | No reply within `read_timeout` | 504 |

Sources: botocore's SageMaker Runtime error shapes, and the SageMaker developer guide (InvokeEndpoint replies within 60 s, so behind a 29 s REST API keep the read timeout under 29 s). Log codes, never message text. Scope the role's `sagemaker:InvokeEndpoint` to that endpoint's ARN, as with model ARNs [9]. From a private subnet, add the `sagemaker.runtime` endpoint (see `network-and-iam.md`).

## Step Functions for ML

- Use `createTrainingJob.sync` rather than a background notebook. Lab 22's "training" was a notebook that ran for 15 minutes and left the instance running. Its run took 15 min 12 s, with 24 transitions and 6 polls of 150 s, and the endpoint was still Creating more than 15 minutes after the execution succeeded [22].
- `.sync` exists only for job-style integrations. `createEndpoint` returns as soon as the request is accepted, so poll it. This definition passes `scripts/asl_lint.py`:

```json
{
  "StartAt": "Create endpoint",
  "TimeoutSeconds": 3600,
  "States": {
    "Create endpoint": {
      "Type": "Task",
      "Resource": "arn:aws:states:::sagemaker:createEndpoint",
      "Parameters": {"EndpointName.$": "$.EndpointName", "EndpointConfigName.$": "$.EndpointConfigName"},
      "ResultPath": null,
      "Next": "Wait for endpoint"
    },
    "Wait for endpoint": {"Type": "Wait", "Seconds": 60, "Next": "Describe endpoint"},
    "Describe endpoint": {
      "Type": "Task",
      "Resource": "arn:aws:states:::aws-sdk:sagemaker:describeEndpoint",
      "Parameters": {"EndpointName.$": "$.EndpointName"},
      "ResultSelector": {"Status.$": "$.EndpointStatus"},
      "ResultPath": "$.endpoint",
      "Next": "Endpoint ready?"
    },
    "Endpoint ready?": {
      "Type": "Choice",
      "Choices": [
        {"Variable": "$.endpoint.Status", "StringEquals": "InService", "Next": "Done"},
        {"Or": [{"Variable": "$.endpoint.Status", "StringEquals": "Creating"},
                {"Variable": "$.endpoint.Status", "StringEquals": "Updating"}],
         "Next": "Wait for endpoint"}
      ],
      "Default": "Endpoint failed"
    },
    "Endpoint failed": {"Type": "Fail", "Error": "EndpointFailed"},
    "Done": {"Type": "Succeed"}
  }
}
```

- Loop only on the "still working" states (Creating, Updating) and send everything else, including Failed, OutOfService and RollingBack, to the Default exit. Lab 22's loop mapped Stopped to Pending, so it could run forever [22].
- `ResultPath: "$.x"` nests a result. The next state must read `$.x.Field`, not `$.Field`. That was lab 22's DIY bug, and `asl_lint.py` flags it [22].

```
python scripts/asl_lint.py definition.json --expect "Train,Create model,Create endpoint"
aws stepfunctions describe-state-machine --state-machine-arn ARN --query definition --output text | python scripts/asl_lint.py -
```

Exit 0 means no findings, 1 means findings, and 2 means the input isn't a state machine.

The data flow from training to model, as a fragment with only the fields that carry it [22]:

```json
"Train": {"Resource": "arn:aws:states:::sagemaker:createTrainingJob.sync", "ResultPath": "$.train"},
"Create model": {"Resource": "arn:aws:states:::sagemaker:createModel", "Parameters": {"PrimaryContainer": {
    "Image": "INFERENCE_IMAGE_URI", "ModelDataUrl.$": "$.train.ModelArtifacts.S3ModelArtifacts"}}}
```

- Use a Standard workflow. Express workflows support neither `.sync` nor `.waitForTaskToken` (aws-core:aws-serverless `orchestration.md`).
- Train with a training image. Lab 22's DIY ran the DJL inference image as its training image, with a 300-second limit [22].
- Retry the SageMaker states on `ThrottlingException` and `SageMaker.AmazonSageMakerException`. Lab 22 used a 5 s interval, 60 attempts and a backoff rate of 1.25 [22].
- A readiness check on an S3 artifact must also check its age. Lab 22's status Lambda accepted any object at the key, so an artifact left by an earlier run would pass at once. Compare `LastModified` with the run start, or write to a run-specific key [22].
- A Choice with no Default fails with `States.NoChoiceMatched` when nothing matches [22].
- With the Step Functions Data Science SDK, `Workflow.create()` doesn't update an existing machine: stepfunctions 2.3.0 logs an error and returns the existing ARN. A timestamped name makes a new machine on every build, and in `strftime`, `%m` is the month and `%M` the minutes [22].

## CI/CD

- In lab 22, CodePipeline deployed the workflow and didn't run it, and each build created a new state machine. Decide which one you want, and start the execution explicitly if needed [22].
- In the pipeline's Role ARN search box, the first option is "Use: typed text". Picking it stores a bare name, and Create fails at the end with `roleArn ... must satisfy regular expression arn:aws...`. Choose the entry that shows the full ARN [22].
- Create project opens CodeBuild in a pop-up that shows a "leave page?" dialog as it closes. Accept it; the project already exists. Creating the pipeline also starts its first run. An S3 source makes the console create an EventBridge rule with `PollForSourceChanges=false` [22].
- Pin build dependencies exactly. Lab 22's buildspec installed `sagemaker<3.0`, `setuptools<70` and `stepfunctions` 2.3.0; pip reported conflicts and the build still passed [22].

## Cost

- A Personalize recommender bills every hour it exists. Delete it when you're done [13].
- Notebook instances bill until stopped. Use training jobs, or a lifecycle config that stops idle notebooks [22].
- A lifecycle config that starts training with `nohup ... &` returns at once, and nothing stops the instance afterwards: lab 22's ml.g5.4xlarge stayed InService [22].
- Metric libraries can be big: the Regard metric downloads about 880 MB [15].
- Real-time endpoints bill per instance-hour until deleted. Give each test endpoint an owner and delete it, with its endpoint config, when the test ends [22].
