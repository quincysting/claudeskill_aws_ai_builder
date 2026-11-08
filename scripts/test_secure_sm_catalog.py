"""Self-tests for secure_sm_catalog.py: fakes for the AWS clients, plus botocore's own request models to
check that the request shapes are valid (no network or credentials needed). Run: python -B test_secure_sm_catalog.py"""

import contextlib
import io
import json
import os
import tempfile
import types
import unittest

import botocore.session
from botocore.validate import ParamValidator

import secure_sm_catalog as m

REGION = "eu-west-1"  # deliberately not the boto3/CLI-ish default, so region plumbing is exercised
VPC = "vpc-0aaa"
API = m.service_name(REGION, "ecr.api")
DKR = m.service_name(REGION, "ecr.dkr")
S3 = m.service_name(REGION, "s3")
SNAP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")  # the lab 19 policies


def ep(service, state="available", subnets=("subnet-1",), sgs=("sg-1",), dns=True, eid="vpce-1"):
    return {"VpcEndpointId": eid, "ServiceName": service, "VpcEndpointType": "Interface", "VpcId": VPC,
            "State": state, "SubnetIds": list(subnets), "Groups": [{"GroupId": g} for g in sgs],
            "PrivateDnsEnabled": dns}


def gw(service=S3, rtbs=("rtb-1",), state="available", eid="vpce-gw"):
    """A gateway endpoint as describe_vpc_endpoints shows it: route tables, no subnets or groups."""
    return {"VpcEndpointId": eid, "ServiceName": service, "VpcEndpointType": "Gateway", "VpcId": VPC,
            "State": state, "RouteTableIds": list(rtbs), "SubnetIds": [], "Groups": [], "PrivateDnsEnabled": False}


def load(name):
    with open(os.path.join(SNAP, name), encoding="utf-8") as f:
        return json.load(f)


def stmt(effect, op, val="vpce-0abc1234", extra=None, principal="*"):
    cond = {op: {"aws:SourceVpce": val}, **(extra or {})}
    return {"Statement": [{"Sid": "S", "Effect": effect, "Principal": principal, "Condition": cond}]}


class EndpointRequest(unittest.TestCase):
    def test_s3_is_a_gateway_with_route_tables(self):
        r = m.endpoint_request(VPC, S3, route_table_ids=["rtb-1"], name="s3-endpoint")
        self.assertEqual((r["VpcEndpointType"], r["RouteTableIds"]), ("Gateway", ["rtb-1"]))
        self.assertNotIn("SubnetIds", r)
        self.assertNotIn("PrivateDnsEnabled", r)
        self.assertEqual(r["TagSpecifications"][0]["Tags"], [{"Key": "Name", "Value": "s3-endpoint"}])

    def test_ecr_is_an_interface_with_private_dns(self):
        r = m.endpoint_request(VPC, API, subnet_ids=["subnet-1"], sg_ids=["sg-1"])
        self.assertEqual(r["VpcEndpointType"], "Interface")
        self.assertEqual((r["SubnetIds"], r["SecurityGroupIds"], r["PrivateDnsEnabled"]), (["subnet-1"], ["sg-1"], True))
        self.assertNotIn("TagSpecifications", r)

    def test_missing_placement_is_an_error(self):
        for kw in ({"service": S3}, {"service": API}, {"service": API, "subnet_ids": ["subnet-1"]}):
            with self.assertRaises(ValueError, msg=kw):
                m.endpoint_request(VPC, **kw)


