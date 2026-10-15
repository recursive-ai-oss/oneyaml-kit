"""The AWS layer: every call the cloud code makes, in one class that tests replace with a fake.

It drives the AWS command line tool (JSON output), so the kit needs no extra Python packages.
Credentials are read by the tool from its usual places; this module never prints them and
never passes them to an instance. The one place they are used directly is `Presigner`, which
signs URLs in memory.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import os
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from . import sigv4

RUN_TAG = "oneyaml-kit-run"
IMAGE_TAG = "oneyaml-kit-image"
# Epoch seconds after which the resource has no business existing. A cleanup that was not told which run to
# clean leaves everything before this time alone, so it cannot terminate a run that is still in progress.
DEADLINE_TAG = "oneyaml-kit-deadline"
CLOCK_ALLOWANCE_SECONDS = 120
NAME_TAG = "Name"


class AwsError(Exception):
    pass


class LaunchError(AwsError):
    """An instance could not be started. `code` is the service's error code."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code


@dataclass
class Instance:
    id: str
    state: str                      # pending, running, shutting-down, terminated, stopping, stopped
    type: str = ""
    lifecycle: str = "on-demand"    # or spot
    az: str = ""
    launched: float = 0.0           # epoch seconds
    ended: float | None = None
    reason: str = ""                # the state transition reason code, e.g. a spot interruption
    tags: dict[str, str] = field(default_factory=dict)
    image: str = ""

    @property
    def deadline(self) -> float | None:
        """When the instance's own lifetime limit has passed, from its tag (None when it carries none)."""
        return parse_deadline(self.tags.get(DEADLINE_TAG))


