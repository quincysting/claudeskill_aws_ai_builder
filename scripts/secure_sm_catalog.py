"""Self-service SageMaker behind VPC endpoints, from SimuLearn lab 19 (Provision SageMaker in a Secure
Environment).

An admin publishes a CloudFormation template as an AWS Service Catalog product with a launch role, and
grants an end-user role access to the portfolio. The end user can launch the product but can't create the
resources directly. Inside the VPC, S3 is reached through a gateway endpoint and ECR through interface
endpoints, and resource policies are meant to accept only requests that arrive through those endpoints.

- `publish_product`: portfolio + product (template URL) + LAUNCH constraint + principal access.
- `endpoint_request`: kwargs for ec2.create_vpc_endpoint (Gateway for s3/dynamodb, Interface otherwise).
- `check_endpoint`: what's wrong with an existing endpoint, against the settings you expected.
- `missing_for_ecr_pull`: which endpoints a private subnet still needs before `docker pull` from ECR works.
- `vpce_only_bucket_policy` / `vpce_only_ecr_policy`: Deny unless aws:SourceVpce matches.
- `audit_vpce_policy`: warnings for aws:SourceVpce conditions that don't do what they look like they do
  (an Allow that blocks nothing or allows everything, a Deny that never fires, a placeholder, an endpoint id
  that isn't in the VPC, a Deny with no way back in).
- CLI: `check-endpoint`, `ecr-gaps`, `policy`, `audit-policy`. `--region` goes after the subcommand; without
  it boto3's usual region resolution applies.

Assumes the aws partition (`com.amazonaws.<region>.*` service names, `arn:aws:` ARNs). Python 3.9+, stdlib +
boto3; the tests also import botocore. Test: python -B test_secure_sm_catalog.py

    python secure_sm_catalog.py check-endpoint --vpc-id vpc-... --service com.amazonaws.us-east-1.ecr.api \
        --subnet-id subnet-... --sg-id sg-... --private-dns on
    python secure_sm_catalog.py ecr-gaps --vpc-id vpc-... --region us-east-1
    python secure_sm_catalog.py policy s3 --bucket my-bucket --vpce-id vpce-... --exempt-principal-arn arn:...
    python secure_sm_catalog.py audit-policy bucket-policy.json --vpc-id vpc-...
"""

import argparse
import json
import re
import sys

GATEWAY_SERVICES = ("s3", "dynamodb")
_VPCE_ID = re.compile(r"^vpce-[0-9a-f]{8,17}$")
_PRINCIPAL_KEYS = ("aws:principalarn", "aws:principalaccount", "aws:principalorgid", "aws:userid",
                   "aws:principaltag/")


def service_name(region, short):
    """("us-east-1", "ecr.api") -> "com.amazonaws.us-east-1.ecr.api"."""
    return f"com.amazonaws.{region}.{short}"


def endpoint_request(vpc_id, service, subnet_ids=(), sg_ids=(), route_table_ids=(), name=None,
                     private_dns=True):
    """kwargs for ec2.create_vpc_endpoint. A service ending in .s3 or .dynamodb becomes a Gateway endpoint
    (route tables; private_dns is ignored); anything else an Interface endpoint (subnets, security groups,
    private DNS, which needs the VPC's DNS hostnames and DNS support turned on)."""
    # ponytail: S3 also offers an Interface endpoint; pass it through boto3 directly if you need that
    gateway = service.rsplit(".", 1)[-1] in GATEWAY_SERVICES
    req = {"VpcEndpointType": "Gateway" if gateway else "Interface", "VpcId": vpc_id, "ServiceName": service}
    if gateway:
        if not route_table_ids:
            raise ValueError("a gateway endpoint needs at least one route table")
        req["RouteTableIds"] = list(route_table_ids)
    else:
        if not subnet_ids:
            raise ValueError("an interface endpoint needs at least one subnet")
        if not sg_ids:
            raise ValueError("pass the endpoint's security group; the VPC default one usually doesn't allow 443 "
                             "from the clients")
        req.update(SubnetIds=list(subnet_ids), SecurityGroupIds=list(sg_ids), PrivateDnsEnabled=private_dns)
    if name:
        req["TagSpecifications"] = [{"ResourceType": "vpc-endpoint", "Tags": [{"Key": "Name", "Value": name}]}]
    return req