class CheckEndpoint(unittest.TestCase):
    def test_matching_endpoint_has_no_problems(self):
        self.assertEqual(m.check_endpoint(ep(API), API, VPC, ["subnet-1"], ["sg-1"], private_dns=True), [])
        self.assertEqual(m.check_endpoint(gw(), S3, VPC, route_table_ids=["rtb-1"]), [])

    def test_each_mismatch_is_reported(self):
        bad = ep(DKR, state="pending", subnets=("subnet-2",), sgs=("sg-2",), dns=False)
        bad["VpcId"] = "vpc-other"
        problems = m.check_endpoint(bad, API, VPC, ["subnet-1"], ["sg-1"], private_dns=True, route_table_ids=["rtb-1"])
        self.assertEqual(len(problems), 7, problems)
        for word in ("service", "VPC", "state", "subnet", "security group", "route table", "private DNS"):
            self.assertTrue(any(word in p for p in problems), word)

    def test_state_is_case_insensitive_and_extra_subnets_are_fine(self):
        self.assertEqual(m.check_endpoint(ep(API, state="Available", subnets=("subnet-1", "subnet-9")), API, VPC,
                                          ["subnet-1"]), [])


class EcrPull(unittest.TestCase):
    def test_api_only_still_misses_dkr_and_s3(self):
        self.assertEqual(m.missing_for_ecr_pull([ep(API)], REGION), [f"{DKR} (Interface)", f"{S3} (Gateway)"])

    def test_complete_set(self):
        self.assertEqual(m.missing_for_ecr_pull([ep(API), ep(DKR), gw()], REGION), [])

    def test_dkr_without_private_dns_and_unavailable_endpoints(self):
        gaps = m.missing_for_ecr_pull([ep(API, state="pending"), ep(DKR, dns=False), gw()], REGION)
        self.assertEqual(gaps, [f"{API} (Interface)", f"{DKR}: enable private DNS"])

    def test_interface_s3_does_not_count_as_the_gateway(self):
        self.assertIn(f"{S3} (Gateway)", m.missing_for_ecr_pull([ep(API), ep(DKR), ep(S3)], REGION))

    def test_service_names_follow_the_region(self):
        self.assertEqual(len(m.missing_for_ecr_pull([ep(API), ep(DKR), gw()], "us-east-1")), 3)


class Policies(unittest.TestCase):
    def test_bucket_policy_denies_outside_the_endpoint_on_bucket_and_objects(self):
        st = m.vpce_only_bucket_policy("b", "vpce-0abc1234")["Statement"][0]
        self.assertEqual((st["Effect"], st["Action"]), ("Deny", "s3:*"))
        self.assertEqual(st["Resource"], ["arn:aws:s3:::b", "arn:aws:s3:::b/*"])
        self.assertEqual(st["Condition"], {"StringNotEquals": {"aws:SourceVpce": "vpce-0abc1234"}})

    def test_exempt_principals_and_several_endpoints(self):
        st = m.vpce_only_ecr_policy(["vpce-0abc1234", "vpce-0def5678"], ["arn:aws:iam::111122223333:role/Admin"])["Statement"][0]
        self.assertEqual(st["Condition"]["StringNotEquals"], {"aws:SourceVpce": ["vpce-0abc1234", "vpce-0def5678"]})
        self.assertEqual(st["Condition"]["ArnNotLike"], {"aws:PrincipalArn": ["arn:aws:iam::111122223333:role/Admin"]})
        self.assertNotIn("Resource", st)
        self.assertEqual(st["Action"], "ecr:*")

    def test_bad_endpoint_ids_are_refused(self):
        for bad in ("<PASTE S3 VPC ENDPOINT ID>", "vpce-XYZ", [], ["vpce-0abc1234", "nope"]):
            with self.assertRaises(ValueError, msg=bad):
                m.vpce_only_bucket_policy("b", bad)


