# Private networking and IAM

Lab tags in brackets name the lab that showed each rule: [16] is SimuLearn lab 16, [FDE1-3] is FDE1 lab 3, [FDE2-2] is FDE2 session 2. Their notes are under `D:\AI\AWSAI\` when that folder is on the machine; the rules stand on their own without it.

Contents:

- Reaching Bedrock from a private subnet
- Gateway and interface endpoints
- SageMaker in a private VPC
- What decides every call, and which denial is which
- Restricting a resource to an endpoint
- Least privilege examples from the labs
- Service Catalog for self-service resources
- Console steps that tripped people

## Reaching Bedrock from a private subnet

- With no route and no endpoint, the call hangs until the function times out, and the error gives no cause [9].
- For model calls, create the interface endpoint `com.amazonaws.<region>.bedrock-runtime`. The `bedrock` endpoint is the control plane. Agents and KB queries use `bedrock-agent-runtime` [9].
- The endpoint's security group must allow 443 from the function's security group. Turn on private DNS so the SDK's normal hostname resolves to the endpoint, with no code change [9].
- Every AWS service the function calls needs its own route: Secrets Manager, STS, CloudWatch Logs and so on [9].
- Put the endpoint in every AZ the function runs in. Adding a second AZ to the endpoint doesn't make the function or the database multi-AZ [9].

Bedrock's endpoint suffixes (Bedrock user guide, vpc-interface-endpoints):

| `com.amazonaws.REGION.` | Calls |
|---|---|
| `bedrock-runtime` | InvokeModel, Converse, ConverseStream, ApplyGuardrail |
| `bedrock` | Control plane, such as creating and updating guardrails |
| `bedrock-agent` | Agents and Knowledge Bases build time, such as CreateKnowledgeBase and StartIngestionJob |
| `bedrock-agent-runtime` | Retrieve, RetrieveAndGenerate, InvokeAgent |
| `bedrock-mantle` | Bedrock Mantle API actions |

```python
ec2.create_vpc_endpoint(
    VpcEndpointType="Interface", VpcId=vpc_id,
    ServiceName=f"com.amazonaws.{region}.bedrock-runtime",
    SubnetIds=[subnet_a, subnet_b],                 # one subnet per AZ the callers run in
    SecurityGroupIds=[endpoint_sg],                 # inbound 443 from the callers' security group
    PrivateDnsEnabled=True,                         # off: pass endpoint_url to every client
    PolicyDocument=json.dumps(endpoint_policy),
)
```

The default endpoint policy allows full access. Name the models instead [9]:

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": "*",
    "Action": ["bedrock:InvokeModel"],
    "Resource": ["arn:aws:bedrock:REGION::foundation-model/amazon.nova-micro-v1:0",
                 "arn:aws:bedrock:REGION::foundation-model/amazon.nova-pro-v1:0"]
  }]
}
```

- `Principal: "*"` here means anyone in the VPC whose own permissions allow the call. It is a second limit, not an identity control [9].
- Converse is covered by `bedrock:InvokeModel`. Streaming needs `bedrock:InvokeModelWithResponseStream` as well [9].
- Timings seen in lab 9: attaching the function to the VPC took about 3.5 minutes; the first call through a new endpoint took 54.1 s against a 60 s limit and the next 2.6 s; a second AZ was Available in about 36 s, and calls took about 15.7 s instead of 0.34 s for the next couple of minutes [9].
- Budget every call in the path, not just the model. Lab 9's module allowed 8 s for the secret, 20 s for the database and 28 s for the model: 56 s against a 60 s function. `read_timeout` applies per read, and a database connection takes several round trips [9].
- Short timeouts turn a dead route into a named error instead of a function timeout, but they also remove automatic retries on throttling [9].

## Gateway and interface endpoints

| | Gateway | Interface |
|---|---|---|
| Services | S3, DynamoDB | Most others |
| Attaches to | Route tables | Subnets, as an ENI |
| Security group | None | Yes, allow 443 |
| DNS | Not needed | Private DNS option |
| Cost | No hourly charge | Hourly, per AZ, plus data |
| Ready in lab 19 | Available at once | Pending, then Available in about 2 minutes |

Pulling an image from ECR in a private subnet needs `ecr.api`, `ecr.dkr` and the S3 gateway endpoint, because image layers come from S3 [19]. `scripts/secure_sm_catalog.py ecr-gaps --vpc-id ...` lists what's missing, and `check-endpoint` compares an endpoint with the settings you expected.

| Endpoint | Why |
|---|---|
| `com.amazonaws.REGION.ecr.api` (Interface) | ECR API calls such as `GetAuthorizationToken` and `DescribeImages` |
| `com.amazonaws.REGION.ecr.dkr` (Interface, private DNS required) | The Docker registry API that `docker pull` and `push` use |
| `com.amazonaws.REGION.s3` (Gateway) | Image layers |