def check_endpoint(ep, service, vpc_id, subnet_ids=(), sg_ids=(), private_dns=None, route_table_ids=()):
    """One VpcEndpoints[] item from describe_vpc_endpoints -> list of problems (empty = as expected).
    Checks only what you pass; it doesn't read security-group rules."""
    problems = []
    if ep.get("ServiceName") != service:
        problems.append(f"service is {ep.get('ServiceName')}, expected {service}")
    if ep.get("VpcId") != vpc_id:
        problems.append(f"VPC is {ep.get('VpcId')}, expected {vpc_id}")
    if str(ep.get("State", "")).lower() != "available":
        problems.append(f"state is {ep.get('State')}, expected available")
    for label, want, have in (("subnet(s)", subnet_ids, ep.get("SubnetIds", [])),
                              ("security group(s)", sg_ids, [g.get("GroupId") for g in ep.get("Groups", [])]),
                              ("route table(s)", route_table_ids, ep.get("RouteTableIds", []))):
        missing = set(want) - set(have)
        if missing:
            problems.append(f"missing {label} {sorted(missing)}")
    if private_dns is not None and bool(ep.get("PrivateDnsEnabled")) != private_dns:
        problems.append(f"private DNS is {'on' if ep.get('PrivateDnsEnabled') else 'off'}, "
                        f"expected {'on' if private_dns else 'off'}")
    return problems


def missing_for_ecr_pull(endpoints, region):
    """describe_vpc_endpoints items for one VPC -> the endpoints still missing for `docker pull` from ECR in
    a subnet without internet access: ecr.api (API calls), ecr.dkr (registry, needs private DNS) and the S3
    gateway (image layers live in S3). Only available endpoints count."""
    # ponytail: presence and private DNS only; subnets, security-group rules and the gateway's route tables
    # need check_endpoint
    up = {(e.get("ServiceName"), e.get("VpcEndpointType")): e for e in endpoints
          if str(e.get("State", "")).lower() == "available"}
    gaps = []
    for short, kind in (("ecr.api", "Interface"), ("ecr.dkr", "Interface"), ("s3", "Gateway")):
        ep = up.get((service_name(region, short), kind))
        if ep is None:
            gaps.append(f"{service_name(region, short)} ({kind})")
        elif short == "ecr.dkr" and not ep.get("PrivateDnsEnabled"):
            gaps.append(f"{service_name(region, short)}: enable private DNS")
    return gaps


def _deny_unless_vpce(vpce_ids, exempt_principal_arns):
    ids = [vpce_ids] if isinstance(vpce_ids, str) else list(vpce_ids)
    bad = [v for v in ids if not _VPCE_ID.match(v)] or ([] if ids else ["(none)"])
    if bad:
        raise ValueError(f"not VPC endpoint ids: {bad}; a Deny with a wrong id blocks every request")
    cond = {"StringNotEquals": {"aws:SourceVpce": ids[0] if len(ids) == 1 else ids}}
    if exempt_principal_arns:
        cond["ArnNotLike"] = {"aws:PrincipalArn": list(exempt_principal_arns)}
    return {"Sid": "DenyOutsideVpce", "Effect": "Deny", "Principal": "*", "Condition": cond}


def vpce_only_bucket_policy(bucket, vpce_ids, exempt_principal_arns=()):
    """S3 bucket policy: deny every S3 action on the bucket unless the request came through one of vpce_ids.
    Without exempt_principal_arns (e.g. an admin or the CloudFormation role) this also blocks the console,
    PutBucketPolicy/DeleteBucketPolicy and stack updates or deletes from outside the VPC."""
    st = {**_deny_unless_vpce(vpce_ids, exempt_principal_arns), "Action": "s3:*",
          "Resource": [f"arn:aws:s3:::{bucket}", f"arn:aws:s3:::{bucket}/*"]}
    return {"Version": "2012-10-17", "Statement": [st]}