def parse_deadline(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


@dataclass
class Launch:
    image: str
    type: str
    security_group: str
    user_data: bytes
    run_id: str
    name: str
    spot: bool
    volume_gb: int
    stop_on_shutdown: bool = False   # the image bake needs a stopped instance, not a terminated one
    deadline: float = 0.0            # epoch seconds; tagged on everything the launch creates
    zone: str = ""                   # a fixed availability zone, or any


class Presigner:
    """Signs URLs with short-lived credentials, so they stop working when the run's deadline passes."""

    def __init__(self, host: str, region: str, creds: dict, expires: int, mode: str = "session token",
                 credentials_expire: float | None = None):
        self.host, self.region, self.expires = host, region, expires
        self.mode, self.credentials_expire = mode, credentials_expire
        self._access, self._secret, self._token = creds["AccessKeyId"], creds["SecretAccessKey"], creds.get("SessionToken")

    def url(self, method: str, key: str, expires: int | None = None) -> str:
        return sigv4.presign(method, self.host, key, self.region, self._access, self._secret, self._token,
                             expires or self.expires)


def parse_time(value) -> float | None:
    """Epoch seconds of an ISO timestamp as the service prints it, or None."""
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def http_get(url: str, timeout: float = 20) -> bytes | None:
    """The object at a presigned URL, or None when it does not exist (yet)."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        if e.code in (403, 404):
            return None
        raise AwsError(f"download failed: HTTP {e.code}") from e
    except (urllib.error.URLError, TimeoutError, OSError):
        return None


class Aws:
    def __init__(self, cfg, runner=None, tmpdir=None):
        self.cfg = cfg
        self.region = cfg.region
        self.bucket = cfg.bucket
        self._runner = runner or self._exec
        self.tmpdir = tmpdir      # where private request files go: the run's own directory, never a shared one

    # -- plumbing ---------------------------------------------------------------------------

    @staticmethod
    def _exec(args: list[str], stdin: str | None = None) -> str:
        r = subprocess.run(args, capture_output=True, text=True, input=stdin)
        if r.returncode != 0:
            raise AwsError((r.stderr.strip() or r.stdout.strip() or f"exit status {r.returncode}")[-800:])
        return r.stdout

    def cli(self, *args: str, parse: bool = True, stdin: str | None = None, region: bool = True):
        cmd = ["aws"] + (["--region", self.region] if region else []) + ["--output", "json", *args]
        out = self._runner(cmd, stdin)
        return json.loads(out) if parse and out.strip() else out

    def cli_json_file(self, service: str, command: str, request: dict):
        """Runs a command whose request goes in through a private file. The request of a launch holds presigned
        URLs, which are capabilities, and a command line is readable by every user of the machine."""
        fd, name = tempfile.mkstemp(prefix="request-", suffix=".json", dir=self.tmpdir)    # created 0600
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(request, f)
            return self.cli(service, command, "--cli-input-json", "file://" + name)
        finally:
            try:
                os.unlink(name)
            except OSError:
                pass

    # -- credentials and signing ------------------------------------------------------------

    def presigner(self, seconds: int, now=time.time) -> Presigner:
        """URLs valid for `seconds`, signed with temporary credentials of the same lifetime.

        A URL stops working when the credentials that signed it expire, so credentials that run out before the
        run's deadline would break the run half way: that is refused up front. The signing mode is kept on the
        result for the run record."""
        seconds = max(900, int(seconds))
        asked_at = now()
        try:
            creds = self.cli("sts", "get-session-token", "--duration-seconds", str(seconds))["Credentials"]
            mode, expires = "session token", parse_time(creds.get("Expiration"))
        except AwsError:
            # Some credential types cannot ask for a session token; their own are short-lived already.
            exp = self.cli("configure", "export-credentials", "--format", "process", region=False)
            creds = {"AccessKeyId": exp["AccessKeyId"], "SecretAccessKey": exp["SecretAccessKey"],
                     "SessionToken": exp.get("SessionToken")}
            expires = parse_time(exp.get("Expiration"))
            mode = "exported credentials" if expires else "exported credentials without a stated expiry"
        # A short allowance for the time the call took and for clock differences with the service.
        if expires is not None and expires < asked_at + seconds - CLOCK_ALLOWANCE_SECONDS:
            raise AwsError("the signing credentials expire in %d minutes, before the run's deadline in %d minutes; "
                           "renew them and run again" % ((expires - asked_at) // 60, seconds // 60))
        return Presigner(sigv4.bucket_host(self.bucket, self.region), self.region, creds, seconds, mode, expires)

    # -- S3 ---------------------------------------------------------------------------------

    def ensure_bucket(self) -> bool:
        """Creates the private bucket with its expiry rule when missing. True when it was created."""
        created = False
        try:
            self.cli("s3api", "head-bucket", "--bucket", self.bucket, parse=False)
        except AwsError:
            args = ["s3api", "create-bucket", "--bucket", self.bucket,
                    "--create-bucket-configuration", f"LocationConstraint={self.region}"]
            if self.region == "us-east-1":
                args = args[:-2]
            self.cli(*args, parse=False)
            created = True
        self.cli("s3api", "put-public-access-block", "--bucket", self.bucket, "--public-access-block-configuration",
                 "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true",
                 parse=False)
        rule = {"Rules": [{"ID": "expire-runs", "Status": "Enabled", "Filter": {"Prefix": "runs/"},
                           "Expiration": {"Days": 1}, "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 1}}]}
        self.cli("s3api", "put-bucket-lifecycle-configuration", "--bucket", self.bucket,
                 "--lifecycle-configuration", json.dumps(rule), parse=False)
        return created

    def upload(self, path: Path, key: str) -> None:
        self._runner(["aws", "--region", self.region, "s3", "cp", "--only-show-errors", str(path),
                      f"s3://{self.bucket}/{key}"], None)

    def delete_keys(self, keys: list[str]) -> int:
        for key in keys:
            self.cli("s3api", "delete-object", "--bucket", self.bucket, "--key", key, parse=False)
        return len(keys)

    def delete_prefix(self, prefix: str) -> int:
        return self.delete_keys(self.list_keys(prefix))

    def list_keys(self, prefix: str, older_than: float | None = None) -> list[str]:
        out, token = [], None
        while True:
            args = ["s3api", "list-objects-v2", "--bucket", self.bucket, "--prefix", prefix]
            if token:
                args += ["--continuation-token", token]
            page = self.cli(*args)
            for o in page.get("Contents", []):
                when = dt.datetime.fromisoformat(o["LastModified"].replace("Z", "+00:00")).timestamp()
                if older_than is None or when < older_than:
                    out.append(o["Key"])
            token = page.get("NextContinuationToken")
            if not token:
                return out

    # -- prices and sizes -------------------------------------------------------------------

    def spot_price(self, itype: str) -> float:
        """The highest current spot price over the zones a launch may land in (the configured zones, or all that
        offer the type). The estimate must hold wherever the instance starts, so it uses the dearest one."""
        r = self.cli("ec2", "describe-spot-price-history", "--instance-types", itype,
                     "--product-descriptions", "Linux/UNIX", "--start-time",
                     dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
        latest: dict[str, float] = {}
        for h in r.get("SpotPriceHistory", []):
            zone = h["AvailabilityZone"]
            if not getattr(self.cfg, "zones", ()) or zone in self.cfg.zones:
                latest.setdefault(zone, float(h["SpotPrice"]))
        if not latest:
            raise AwsError(f"no spot price for {itype} in {self.region}")
        return max(latest.values())

    def on_demand_price(self, itype: str) -> float | None:
        """The list price per hour, or None when the pricing service does not answer."""
        names = {"us-east-1": "US East (N. Virginia)", "us-east-2": "US East (Ohio)", "us-west-2": "US West (Oregon)"}
        loc = names.get(self.region)
        if not loc:
            return None
        flt = [("instanceType", itype), ("location", loc), ("operatingSystem", "Linux"), ("tenancy", "Shared"),
               ("preInstalledSw", "NA"), ("capacitystatus", "Used")]
        # One --filters option with every term: repeating the option keeps only the last one.
        args = ["pricing", "get-products", "--service-code", "AmazonEC2", "--max-items", "5", "--filters",
                *[f"Type=TERM_MATCH,Field={k},Value={v}" for k, v in flt]]
        try:
            r = subprocess.run(["aws", "--region", "us-east-1", "--output", "json", *args], capture_output=True, text=True)
            for item in json.loads(r.stdout).get("PriceList", []):
                doc = json.loads(item)
                for term in doc["terms"]["OnDemand"].values():
                    for dim in term["priceDimensions"].values():
                        return float(dim["pricePerUnit"]["USD"])
        except (OSError, ValueError, KeyError):
            pass
        return None

    def vcpus(self, itype: str) -> int:
        r = self.cli("ec2", "describe-instance-types", "--instance-types", itype)
        return int(r["InstanceTypes"][0]["VCpuInfo"]["DefaultVCpus"])

    # -- network ----------------------------------------------------------------------------

    def create_security_group(self, run_id: str, deadline: float = 0.0) -> str:
        """A group with no inbound rule in the default network. Outbound stays open for downloads."""
        vpc = self.cli("ec2", "describe-vpcs", "--filters", "Name=is-default,Values=true")["Vpcs"][0]["VpcId"]
        r = self.cli("ec2", "create-security-group", "--group-name", f"oneyaml-kit-{run_id}", "--vpc-id", vpc,
                     "--description", "benchmark run, no inbound",
                     "--tag-specifications", f"ResourceType=security-group,Tags=[{{Key={RUN_TAG},Value={run_id}}}"
                     + (f",{{Key={DEADLINE_TAG},Value={int(deadline)}}}" if deadline else "") + "]")
        return r["GroupId"]

    def delete_security_group(self, gid: str, attempts: int = 12) -> bool:
        for _ in range(attempts):
            try:
                self.cli("ec2", "delete-security-group", "--group-id", gid, parse=False)
                return True
            except AwsError as e:
                if "InvalidGroup.NotFound" in str(e):
                    return True
                time.sleep(5)    # still referenced by an interface that is being released
        return False

    # -- instances --------------------------------------------------------------------------

    def launch(self, spec: Launch) -> str:
        tags = [{"Key": RUN_TAG, "Value": spec.run_id}, {"Key": NAME_TAG, "Value": spec.name}]
        if spec.deadline:
            tags.append({"Key": DEADLINE_TAG, "Value": str(int(spec.deadline))})
        req = {
            "ImageId": spec.image, "InstanceType": spec.type, "MinCount": 1, "MaxCount": 1,
            "SecurityGroupIds": [spec.security_group],
            "UserData": base64.b64encode(spec.user_data).decode(),
            "InstanceInitiatedShutdownBehavior": "stop" if spec.stop_on_shutdown else "terminate",
            "MetadataOptions": {"HttpTokens": "required", "HttpEndpoint": "enabled", "HttpPutResponseHopLimit": 1,
                                "InstanceMetadataTags": "disabled"},
            "BlockDeviceMappings": [{"DeviceName": "/dev/sda1", "Ebs": {
                "VolumeSize": spec.volume_gb, "VolumeType": "gp3", "Iops": 16000, "Throughput": 1000,
                "DeleteOnTermination": True}}],
            "TagSpecifications": [{"ResourceType": t, "Tags": tags} for t in ("instance", "volume")],
        }
        if spec.zone:
            req["Placement"] = {"AvailabilityZone": spec.zone}
        if spec.spot:
            req["InstanceMarketOptions"] = {"MarketType": "spot", "SpotOptions": {
                "SpotInstanceType": "one-time", "InstanceInterruptionBehavior": "terminate"}}
            req["TagSpecifications"].append({"ResourceType": "spot-instances-request", "Tags": tags})
        try:
            r = self.cli_json_file("ec2", "run-instances", req)
        except AwsError as e:
            m = str(e)
            code = next((c for c in ("InsufficientInstanceCapacity", "SpotMaxPriceTooLow", "MaxSpotInstanceCountExceeded",
                                     "VcpuLimitExceeded", "Unsupported", "InternalError") if c in m), "Error")
            raise LaunchError(code, m) from e
        return r["Instances"][0]["InstanceId"]

    def instances(self, run_id: str | None = None) -> list[Instance]:
        flt = [f"Name=tag-key,Values={RUN_TAG}"] if run_id is None else [f"Name=tag:{RUN_TAG},Values={run_id}"]
        r = self.cli("ec2", "describe-instances", "--filters", *flt)
        out = []
        for res in r.get("Reservations", []):
            for i in res["Instances"]:
                tags = {t["Key"]: t["Value"] for t in i.get("Tags", [])}
                out.append(Instance(
                    id=i["InstanceId"], state=i["State"]["Name"], type=i.get("InstanceType", ""),
                    lifecycle=i.get("InstanceLifecycle", "on-demand"), az=i.get("Placement", {}).get("AvailabilityZone", ""),
                    launched=dt.datetime.fromisoformat(i["LaunchTime"].replace("Z", "+00:00")).timestamp(),
                    reason=(i.get("StateReason") or {}).get("Code", ""), tags=tags,
                    image=i.get("ImageId", "")))
        return out

    def expire(self, resource_ids: list[str]) -> None:
        """Sets the deadline of the given instances and groups to now."""
        if resource_ids:
            self.cli("ec2", "create-tags", "--resources", *resource_ids, "--tags",
                     f"Key={DEADLINE_TAG},Value={int(time.time())}", parse=False)

    def terminate(self, ids: list[str]) -> None:
        if ids:
            self.cli("ec2", "terminate-instances", "--instance-ids", *ids, parse=False)

    def spot_requests(self, run_id: str | None = None) -> list[str]:
        flt = [f"Name=tag-key,Values={RUN_TAG}"] if run_id is None else [f"Name=tag:{RUN_TAG},Values={run_id}"]
        r = self.cli("ec2", "describe-spot-instance-requests", "--filters", *flt,
                     "Name=state,Values=open,active,disabled")
        return [x["SpotInstanceRequestId"] for x in r.get("SpotInstanceRequests", [])]

    def cancel_spot_requests(self, run_id: str | None = None) -> int:
        ids = self.spot_requests(run_id)
        if ids:
            self.cli("ec2", "cancel-spot-instance-requests", "--spot-instance-request-ids", *ids, parse=False)
        return len(ids)

    def security_groups(self, run_id: str | None = None) -> list[str]:
        flt = [f"Name=tag-key,Values={RUN_TAG}"] if run_id is None else [f"Name=tag:{RUN_TAG},Values={run_id}"]
        return [g["GroupId"] for g in self.cli("ec2", "describe-security-groups", "--filters", *flt)["SecurityGroups"]]

    def launch_templates(self, run_id: str | None = None) -> list[str]:
        flt = [f"Name=tag-key,Values={RUN_TAG}"] if run_id is None else [f"Name=tag:{RUN_TAG},Values={run_id}"]
        r = self.cli("ec2", "describe-launch-templates", "--filters", *flt)
        return [t["LaunchTemplateId"] for t in r.get("LaunchTemplates", [])]

    def delete_launch_template(self, lid: str) -> None:
        self.cli("ec2", "delete-launch-template", "--launch-template-id", lid, parse=False)

    def volumes(self, run_id: str | None = None) -> list[str]:
        flt = [f"Name=tag-key,Values={RUN_TAG}"] if run_id is None else [f"Name=tag:{RUN_TAG},Values={run_id}"]
        r = self.cli("ec2", "describe-volumes", "--filters", *flt)
        return [v["VolumeId"] for v in r["Volumes"] if v["State"] == "available"]

    def delete_volume(self, vid: str) -> None:
        self.cli("ec2", "delete-volume", "--volume-id", vid, parse=False)

    # -- images -----------------------------------------------------------------------------

    def find_image(self, digest: str) -> str | None:
        r = self.cli("ec2", "describe-images", "--owners", "self", "--filters",
                     f"Name=tag:{IMAGE_TAG},Values={digest}", "Name=state,Values=available")
        imgs = sorted(r.get("Images", []), key=lambda i: i["CreationDate"])
        return imgs[-1]["ImageId"] if imgs else None

    def create_image(self, instance_id: str, digest: str, name: str) -> str:
        r = self.cli("ec2", "create-image", "--instance-id", instance_id, "--name", name, "--tag-specifications",
                     f"ResourceType=image,Tags=[{{Key={IMAGE_TAG},Value={digest}}}]",
                     f"ResourceType=snapshot,Tags=[{{Key={IMAGE_TAG},Value={digest}}}]")
        return r["ImageId"]

    def wait_image(self, image_id: str, timeout: float = 1800) -> None:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            r = self.cli("ec2", "describe-images", "--image-ids", image_id)["Images"]
            if r and r[0]["State"] == "available":
                return
            if r and r[0]["State"] in ("failed", "error"):
                raise AwsError(f"image {image_id} failed")
            time.sleep(10)
        raise AwsError(f"image {image_id} not available in time")

    def superseded_images(self, keep_digest: str, in_use: set[str] = frozenset()) -> list[dict]:
        """Machine images of this tool that are not the current one: another digest, or an older image of the
        current digest. Each entry carries the image id and its snapshots. Images that a live instance uses stay."""
        r = self.cli("ec2", "describe-images", "--owners", "self", "--filters", f"Name=tag-key,Values={IMAGE_TAG}")
        imgs = sorted(r.get("Images", []), key=lambda i: i["CreationDate"])

        def tag(i):
            return next((t["Value"] for t in i.get("Tags", []) if t["Key"] == IMAGE_TAG), "")
        current = [i for i in imgs if tag(i) == keep_digest and i["State"] == "available"]
        keep = current[-1]["ImageId"] if current else None
        out = []
        for i in imgs:
            if i["ImageId"] == keep or i["ImageId"] in in_use:
                continue
            snaps = [b["Ebs"]["SnapshotId"] for b in i.get("BlockDeviceMappings", []) if b.get("Ebs", {}).get("SnapshotId")]
            out.append({"image": i["ImageId"], "digest": tag(i), "snapshots": snaps})
        return out

    def current_image_ids(self, keep_digest: str) -> list[str]:
        found = self.find_image(keep_digest)
        return [found] if found else []

    def remove_image(self, image_id: str, snapshots: list[str]) -> None:
        self.cli("ec2", "deregister-image", "--image-id", image_id, parse=False)
        for sid in snapshots:
            self.cli("ec2", "delete-snapshot", "--snapshot-id", sid, parse=False)

    # -- who owns what ----------------------------------------------------------------------

    def run_deadlines(self, unknown_age: float = 24 * 3600) -> dict[str, float]:
        """For every run that left something tagged: the latest deadline among its resources. A resource without a
        deadline tag counts as expired, except an instance from before the tag existed, which is trusted for
        `unknown_age` seconds after it was launched."""
        out: dict[str, float] = {}

        def note(tags, fallback=0.0):
            run = next((t["Value"] for t in tags if t["Key"] == RUN_TAG), None)
            if run is None:
                return
            d = parse_deadline(next((t["Value"] for t in tags if t["Key"] == DEADLINE_TAG), None))
            out[run] = max(out.get(run, 0.0), d if d is not None else fallback)
        for i in self.instances(None):
            if i.state != "terminated":
                note([{"Key": k, "Value": v} for k, v in i.tags.items()], i.launched + unknown_age)
        flt = [f"Name=tag-key,Values={RUN_TAG}"]
        for call, key in (("describe-security-groups", "SecurityGroups"), ("describe-volumes", "Volumes"),
                          ("describe-launch-templates", "LaunchTemplates")):
            for res in self.cli("ec2", call, "--filters", *flt).get(key, []):
                note(res.get("Tags", []))
        return out