- Lab 19's DIY validated `ecr.api` alone, which isn't enough for a pull [19].
- `ecr-gaps` checks only that the endpoints exist, are available and (for `ecr.dkr`) have private DNS. Neither command reads security-group rules [19].
- Private DNS needs the VPC's DNS hostnames and DNS support turned on [19].
- The service filter lists two `com.amazonaws.REGION.s3` rows, one Gateway and one Interface. Pick by the Type column [19].

## SageMaker in a private VPC

| Endpoint | For |
|---|---|
| `com.amazonaws.REGION.sagemaker.api` | The SageMaker API, including `CreatePresignedNotebookInstanceUrl` and `CreatePresignedDomainUrl` |
| `com.amazonaws.REGION.sagemaker.runtime` | `InvokeEndpoint` from functions and apps in the VPC |
| `aws.sagemaker.REGION.notebook` | The notebook instance UI |
| `aws.sagemaker.REGION.studio` | Studio |
| `com.amazonaws.REGION.sts`, `logs` | Role sessions and logs. Lab 19's lifecycle config pointed STS at the regional endpoint [19] |
| `com.amazonaws.REGION.s3` (Gateway) | Training data, model artifacts, image layers |
| `com.amazonaws.REGION.servicecatalog` | SageMaker Projects templates in Studio |

Sources: SageMaker developer guide (interface-vpc-endpoint, notebook-interface-endpoint, studio-interface-endpoint), re:Post sagemaker-studio-jupyterlab-connect, and lab 19, whose VPC had the `sagemaker.api`, `sagemaker.runtime`, `sts` and `logs` endpoints [19].

- Create the `sagemaker.api` endpoint along with the notebook or Studio endpoint (before it, for notebooks), so the presigned-URL call also goes through the VPC (SageMaker developer guide).
- An endpoint doesn't stop people outside the VPC from opening the notebook. Deny the presigned-URL call unless it comes through your endpoints, for every principal that could make it. List both the API endpoint and the notebook endpoint (SageMaker developer guide; the Deny shape is from [19]):

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Sid": "NotebookOnlyThroughTheVpc",
    "Effect": "Deny",
    "Action": "sagemaker:CreatePresignedNotebookInstanceUrl",
    "Resource": "*",
    "Condition": {"StringNotEquals": {"aws:SourceVpce": ["vpce-EXAMPLE-api", "vpce-EXAMPLE-notebook"]}}
  }]
}
```

- This also blocks Open JupyterLab in the console, and the URL, once issued, works from anywhere on the internet (SageMaker developer guide).
- Set `DirectInternetAccess=Disabled`, and `RootAccess=Disabled` unless users need it. Lab 19's template defaulted root access to Enabled [19].
- Run training jobs in the VPC with `VpcConfig` (private subnets and security groups) and an S3 gateway endpoint for data and artifacts. Subnets in several AZs give SageMaker a larger capacity pool (SageMaker developer guide, train-vpc and train-get-capacity).
- `EnableNetworkIsolation` blocks the container's outbound calls, while input channels and logs still work. With isolation on, the container gets no AWS credentials, with or without `VpcConfig` (re:Post sagemaker-access-network-isolation).
- A function in a private subnet that calls an endpoint needs the `sagemaker.runtime` endpoint, and fails the same way as lab 9's Bedrock call without one (SageMaker developer guide; [9]).

## What decides every call, and which denial is which

1. The network path (a route or an endpoint, and security groups). A failure here gives no denial, only a timeout.
2. The endpoint policy.
3. The role's identity policy, plus any resource policy, permission boundary, SCP or RCP.

A policy denial names the policy type, for example "with an explicit deny in a VPC endpoint policy". The IAM policy simulator doesn't reliably model endpoint policies, so confirm with a real call from inside the VPC [9].

| Cause | What you see |
|---|---|
| No route and no endpoint | A hang until the function timeout (60 s in lab 9). With short timeouts, `ConnectTimeoutError` or `EndpointConnectionError` [9] |
| Database with no route | `OperationalError 2003` after about 10 s [9] |
| Endpoint policy | AccessDenied "... because no VPC endpoint policy allows the bedrock:InvokeModel action" [9] |
| Role policy | AccessDenied "... because no identity-based policy allows the bedrock:InvokeModel action" [9] |
| Policy change still applying | The old denial for about a minute after the save, while the policy read back correctly [9] |

Name the layer in the logs [9]:

```python
def diagnose(err) -> str:
    if "secretsmanager:" in str(err) or "//secretsmanager." in str(err):
        return "secrets"
    if type(err).__name__ in {"ConnectTimeoutError", "EndpointConnectionError"}:
        return "no_route"
    message = getattr(err, "response", {}).get("Error", {}).get("Message", "")
    if "VPC endpoint policy" in message:
        return "endpoint_policy"
    if "identity-based policy" in message:
        return "role_policy"
    return "unknown"