def vpce_only_ecr_policy(vpce_ids, exempt_principal_arns=()):
    """ECR repository policy: deny every ECR action on the repository unless the request came through one of
    vpce_ids. Docker pulls arrive via the ecr.dkr endpoint and API calls via ecr.api, so list both if both
    must work. A repository policy has no Resource element. The same lockout note as the bucket policy
    applies (SetRepositoryPolicy/DeleteRepositoryPolicy included)."""
    return {"Version": "2012-10-17", "Statement": [{**_deny_unless_vpce(vpce_ids, exempt_principal_arns),
                                                     "Action": "ecr:*"}]}


def unwrap_policy(doc):
    """A policy document, or `aws s3api get-bucket-policy` / `aws ecr get-repository-policy` output (policy as
    a JSON string under "Policy" / "policyText") -> the policy dict. ValueError if there's no Statement."""
    for key in ("Policy", "policyText"):
        if isinstance(doc, dict) and isinstance(doc.get(key), str):
            doc = json.loads(doc[key])
    if not isinstance(doc, dict) or "Statement" not in doc:
        raise ValueError("no Statement found: pass a policy document or get-bucket-policy / "
                         "get-repository-policy output")
    return doc


def _operator(op):
    """"ForAnyValue:StringNotEqualsIfExists" -> ("foranyvalue", "stringnotequals", True)."""
    qualifier, _, base = op.rpartition(":")
    base = base.lower()
    if_exists = base.endswith("ifexists")
    return qualifier.lower(), base[:-len("ifexists")] if if_exists else base, if_exists


def _audit_ids(sid, values, endpoint_ids):
    out = []
    for v in values:
        if not isinstance(v, str) or "*" in v or "?" in v:  # wildcards and non-strings: can't check an id
            continue
        if not _VPCE_ID.match(v):
            out.append(f"{sid}: {v!r} is not a VPC endpoint id (placeholder left in?)")
        elif endpoint_ids is not None and v not in endpoint_ids:
            out.append(f"{sid}: {v} is not an endpoint in this VPC, so the condition never matches a request "
                       f"from it")
    return out


def _audit_effect(sid, st, op, matches_absent, cond):
    """IAM: a condition on a key the request lacks is false, except for negated operators, ...IfExists and
    ForAllValues (true), and ForAnyValue is false even when negated. Requests that don't come through any
    VPC endpoint carry no aws:SourceVpce."""
    effect = st.get("Effect")
    pairs = [(_operator(o)[1], k.lower()) for o, block in cond.items() for k in block]
    # an exemption is a principal key under a negated operator (ArnNotLike aws:PrincipalArn ...) or NotPrincipal;
    # a principal key under a positive operator narrows the Deny to that principal instead
    exempt = "NotPrincipal" in st or any("not" in b and k.startswith(_PRINCIPAL_KEYS) for b, k in pairs)
    others = [k for b, k in pairs if k != "aws:sourcevpce" and not ("not" in b and k.startswith(_PRINCIPAL_KEYS))]
    if effect == "Allow":
        if matches_absent:
            return [f"{sid}: with {op} this Allow also matches requests that don't come through any endpoint, "
                    "so it grants access from everywhere"]
        star = st.get("Principal") in ("*", {"AWS": "*"})
        return [f"{sid}: an Allow with {op} only adds access through the endpoint; it doesn't block other paths "
                "(principals with IAM permissions still get in from anywhere). Use a Deny with StringNotEquals "
                "to restrict" + ("; Principal * also lets in any caller that uses the endpoint, other accounts "
                                 "included" if star else "")]
    if effect != "Deny":
        return []
    if not matches_absent:
        # a positive Deny ("block this endpoint") is a legitimate pattern; a negated one under ForAnyValue isn't
        return [f"{sid}: with {op} this Deny never applies to requests that don't come through an endpoint, so "
                "it doesn't restrict them"] if "not" in op.lower() else []
    out = []
    if others:
        out.append(f"{sid}: the other condition key(s) {others} are ANDed, so this Deny applies only when they "
                   "hold as well")
    if not exempt:
        out.append(f"{sid}: for the actions it covers, this Deny also applies to the console and every admin "
                   "outside the VPC; add an aws:PrincipalArn exemption if you need a way back in")
    return out