class Audit(unittest.TestCase):
    def test_lab_bucket_policy_allow_with_placeholder(self):
        w = m.audit_vpce_policy(load("s3_bucket_policy_template.json"))
        self.assertEqual(len(w), 2, w)
        self.assertIn("placeholder", w[0])
        self.assertIn("doesn't block other paths", w[1])
        self.assertIn("Principal *", w[1])

    def test_lab_bucket_policy_after_the_fix_is_still_allow_only(self):
        doc = load("s3_bucket_policy_template.json")
        doc["Statement"][0]["Condition"]["StringEquals"]["aws:sourceVpce"] = "vpce-0850296d"
        w = m.audit_vpce_policy(doc, {"vpce-0850296d"})
        self.assertEqual(len(w), 1, w)
        self.assertIn("Allow", w[0])

    def test_lab_ecr_policy_names_an_endpoint_that_is_not_in_the_vpc(self):
        doc = load("ecr_repository_policy.json")
        doc["Statement"][0]["Condition"]["StringNotEquals"]["aws:sourceVpce"] = "vpce-08404ca9"
        w = m.audit_vpce_policy(doc, {"vpce-0919efad", "vpce-0850296d"})
        self.assertEqual(len(w), 2, w)
        self.assertIn("not an endpoint in this VPC", w[0])
        self.assertIn("way back in", w[1])

    def test_generated_policies_are_clean_when_exempted(self):
        admin = ["arn:aws:iam::111122223333:role/Admin"]
        self.assertEqual(m.audit_vpce_policy(m.vpce_only_bucket_policy("b", "vpce-0abc1234", admin), {"vpce-0abc1234"}), [])
        self.assertEqual(m.audit_vpce_policy(m.vpce_only_ecr_policy("vpce-0abc1234", admin), {"vpce-0abc1234"}), [])
        self.assertEqual(len(m.audit_vpce_policy(m.vpce_only_ecr_policy("vpce-0abc1234"))), 1)

    def test_operators_that_match_or_miss_requests_without_the_key(self):
        cases = {("Allow", "ForAllValues:StringEquals"): "from everywhere",
                 ("Allow", "StringEqualsIfExists"): "from everywhere",
                 ("Allow", "StringNotEquals"): "from everywhere",
                 ("Deny", "ForAnyValue:StringNotEquals"): "never applies"}
        for (effect, op), phrase in cases.items():
            w = m.audit_vpce_policy(stmt(effect, op))
            self.assertEqual(len(w), 1, (op, w))
            self.assertIn(phrase, w[0], op)
        # these still fire for keyless requests, so they behave like the plain Deny
        for op in ("StringNotEqualsIfExists", "ForAllValues:StringNotEquals", "StringNotLike"):
            self.assertIn("way back in", m.audit_vpce_policy(stmt("Deny", op))[0], op)

    def test_extra_conditions_are_flagged_and_principal_keys_count_as_exemptions(self):
        w = m.audit_vpce_policy(stmt("Deny", "StringNotEquals", extra={"Bool": {"aws:SecureTransport": "false"}}))
        self.assertTrue(any("ANDed" in x for x in w), w)
        for key in ("aws:PrincipalArn", "aws:PrincipalAccount", "aws:PrincipalTag/team", "aws:userid"):
            w = m.audit_vpce_policy(stmt("Deny", "StringNotEquals", extra={"StringNotLike": {key: "x"}}))
            self.assertEqual(w, [], key)
        # a principal key under a positive operator narrows the Deny to that principal: not an exemption
        for op in ("StringEquals", "ArnLike"):
            w = m.audit_vpce_policy(stmt("Deny", "StringNotEquals", extra={op: {"aws:PrincipalArn": "x"}}))
            self.assertEqual(len(w), 2, (op, w))
            self.assertIn("ANDed", w[0])
            self.assertIn("way back in", w[1])
        doc = stmt("Deny", "StringNotEquals")
        doc["Statement"][0]["NotPrincipal"] = {"AWS": "arn:aws:iam::1:role/A"}
        self.assertEqual(m.audit_vpce_policy(doc), [])

    def test_a_deny_that_blocks_one_endpoint_is_not_flagged(self):
        self.assertEqual(m.audit_vpce_policy(stmt("Deny", "StringEquals")), [])

    def test_null_bools_wildcards_lists_and_dict_statement(self):
        self.assertEqual(m.audit_vpce_policy(stmt("Deny", "Null", True)), [])
        self.assertEqual(m.audit_vpce_policy(stmt("Deny", "Null", "true")), [])
        w = m.audit_vpce_policy(stmt("Deny", "StringNotLike", "vpce-*"), {"vpce-0abc1234"})
        self.assertFalse(any("placeholder" in x or "not an endpoint" in x for x in w), w)
        doc = {"Statement": {"Sid": "S", "Effect": "Deny", "Condition": {"StringNotEquals": {
            "aws:SourceVpce": ["vpce-0abc1234", "vpce-0def5678"]}, "ArnNotLike": {"aws:PrincipalArn": "x"}}}}
        self.assertEqual(m.audit_vpce_policy(doc, {"vpce-0abc1234", "vpce-0def5678"}), [])
        self.assertEqual(len(m.audit_vpce_policy(doc, {"vpce-0abc1234"})), 1)
        self.assertEqual(m.audit_vpce_policy({"Statement": [{"Effect": "Allow", "Action": "s3:*"}]}), [])

    def test_cli_output_formats_are_unwrapped_and_unparsed_input_is_an_error(self):
        inner = json.dumps(stmt("Allow", "StringEquals"))
        for wrapped in ({"Policy": inner}, {"registryId": "1", "repositoryName": "r", "policyText": inner}):
            self.assertEqual(len(m.audit_vpce_policy(wrapped)), 1, wrapped)
        for bad in ({}, {"Policy": {"Statement": []}}, [], {"Version": "2012-10-17"}):
            with self.assertRaises(ValueError, msg=bad):
                m.audit_vpce_policy(bad)