```

- Change one layer at a time when testing. Setting Nova Pro before widening the endpoint policy showed the endpoint denial first and the role denial after it [9].
- Check the role with the simulator, and the endpoint separately [9]. In FDE2-1 each agent's role had to return `allowed` for its own store and `implicitDeny` for every other store [FDE2-1]:

```bash
aws iam simulate-principal-policy --policy-source-arn arn:aws:iam::111122223333:role/APP_ROLE \
  --action-names bedrock:InvokeModel --resource-arns arn:aws:bedrock:REGION::foundation-model/MODEL_ID \
  --query 'EvaluationResults[0].EvalDecision' --output text     # allowed, implicitDeny or explicitDeny
```

- Read every inline policy's JSON. In FDE2-1 an inline policy with a harmless name gave the vector agent's role access to the memory table [FDE2-1].

Converse needs `bedrock:InvokeModel` and ConverseStream needs `bedrock:InvokeModelWithResponseStream`. Scope both to the model ARNs you use. With a cross-Region inference profile, grant the profile ARN plus the foundation model ARN in every Region it can route to. An SCP that denies other Regions breaks those calls [9].

## Restricting a resource to an endpoint

A condition on an Allow narrows only that statement, and any other Allow still grants access. To keep a bucket private to an endpoint, deny everything else, and exempt an admin role so you can't lock yourself out [19]:

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Sid": "OnlyThroughTheEndpoint",
    "Effect": "Deny",
    "Principal": "*",
    "Action": "s3:*",
    "Resource": ["arn:aws:s3:::BUCKET", "arn:aws:s3:::BUCKET/*"],
    "Condition": {
      "StringNotEquals": {"aws:SourceVpce": "vpce-0123456789abcdef0"},
      "ArnNotLike": {"aws:PrincipalArn": "arn:aws:iam::111122223333:role/StorageAdmin"}
    }
  }]
}
```

Conditions under different operators must all match, so the Deny applies only to requests that come from outside the endpoint and from someone other than the admin role. Console requests don't come through your VPC endpoint, so this also blocks console browsing for everyone except the admin role [19].

`scripts/secure_sm_catalog.py policy s3 --bucket B --vpce-id V --exempt-principal-arn A` writes this policy. `audit-policy file.json --vpc-id ...` warns about an Allow that restricts nothing, a Deny that never fires, a placeholder ID, or an endpoint that isn't in the VPC.

A request that doesn't come through any endpoint has no `aws:SourceVpce` key at all. How each operator treats the missing key decides whether a statement fires [19, from the IAM condition operator docs]:

| Operator | Missing key evaluates to |
|---|---|
| `StringEquals`, `StringLike` | false |
| `StringNotEquals`, `StringNotLike`, `ArnNotLike` | true |
| Any `...IfExists` | true |
| `ForAllValues:` | true |
| `ForAnyValue:` | false, even when negated |

- So Deny with `StringNotEquals` catches outside requests, and Deny with `ForAnyValue:StringNotEquals` never fires. Allow with `StringEqualsIfExists` or `ForAllValues:StringEquals` lets everyone in. `aws:SourceVpce` is single-valued, so leave the set operators off it [19].
- Condition key names are case-insensitive (`aws:sourceVpce` is `aws:SourceVpce`), but the values in `StringEquals` aren't [19].
- An ECR repository policy has the same shape with the pull actions (or `ecr:*`) and no `Resource` element. Pulls come through `ecr.dkr` and API calls through `ecr.api`, so list both endpoint IDs if both must work [19].
- `aws:SourceVpc` (the VPC ID) is looser: it matches any endpoint in that VPC [19].
- An endpoint ID that doesn't exist turns a Deny into "deny everyone". Lab 19's ECR policy named an ID that wasn't in the account, and the lab said to leave it [19].
- If you lock yourself out, fix the policy from inside the VPC. For S3, the account root user can also delete the bucket policy [19].

## Least privilege examples from the labs