def _vpce_operators(policy):
    """Base operators ("stringnotequals", "null", ...) of every Condition key named aws:SourceVpce. Looks at
    condition keys only, so the text in a Sid or a value doesn't count."""
    stmts = unwrap_policy(policy)["Statement"]
    return [_operator(op)[1] for st in ([stmts] if isinstance(stmts, dict) else stmts)
            for op, block in (st.get("Condition") or {}).items() for key in block if key.lower() == "aws:sourcevpce"]


def audit_vpce_policy(policy, endpoint_ids=None):
    """Resource policy (dict, or get-*-policy output) -> warnings about its aws:SourceVpce conditions. With
    endpoint_ids (the ids of the VPC's endpoints) it also flags ids that don't exist there. ValueError if it
    can't find a Statement, so an unparsed input never reads as clean."""
    stmts = unwrap_policy(policy)["Statement"]
    warnings = []
    for i, st in enumerate([stmts] if isinstance(stmts, dict) else stmts):
        sid, cond = st.get("Sid", f"statement {i}"), st.get("Condition") or {}
        for op, block in cond.items():
            for key, val in block.items():
                if key.lower() != "aws:sourcevpce":
                    continue
                qualifier, base, if_exists = _operator(op)
                if base == "null":  # ponytail: Null tests presence, not an id; not judged
                    continue
                warnings += _audit_ids(sid, val if isinstance(val, list) else [val], endpoint_ids)
                negated = "not" in base
                matches_absent = if_exists or qualifier == "forallvalues" or (negated and qualifier != "foranyvalue")
                warnings += _audit_effect(sid, st, op, matches_absent, cond)
    return warnings


def publish_product(sc, portfolio_name, product_name, owner, template_url, launch_role_arn, principal_arns,
                    version="1"):
    """servicecatalog client -> {"portfolio_id", "product_id", "artifact_id"}. Creates the portfolio and a
    CloudFormation product from template_url, adds a LAUNCH constraint so the product launches with
    launch_role_arn (end users need no rights on the underlying services), and grants principal_arns."""
    # ponytail: not idempotent and no rollback; a failure after create_product leaves the portfolio and
    # product behind, and a rerun creates new ones
    port = sc.create_portfolio(DisplayName=portfolio_name, ProviderName=owner)["PortfolioDetail"]["Id"]
    prod = sc.create_product(Name=product_name, Owner=owner, ProductType="CLOUD_FORMATION_TEMPLATE",
                             ProvisioningArtifactParameters={"Name": version, "Type": "CLOUD_FORMATION_TEMPLATE",
                                                             "Info": {"LoadTemplateFromURL": template_url}})
    prod_id = prod["ProductViewDetail"]["ProductViewSummary"]["ProductId"]
    sc.associate_product_with_portfolio(ProductId=prod_id, PortfolioId=port)
    sc.create_constraint(PortfolioId=port, ProductId=prod_id, Type="LAUNCH",
                         Parameters=json.dumps({"RoleArn": launch_role_arn}))
    for arn in principal_arns:
        sc.associate_principal_with_portfolio(PortfolioId=port, PrincipalARN=arn, PrincipalType="IAM")
    return {"portfolio_id": port, "product_id": prod_id,
            "artifact_id": prod["ProvisioningArtifactDetail"]["Id"]}


def _endpoints(ec2, vpc_id, region):
    """-> (endpoints in vpc_id, the region they were read from)."""
    if ec2 is None:
        import boto3

        ec2 = boto3.client("ec2", region_name=region)
    region = region or getattr(getattr(ec2, "meta", None), "region_name", None)
    eps = ec2.describe_vpc_endpoints(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])["VpcEndpoints"]
    return eps, region


def _cmd_check(args, eps):
    dns = None if args.private_dns is None else args.private_dns == "on"
    candidates = [e for e in eps if e.get("ServiceName") == args.service]
    for ep in candidates:
        problems = check_endpoint(ep, args.service, args.vpc_id, args.subnet_id, args.sg_id, dns,
                                  args.route_table_id)
        print(f"{ep.get('VpcEndpointId')}: {'OK' if not problems else '; '.join(problems)}")
        if not problems:
            return 0
    if not candidates:
        print(f"no endpoint for {args.service} in {args.vpc_id}")
    return 1