class LabDiy(unittest.TestCase):
    def test_the_diy_endpoint_passes_the_hint_check_but_pull_still_needs_dkr(self):
        diy = ep(API, subnets=("subnet-eni",), sgs=("sg-eni",), eid="vpce-0919efad")
        self.assertEqual(m.check_endpoint(diy, API, VPC, ["subnet-eni"], ["sg-eni"], private_dns=True), [])
        self.assertEqual(m.missing_for_ecr_pull([diy, gw(rtbs=("rtb-private",))], REGION), [f"{DKR} (Interface)"])


class FakeSC:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def call(**kw):
            self.calls.append((name, kw))
            return {"create_portfolio": {"PortfolioDetail": {"Id": "port-1"}},
                    "create_product": {"ProductViewDetail": {"ProductViewSummary": {"ProductId": "prod-1"}},
                                       "ProvisioningArtifactDetail": {"Id": "pa-1"}}}.get(name, {})
        return call


class Publish(unittest.TestCase):
    def test_calls_ids_and_arguments(self):
        sc = FakeSC()
        out = m.publish_product(sc, "P", "Prod", "CQ", "https://b.s3.amazonaws.com/t.yml", "arn:role/Launch",
                                ["arn:role/User1", "arn:role/User2"])
        self.assertEqual(out, {"portfolio_id": "port-1", "product_id": "prod-1", "artifact_id": "pa-1"})
        self.assertEqual(sc.calls, [
            ("create_portfolio", {"DisplayName": "P", "ProviderName": "CQ"}),
            ("create_product", {"Name": "Prod", "Owner": "CQ", "ProductType": "CLOUD_FORMATION_TEMPLATE",
                                "ProvisioningArtifactParameters": {
                                    "Name": "1", "Type": "CLOUD_FORMATION_TEMPLATE",
                                    "Info": {"LoadTemplateFromURL": "https://b.s3.amazonaws.com/t.yml"}}}),
            ("associate_product_with_portfolio", {"ProductId": "prod-1", "PortfolioId": "port-1"}),
            ("create_constraint", {"PortfolioId": "port-1", "ProductId": "prod-1", "Type": "LAUNCH",
                                   "Parameters": json.dumps({"RoleArn": "arn:role/Launch"})}),
            ("associate_principal_with_portfolio", {"PortfolioId": "port-1", "PrincipalARN": "arn:role/User1",
                                                    "PrincipalType": "IAM"}),
            ("associate_principal_with_portfolio", {"PortfolioId": "port-1", "PrincipalARN": "arn:role/User2",
                                                    "PrincipalType": "IAM"})])