- Generated SQL runs as a role that can only read [3].
- The function role lists exact model ARNs. Lab 9's role allowed nova-lite and nova-micro but not the Nova Pro it called, and the validator never noticed [9].
- No `ec2:*VpcEndpoint*` on `*` for a function [9], and no PutItem on every table [20].
- OpenSearch domain policies must not allow `Principal "*"` with `es:*`, and notebooks must not print master credentials [17].
- Give each agent its own role, scoped to its own store [FDE2-1].
- A "read-only" session mode still ran procedures. The real boundary is a read-only database identity: Neo4j without `EXECUTE PROCEDURE`, or Neptune with only `neptune-db:ReadDataViaQuery` [FDE2-1].
- Run governed queries from an allow-list of parameterized SQL in a `READ ONLY` transaction, and raise on an unknown name [FDE2-1].
- Give a notebook's execution role only the use actions on its KMS key (`Encrypt`, `Decrypt`, `GenerateDataKey*`, `DescribeKey`, `CreateGrant`), never `PutKeyPolicy` or `ScheduleKeyDeletion` [19].
- Check the resource type in each statement. Lab 19's role scoped `CreateTrainingJob`, `CreateEndpoint` and `DescribeTrainingJob` to a lifecycle-config ARN, so those statements granted nothing [19].
- Scope `iam:PassRole` to the role ARNs a service needs. PassRole on `*` with SageMaker, Lambda or CloudFormation create calls lets the caller pass any role, including an admin one (aws-core:aws-iam).

## Service Catalog for self-service resources

- A launch constraint gives provisioning to a launch role, so end users only need Service Catalog actions [19].
- A launch constraint is set per product in each portfolio, not for the portfolio as a whole. Lab 19 saw a changed constraint apply to new launches [19]. The docs also use the launch role when a user updates or terminates a provisioned product, so test what a change does to products already launched before you rely on either.
- A template parameter with one allowed value forces a setting such as `DirectInternetAccess=Disabled` or `RootAccess=Disabled` [19].
- Lab 19's template let the role delete its own KMS key and left root access on by default. Read templates before you publish them [19].

Publishing is five calls (`publish_product` in `scripts/secure_sm_catalog.py`) [19]:

```python
port = sc.create_portfolio(DisplayName="ML portfolio", ProviderName="Platform")["PortfolioDetail"]["Id"]
prod = sc.create_product(Name="Private notebook", Owner="Platform", ProductType="CLOUD_FORMATION_TEMPLATE",
    ProvisioningArtifactParameters={"Name": "1", "Type": "CLOUD_FORMATION_TEMPLATE",
                                    "Info": {"LoadTemplateFromURL": template_url}})
pid = prod["ProductViewDetail"]["ProductViewSummary"]["ProductId"]
sc.associate_product_with_portfolio(ProductId=pid, PortfolioId=port)
sc.create_constraint(PortfolioId=port, ProductId=pid, Type="LAUNCH",
                     Parameters=json.dumps({"RoleArn": "arn:aws:iam::111122223333:role/LaunchRole"}))
sc.associate_principal_with_portfolio(PortfolioId=port, PrincipalType="IAM",
                                      PrincipalARN="arn:aws:iam::111122223333:role/EndUserRole")
```

- The launch role needs rights on everything the template creates (CloudFormation, SageMaker, IAM, KMS and S3 in lab 19) [19].
- As the end user, Service Catalog showed only Products and Provisioned products, and SageMaker listed the new notebook but refused `DescribeNotebookInstance` [19].
- The launch took about 8 minutes, because the template's custom resource created the notebook and polled every 200 s [19].
- A custom resource must PUT exactly one result to `ResponseURL`, or the stack waits until it times out. Lab 19's handler used the Lambda ARN as `PhysicalResourceId` for every notebook, and could send FAILED and then SUCCESS on Delete [19].
- The native resource does the same job with no Lambda (a sketch, not deployed in the lab) [19]:

```yaml
Notebook:
  Type: AWS::SageMaker::NotebookInstance
  Properties:
    InstanceType: ml.t3.medium
    RoleArn: !GetAtt ExecutionRole.Arn
    SubnetId: !Ref SubnetId                 # pass IDs, not Name tags
    SecurityGroupIds: [!Ref SecurityGroupId]
    DirectInternetAccess: Disabled
    RootAccess: Disabled
    KmsKeyId: !Ref NotebookKey
```

- `Ref` on an IAM role returns its name, not its ARN. Lab 19's `ExecutionRoleArn` output held a name [19].

## Console steps that tripped people

- The endpoint subnet dropdown also offers public and database subnets. Choose one private subnet per AZ [9].
- The VPC console draws its forms in a same-origin iframe, and the policy box is an Ace editor. Set it with `env.editor.setValue`, then reload and read it back [9].
- `#EndpointDetails:vpcEndpointId=...` and `#ModifyVpcEndpointPolicy:vpcEndpointId=...` open directly. A guessed page name lands on the VPC home page [9].
- Switch role runs on the AWS sign-in domain. Copy the end-user role's full name, suffix included, before you start [19].
- The Service Catalog product list didn't refresh after Create product. Refresh before you assume it failed [19].
- The EC2 console's security-group page didn't load in lab 19; the same page in the VPC console did [19].
- Lab roles are narrow: the notebook role couldn't call `lambda:GetFunction` [FDE2-3], and Studio roles couldn't call `es:DescribeDomain` [17] or `transcribe:GetTranscriptionJob` [11]. Plan to do those steps in the console.