def _parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--region", help="default: boto3's usual resolution (AWS_REGION, profile, ...)")
    p = argparse.ArgumentParser(description="Check VPC endpoints and their resource policies.")
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check-endpoint", parents=[common], help="is there an endpoint with these settings?")
    c.add_argument("--vpc-id", required=True)
    c.add_argument("--service", required=True, help="e.g. com.amazonaws.us-east-1.ecr.api")
    c.add_argument("--subnet-id", action="append", default=[])
    c.add_argument("--sg-id", action="append", default=[])
    c.add_argument("--route-table-id", action="append", default=[])
    c.add_argument("--private-dns", choices=("on", "off"))
    g = sub.add_parser("ecr-gaps", parents=[common], help="endpoints still missing for docker pull from ECR")
    g.add_argument("--vpc-id", required=True)
    pol = sub.add_parser("policy", help="print a deny-unless-these-endpoints resource policy")
    pol.add_argument("kind", choices=("s3", "ecr"))
    pol.add_argument("--vpce-id", action="append", required=True, help="repeat for several endpoints")
    pol.add_argument("--bucket", help="required for s3")
    pol.add_argument("--exempt-principal-arn", action="append", default=[])
    a = sub.add_parser("audit-policy", parents=[common], help="warn about aws:SourceVpce conditions in a file")
    a.add_argument("file", help="policy JSON, or get-bucket-policy / get-repository-policy output")
    a.add_argument("--vpc-id", help="also check the endpoint ids against this VPC's endpoints")
    return p


def _run(p, args, ec2):
    if args.cmd == "policy":
        if args.kind == "s3" and not args.bucket:
            p.error("policy s3 needs --bucket")
        doc = (vpce_only_bucket_policy(args.bucket, args.vpce_id, args.exempt_principal_arn) if args.kind == "s3"
               else vpce_only_ecr_policy(args.vpce_id, args.exempt_principal_arn))
        if not args.exempt_principal_arn:
            print("warning: no --exempt-principal-arn; this policy also locks out admins outside the VPC",
                  file=sys.stderr)
        print(json.dumps(doc, indent=2))
        return 0
    if args.cmd == "audit-policy":
        with open(args.file, encoding="utf-8-sig") as f:  # -sig: PowerShell redirection can add a BOM
            doc = json.load(f)
        ids = {e["VpcEndpointId"] for e in _endpoints(ec2, args.vpc_id, args.region)[0]} if args.vpc_id else None
        warnings = audit_vpce_policy(doc, ids)
        judged = [b for b in _vpce_operators(doc) if b != "null"]
        if not warnings and not judged:
            # nothing to audit is not the same as clean
            print("only Null checks on aws:SourceVpce, which this audit doesn't judge; review them by hand"
                  if _vpce_operators(doc) else
                  "no aws:SourceVpce condition in this policy, so it doesn't limit access to a VPC endpoint")
            return 1
        print("\n".join(warnings) if warnings else "no aws:SourceVpce problems found")
        return 1 if warnings else 0
    eps, region = _endpoints(ec2, args.vpc_id, args.region)
    if args.cmd == "ecr-gaps":
        if not region:
            raise ValueError("no region: pass --region")
        gaps = missing_for_ecr_pull(eps, region)
        print("\n".join(gaps) if gaps else f"ecr.api, ecr.dkr (private DNS) and the s3 gateway are all present "
                                           f"in {args.vpc_id}")
        return 1 if gaps else 0
    return _cmd_check(args, eps)


def main(argv=None, ec2=None):
    """check-endpoint: 0 when an endpoint matches, 1 when none does. ecr-gaps and audit-policy: 0 clean,
    1 findings (audit-policy also 1 when the policy has no aws:SourceVpce condition at all). policy: prints
    JSON, 0. 2 on bad arguments, bad input, unreadable files or AWS errors."""
    p = _parser()
    args = p.parse_args(argv)
    try:
        return _run(p, args, ec2)
    except Exception as e:  # OSError, ValueError (bad JSON / no Statement / bad id), NoCredentialsError, ...
        print(f"{args.cmd} failed: {type(e).__name__}: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