class RequestShapes(unittest.TestCase):
    """The kwargs we build are valid input for the real botocore operations."""

    session = botocore.session.get_session()

    def valid(self, service, op, kwargs):
        shape = self.session.get_service_model(service).operation_model(op).input_shape
        report = ParamValidator().validate(kwargs, shape)
        self.assertFalse(report.has_errors(), report.generate_report())

    def test_create_vpc_endpoint(self):
        self.valid("ec2", "CreateVpcEndpoint", m.endpoint_request(VPC, S3, route_table_ids=["rtb-1"], name="n"))
        self.valid("ec2", "CreateVpcEndpoint", m.endpoint_request(VPC, API, ["subnet-1"], ["sg-1"]))

    def test_service_catalog_calls(self):
        sc = FakeSC()
        m.publish_product(sc, "P", "Prod", "CQ", "https://b/t.yml", "arn:aws:iam::111122223333:role/L",
                          ["arn:aws:iam::111122223333:role/U"])
        ops = {"create_portfolio": "CreatePortfolio", "create_product": "CreateProduct",
               "associate_product_with_portfolio": "AssociateProductWithPortfolio",
               "create_constraint": "CreateConstraint",
               "associate_principal_with_portfolio": "AssociatePrincipalWithPortfolio"}
        for name, kw in sc.calls:
            # IdempotencyToken is filled in by botocore when it's missing
            self.valid("servicecatalog", ops[name], {**kw, **({"IdempotencyToken": "t"} if name.startswith("create_") else {})})


class FakeEC2:
    def __init__(self, eps=None, error=None, region=REGION):
        self.eps, self.error, self.filters = eps or [], error, None
        self.meta = types.SimpleNamespace(region_name=region)

    def describe_vpc_endpoints(self, Filters):
        if self.error:
            raise self.error
        self.filters = Filters
        vpcs = next(f["Values"] for f in Filters if f["Name"] == "vpc-id")
        return {"VpcEndpoints": [e for e in self.eps if e["VpcId"] in vpcs]}


def run(argv, ec2=None):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = m.main(argv, ec2=ec2)
        except SystemExit as e:  # argparse errors
            code = e.code
    return code, out.getvalue(), err.getvalue()


class Cli(unittest.TestCase):
    ARGS = ["check-endpoint", "--vpc-id", VPC, "--service", API, "--subnet-id", "subnet-1", "--sg-id", "sg-1"]

    def test_check_endpoint_ok_mismatch_none_and_the_vpc_filter(self):
        ec2 = FakeEC2([ep(API)])
        self.assertEqual(run(self.ARGS, ec2)[0], 0)
        self.assertEqual(ec2.filters, [{"Name": "vpc-id", "Values": [VPC]}])
        code, out, _ = run(self.ARGS, FakeEC2([ep(API, sgs=("sg-9",))]))
        self.assertEqual((code, "security group" in out), (1, True))
        other = ep(API)
        other["VpcId"] = "vpc-other"
        code, out, _ = run(self.ARGS, FakeEC2([other]))
        self.assertEqual((code, "no endpoint" in out), (1, True))

    def test_check_endpoint_private_dns_and_route_tables(self):
        self.assertEqual(run(self.ARGS + ["--private-dns", "on"], FakeEC2([ep(API, dns=False)]))[0], 1)
        self.assertEqual(run(self.ARGS + ["--private-dns", "off"], FakeEC2([ep(API, dns=False)]))[0], 0)
        gw_args = ["check-endpoint", "--vpc-id", VPC, "--service", S3, "--route-table-id", "rtb-1"]
        self.assertEqual(run(gw_args, FakeEC2([gw()]))[0], 0)
        self.assertEqual(run(gw_args, FakeEC2([gw(rtbs=("rtb-2",))]))[0], 1)

    def test_ecr_gaps_uses_the_clients_region_or_the_flag_after_the_subcommand(self):
        complete = [ep(API), ep(DKR), gw()]
        code, out, _ = run(["ecr-gaps", "--vpc-id", VPC], FakeEC2(complete))
        self.assertEqual((code, "all present" in out), (0, True))
        self.assertEqual(run(["ecr-gaps", "--vpc-id", VPC, "--region", "us-east-1"], FakeEC2(complete))[0], 1)
        code, out, _ = run(["ecr-gaps", "--vpc-id", VPC], FakeEC2([ep(API)]))
        self.assertEqual((code, DKR in out), (1, True))
        self.assertEqual(run(["ecr-gaps", "--vpc-id", VPC], FakeEC2(complete, region=None))[0], 2)

    def test_aws_error_is_exit_2(self):
        code, _, err = run(["ecr-gaps", "--vpc-id", VPC], FakeEC2(error=RuntimeError("no creds")))
        self.assertEqual((code, "no creds" in err), (2, True))

    def test_audit_policy_file_with_and_without_vpc(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "p.json")
            with open(path, "w", encoding="utf-8-sig") as f:  # with a BOM, like PowerShell output
                json.dump(m.vpce_only_bucket_policy("b", "vpce-0abc1234", ["arn:aws:iam::1:role/A"]), f)
            self.assertEqual(run(["audit-policy", path])[0], 0)
            open_path = os.path.join(d, "open.json")
            allow = {"Effect": "Allow", "Principal": "*", "Action": "s3:GetObject"}
            cases = [({"Statement": [allow]}, "doesn't limit access"),                        # no condition at all
                     ({"Statement": [{**allow, "Sid": "aws:SourceVpce todo"}]}, "doesn't limit access"),  # text only
                     ({"Statement": [{**allow, "Condition": {"Null": {"aws:SourceVpce": "false"}}}]}, "by hand")]
            for doc, phrase in cases:
                with open(open_path, "w", encoding="utf-8") as f:
                    json.dump(doc, f)
                code, out, _ = run(["audit-policy", open_path])
                self.assertEqual((code, phrase in out), (1, True), doc)
            code, out, _ = run(["audit-policy", path, "--vpc-id", VPC], FakeEC2([ep(API, eid="vpce-0other00")]))
            self.assertEqual((code, "not an endpoint in this VPC" in out), (1, True))
            for text in ("{not json", '{"Policy": "{}"}'):
                with open(path, "w") as f:
                    f.write(text)
                self.assertEqual(run(["audit-policy", path])[0], 2, text)
        self.assertEqual(run(["audit-policy", os.path.join(SNAP, "no-such-file.json")])[0], 2)

    def test_policy_command(self):
        code, out, err = run(["policy", "ecr", "--vpce-id", "vpce-0abc1234", "--vpce-id", "vpce-0def5678"])
        self.assertEqual((code, json.loads(out)["Statement"][0]["Condition"]["StringNotEquals"]["aws:SourceVpce"]),
                         (0, ["vpce-0abc1234", "vpce-0def5678"]))
        self.assertIn("locks out", err)
        code, _, err = run(["policy", "s3", "--bucket", "b", "--vpce-id", "vpce-0abc1234",
                            "--exempt-principal-arn", "arn:aws:iam::1:role/A"])
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(run(["policy", "s3", "--vpce-id", "vpce-0abc1234"])[0], 2)
        self.assertEqual(run(["policy", "s3", "--bucket", "b", "--vpce-id", "<PASTE>"])[0], 2)


if __name__ == "__main__":
    unittest.main()
