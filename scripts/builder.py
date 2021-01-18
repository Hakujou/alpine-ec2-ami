#!/usr/bin/env python3

# This bit has to stay at the very top of the script. It exists to ensure that
# running this script all by itself uses the python virtual environment with
# our dependencies installed. If will create that environment if it doesn't
# exist.
import os
import sys
import subprocess

args = [os.path.join("build", "bin", "python3")] + sys.argv

# Create the build root if it doesn't exist
if not os.path.exists("build"):
    import venv

    print("Build environment does not exist, creating...", file=sys.stderr)
    venv.create("build", with_pip=True)
    subprocess.run(["build/bin/pip", "install", "-U", "pip",
        "pyhocon", "boto3", "python-dateutil", "PyYAML"])

    print("Re-executing with builder python...", file=sys.stderr)
    os.execv(args[0], args)
else:
    # If the build root python is not running this script re-execute it with
    # that python instead to ensure all of our dependencies exist.
    if os.path.join(os.getcwd(), args[0]) != sys.executable:
        print("Re-executing with builder python...", file=sys.stderr)
        os.execv(args[0], args)

# Below here is the real script
import io
import os
import re
import sys
import glob
import json
import time
import shutil
import logging
import argparse
import textwrap
import subprocess
import urllib.error
import dateutil.parser

from enum import Enum
from collections import defaultdict
from datetime import datetime, timedelta
from distutils.version import StrictVersion
from urllib.request import Request, urlopen

import yaml
import boto3
import pyhocon


# allows us to set values deep within an object that might not be fully defined
def dictfactory():
    return defaultdict(dictfactory)

# undo dictfactory() objects to normal objects
def undictfactory(o):
    if isinstance(o, defaultdict):
        o = {k: undictfactory(v) for k, v in o.items()}
    return o

# This is an ugly hack. We occasionally need the region name but it's not
# attached to anything publicly exposed on the client objects. Hide this here.
def region_from_client(client):
    return client._client_config.region_name

# version sorting
def sortable_version(x):
    v = x.split('_rc')[0]
    return StrictVersion("0.0" if v == "edge" else v)


class EC2Architecture(Enum):

    I386 = "i386"
    X86_64 = "x86_64"
    ARM64 = "arm64"


class AMIState(Enum):

    PENDING = "pending"
    AVAILABLE = "available"
    INVALID = "invalid"
    DEREGISTERED = "deregistered"
    TRANSIENT = "transient"
    FAILED = "failed"
    ERROR = "error"


class EC2SnapshotState(Enum):

    PENDING = "pending"
    COMPLETED = "completed"
    ERROR = "error"


class TaggedAWSObject:
    """Base class for AWS API models that support tagging
    """

    EDGE = StrictVersion("0.0")

    missing_known_tags = None

    _identity = lambda x: x
    _known_tags = {
        "Name": _identity,
        "profile":  _identity,
        "revision":  _identity,
        "profile_build":  _identity,
        "source_ami":  _identity,
        "source_region":  _identity,
        "arch": lambda x: EC2Architecture(x),
        "end_of_life": lambda x: datetime.fromisoformat(x),
        "release": lambda v: EDGE if v == "edge" else StrictVersion(v),
        "version": lambda v: EDGE if v == "edge" else StrictVersion(v),
    }

    def __repr__(self):
        attrs = []
        for k, v in self.__dict__.items():
            if isinstance(v, TaggedAWSObject):
                attrs.append(f"{k}=" + object.__repr__(v))
            elif not k.startswith("_"):
                attrs.append(f"{k}={v!r}")
        attrs = ", ".join(attrs)

        return f"{self.__class__.__name__}({attrs})"

    __str__ = __repr__

    @property
    def aws_tags(self):
        """Convert python tags to AWS API tags

        See AMI.aws_permissions for rationale.
        """
        for key, values in self.tags.items():
            for value in values:
                yield { "Key": key, "Value": value }

    @aws_tags.setter
    def aws_tags(self, values):
        """Convert AWS API tags to python tags

        See AMI.aws_permissions for rationale.
        """
        if not getattr(self, "tags", None):
            self.tags = {}

        tags = defaultdict(list)

        for tag in values:
            tags[tag["Key"]].append(tag["Value"])

        self.tags.update(tags)
        self._transform_known_tags()

    # XXX(mcrute): The second paragraph might be considered a bug and worth
    # fixing at some point. For now those are all read-only attributes though.
    def _transform_known_tags(self):
        """Convert well known tags into python attributes

        Some tags have special meanings for the model objects that they're
        attached to. This copies those tags, transforms them, then sets them in
        the model attributes.

        It doesn't touch the tag itself so if that
        attribute needs updated and re-saved the tag must be updated in
        addition to the model.
        """
        self.missing_known_tags = []

        for k, tf in self._known_tags.items():
            v = self.tags.get(k, [])
            if not v:
                self.missing_known_tags.append(k)
                continue

            if len(v) > 1:
                raise Exception(f"multiple instances of tag {k}")

            setattr(self, k, v[0])


class AMI(TaggedAWSObject):

    @property
    def aws_permissions(self):
        """Convert python permissions to AWS API permissions

        The permissions model for the API makes more sense for a web service
        but is overly verbose for working with in Python. This and the setter
        allow transforming to/from the API syntax. The python code should
        consume the allowed_groups and allowed_users lists directly.
        """
        perms = []
        for g in self.allowed_groups:
            perms.append({"Group": g})

        for i in self.allowed_users:
            perms.append({"UserId": i})

        return perms

    @aws_permissions.setter
    def aws_permissions(self, perms):
        """Convert AWS API permissions to python permissions
        """
        for perm in perms:
            group = perm.get("Group")
            if group:
                self.allowed_groups.append(group)

            user = perm.get("UserId")
            if user:
                self.allowed_users.append(user)

    @classmethod
    def from_aws_model(cls, ob, region):
        self = cls()

        self.linked_snapshot = None
        self.allowed_groups = []
        self.allowed_users = []
        self.region = region
        self.architecture = EC2Architecture(ob["Architecture"])
        self.creation_date = ob["CreationDate"]
        self.description = ob.get("Description", None)
        self.image_id = ob["ImageId"]
        self.name = ob.get("Name")
        self.owner_id = int(ob["OwnerId"])
        self.public = ob["Public"]
        self.state = AMIState(ob["State"])
        self.virtualization_type = ob["VirtualizationType"]
        self.state_reason = ob.get("StateReason", {}).get("Message", None)
        self.aws_tags = ob.get("Tags", [])

        # XXX(mcrute): Assumes we only ever have one device mapping, which is
        # valid for Alpine AMIs but not a good general assumption.
        #
        # This should always resolve for AVAILABLE images but any part of the
        # data structure may not yet exist for images that are still in the
        # process of copying.
        if ob.get("BlockDeviceMappings"):
            self.snapshot_id = \
                    ob["BlockDeviceMappings"][0]["Ebs"].get("SnapshotId")

        return self


class EC2Snapshot(TaggedAWSObject):

    @classmethod
    def from_aws_model(cls, ob, region):
        self = cls()

        self.linked_ami = None
        self.region = region
        self.snapshot_id = ob["SnapshotId"]
        self.description = ob.get("Description", None)
        self.owner_id = int(ob["OwnerId"])
        self.progress = int(ob["Progress"].rstrip("%")) / 100
        self.start_time = ob["StartTime"]
        self.state = EC2SnapshotState(ob["State"])
        self.volume_size = ob["VolumeSize"]
        self.aws_tags = ob.get("Tags", [])

        return self


class ColoredFormatter(logging.Formatter):
    """Log formatter that colors output based on level
    """

    _colors = {
        "red": "31",
        "green": "32",
        "yellow": "33",
        "blue": "34",
        "magenta": "35",
        "cyan": "36",
        "white": "37",
    }

    def _color_wrap(self, text, color, bold=False):
        code = self._colors[color]
        if bold:
            code = "1;{}".format(code)
        return "\033[{}m{}\033[0m".format(code, text)

    def format(self, record):
        msg = super().format(record)
        # Levels: CRITICAL, ERROR, WARNING, INFO, DEBUG, NOTSET
        if record.levelno in {logging.ERROR, logging.CRITICAL}:
            return self._color_wrap(msg, "red")
        elif record.levelno == logging.WARNING:
            return self._color_wrap(msg, "yellow")
        else:
            return self._color_wrap(msg, "green")


class IdentityBrokerClient:
    """Client for identity broker

    Export IDENTITY_BROKER_ENDPOINT to override the default broker endpoint.
    Export IDENTITY_BROKER_API_KEY to specify an API key for the broker.

    See README_BROKER.md for more information and a spec.
    """

    _DEFAULT_ENDPOINT = "https://aws-access.crute.us/api/account"
    _DEFAULT_ACCOUNT = "alpine-amis-user"

    def __init__(self, endpoint=None, key=None, account=None):
        self.endpoint = endpoint or self._DEFAULT_ENDPOINT
        self.account = account or self._DEFAULT_ACCOUNT
        self.key = key
        self._logger = logging.getLogger()

        override_endpoint = os.environ.get("IDENTITY_BROKER_ENDPOINT")
        if override_endpoint:
            self.endpoint = override_endpoint

        if not self.key:
            self.key = os.environ.get("IDENTITY_BROKER_API_KEY")

        if not self.key:
            raise Exception("No identity broker key found")

    def _get(self, path):
        while True: # to handle rate limits
            try:
                res = urlopen(Request(path, headers={"X-API-Key": self.key}))
            except urllib.error.HTTPError as ex:
                if ex.headers.get("Location") == "/logout":
                    raise Exception("Identity broker token is expired")

                if ex.status == 429:
                    self._logger.warning(
                        "Rate-limited by identity broker, sleeping 30 seconds")
                    time.sleep(30)
                    continue

                raise ex

            if res.status not in {200, 429}:
                raise Exception(res.reason)

            return json.load(res)

    def get_credentials_url(self):
        for account in self._get(self.endpoint):
            if account["short_name"] == self.account:
                return account["credentials_url"]

        raise Exception("No account found")

    def get_regions(self):
        out = {}

        for region in self._get(self.get_credentials_url()):
            if region["enabled"]:
                out[region["name"]] = region["credentials_url"]

        return out

    def get_credentials(self, region):
        return self._get(self.get_regions()[region])

    def _boto3_session_from_creds(self, creds, region):
        return boto3.session.Session(
            aws_access_key_id=creds["access_key"],
            aws_secret_access_key=creds["secret_key"],
            aws_session_token=creds["session_token"],
            region_name=region)

    def boto3_session_for_region(self, region):
        return self._boto3_session_from_creds(
            self.get_credentials(region), region)

    def iter_regions(self):
        for region, cred_url in self.get_regions().items():
            yield self._boto3_session_from_creds(self._get(cred_url), region)


class ConfigBuilder:

    now = datetime.utcnow()
    tomorrow = now + timedelta(days=1)

    @staticmethod
    def unquote(x):
        return x.strip('"')

    @staticmethod
    def force_iso_date(input):
        return datetime.fromisoformat(input).isoformat(timespec="seconds")

    @classmethod
    def resolve_now(cls):
        return cls.now.strftime("%Y%m%d%H%M%S")

    @classmethod
    def resolve_revision(cls, input):
        if input is None or input == "":
            return cls.resolve_now()
        return input

    @classmethod
    def resolve_tomorrow(cls):
        return cls.tomorrow.isoformat(timespec="seconds")

    @classmethod
    def resolve_end_of_life(cls, input):
        if input is None or input == "":
            return cls.resolve_tomorrow()
        return input

    @classmethod
    def fold_comma(cls, input):
        return ",".join([cls.unquote(k) for k in input.keys()])

    @classmethod
    def fold_space(cls, input):
        return " ".join([cls.unquote(k) for k in input.keys()])

    @classmethod
    def fold_repos(cls, input):
        return "\n".join(
            f"@{v} {cls.unquote(k)}" if isinstance(v, str) else cls.unquote(k)
            for k, v in input.items())

    @staticmethod
    def fold_packages(input):
        return " ".join(
            f"{k}@{v}" if isinstance(v, str) else k
            for k, v in input.items())

    @staticmethod
    def fold_services(input):
        return " ".join(
            "{}={}".format(k, ",".join(v.keys()))
            for k, v in input.items())

    def __init__(self, config_path, out_dir):
        self.config_path = config_path
        self.out_dir = out_dir

        self._keys_to_transform = {
            "kernel_modules"  : self.fold_comma,
            "kernel_options"  : self.fold_space,
            "initfs_features" : self.fold_space,
            "repos"           : self.fold_repos,
            "pkgs"            : self.fold_packages,
            "svcs"            : self.fold_services,
            "revision"        : self.resolve_revision,
            "end_of_life"     : lambda x: \
                self.force_iso_date(self.resolve_end_of_life(x)),
        }

    def build_all(self):
        for file in glob.glob(os.path.join(self.config_path, "*.conf")):
            profile = os.path.splitext(os.path.split(file)[-1])[0]
            self.build_profile(profile)

    def rel_symlink(self, src_path, dest_dir, dest):
        os.symlink(
            os.path.relpath(src_path, dest_dir),
            os.path.join(dest_dir, dest))

    def build_profile(self, profile):
        build_config = pyhocon.ConfigFactory.parse_file(
            os.path.join(self.config_path, f"{profile}.conf"))

        for build, cfg in build_config["BUILDS"].items():
            build_dir = os.path.join(self.out_dir, profile, build)
            setup_dir = os.path.join(build_dir, "setup-ami.d")

            # Always start fresh
            shutil.rmtree(build_dir, ignore_errors=True)
            os.makedirs(setup_dir)

            # symlink everything in scripts/setup-ami.d
            for item in os.listdir("scripts/setup-ami.d"):
                self.rel_symlink(os.path.join("scripts/setup-ami.d", item), setup_dir, item)

            # symlink additional setup_script
            if "setup_script" in cfg.keys() and cfg["setup_script"] is not None:
                self.rel_symlink(cfg["setup_script"], setup_dir, "setup_script")
                del cfg["setup_script"]

            if "setup_copy" in cfg.keys() and cfg["setup_copy"] is not None:
                for dst, src in cfg["setup_copy"].items():
                    self.rel_symlink(src, setup_dir, dst)
                del cfg["setup_copy"]

            cfg["profile"] = profile
            cfg["profile_build"] = build

            # Order of operations is important here
            for k, v in cfg.items():
                transform = self._keys_to_transform.get(k)
                if transform:
                    cfg[k] = transform(v)

                if isinstance(v, str) and "{var." in v:
                    cfg[k] = v.format(var=cfg)

            with open(os.path.join(build_dir, "vars.json"), "w") as out:
                json.dump(cfg, out, indent=4, separators=(",", ": "))


class BuildAMIs:
    """Build all AMIs for profile, or specific builds within a profile
    """
    command_name = "omis"

    @staticmethod
    def add_args(parser):
        parser.add_argument("--region", "-r", default="us-west-2",
            help="region to use for build")
        parser.add_argument("profile", metavar="PROFILE",
            help="name of profile to build")
        parser.add_argument("builds", metavar="BUILD", nargs="*",
            help="name of build within a profile (multiple OK)")

    @staticmethod
    def get_artifact_id(root, profile, build):
        manifest_json = os.path.join(
            root, "build", "profile", profile, build,
            "manifest.json")
        with open(manifest_json, "r") as data:
            manifest = json.load(data)
        return manifest['builds'][0]['artifact_id'].split(':')[1]

    def make_amis(self, args, root, log):
        os.chdir(os.path.join(root, "build"))

        builds = args.builds or os.listdir(
            os.path.join("profile", args.profile))

        artifacts = []

        for build in builds:
            log.info("\n*** Building %s/%s ***\n\n", args.profile, build)

            build_dir = os.path.join("profile", args.profile, build)
            if not os.path.exists(build_dir):
                log.info("Build dir '%s' does not exist", build_dir)
                break

            env = None

            out = io.StringIO()

            res = subprocess.Popen([
                    os.environ.get("PACKER", "packer"),
                    "build",
                    f"-var-file={build_dir}/vars.json",
                    "packer.json"
                ], stdout=subprocess.PIPE, encoding="utf-8", env=env)

            while res.poll() is None:
                text = res.stdout.readline()
                out.write(text)
                print(text, end="") # input is already colorized

            if res.returncode == 0:
                artifacts.append(self.get_artifact_id(root, args.profile, build))
            else:
                if "is used by an existing AMI" in out.getvalue():
                    continue
                else:
                    sys.exit(res.returncode)

        return artifacts

    def run(self, args, root, log):
        log.info("Converting packer.conf to JSON...")
        source = os.path.join(root, "packer.conf")
        dest = os.path.join(root, "build", "packer.json")
        pyhocon.converter.HOCONConverter.convert_from_file(
            source, dest, "json", 2, False)

        log.info("Resolving profile...")
        builder = ConfigBuilder(
            os.path.join(root, "profiles"),
            os.path.join(root, "build", "profile"))
        builder.build_profile(args.profile)

        log.info("Running packer...")
        amis = self.make_amis(args, root, log)
        log.info("\n=== DONE ===\n\nNew: " + ' '.join(amis) + "\n")


# These are general-purpose methods

# iterate over EC2 region clients, whether we're using the broker or not
def iter_regions(use_broker, regions):
    if use_broker:
        for region in IdentityBrokerClient().iter_regions():
            yield region.client('ec2')
    else:
        for region in regions:
            yield boto3.session.Session(region_name=region).client('ec2')

def get_image(client, image_id):
    images = client.describe_images(ImageIds=[image_id], Owners=["self"])
    perms = client.describe_image_attribute(
        Attribute="launchPermission", ImageId=image_id)

    ami = AMI.from_aws_model(images["Images"][0], region_from_client(client))
    ami.aws_permissions = perms["LaunchPermissions"]

    return ami

def get_images_with_tags(client, **tags):
    images = []

    res = client.describe_images(Owners=["self"], Filters=[
        {"Name": f"tag:{k}", "Values": [v]} for k, v in tags.items()])

    for image in res["Images"]:
        ami = AMI.from_aws_model(image, region_from_client(client))
        perms = client.describe_image_attribute(
            Attribute="launchPermission", ImageId=ami.image_id)
        ami.aws_permissions = perms["LaunchPermissions"]
        images.append(ami)

    return images

def get_image_with_tags(client, **tags):
    images = get_images_with_tags(client, **tags)
    if len(images) > 1:
        raise Exception(f"Too many images for query {tags!r}")
    elif len(images) == 0:
        return None
    else:
        return images[0]

def get_all_images(client):
    return get_images_with_tags(client)


class ReleaseAMIs:
    """Copy one or more AMIs to other regions and/or set AMI permissions.

    By default, source AMI permissions are applied to their copies, unless
    --public, --private, or --allow-account options are specified.

    If the source AMI's permissions are different than the options provided,
    its permissions will be updated to match.
    """
    command_name = "release"

    @staticmethod
    def add_args(parser):
        parser.add_argument("--source-region", default="us-west-2",
            metavar="REGION", help="region of source AMI(s)")
        rgroup = parser.add_mutually_exclusive_group(required=True)
        rgroup.add_argument("--use-broker", action="store_true",
            help="identity broker provides destination regions and credentials")
        rgroup.add_argument("--region", "-r", action="append", dest="regions",
            metavar="REGION", help="destination region (multiple OK)")
        pgroup = parser.add_mutually_exclusive_group()
        pgroup.add_argument("--public", action="store_true", default=None,
            help="make source and copied AMIs public")
        pgroup.add_argument("--private", dest="public", action="store_false",
            help="make source and copied AMIs private")
        pgroup.add_argument("--allow-account", dest="allow_accounts",
            action="append", metavar="ID", help="make source and copied AMIs "
            "accessible by AWS account id (multiple OK)")
        parser.add_argument("amis", metavar="AMI", nargs="+",
            help="AMI id(s) to copy")

    def get_source_region_client(self, use_broker, source_region):
        if use_broker:
            return IdentityBrokerClient().boto3_session_for_region(
                source_region).client("ec2")
        else:
            return boto3.session.Session(region_name=source_region).client(
                "ec2")

    def copy_image(self, from_client, to_client, image_id):
        source = get_image(from_client, image_id)

        res = to_client.copy_image(
            Name=source.name, Description=source.description,
            SourceImageId=source.image_id, SourceRegion=source.region)

        tags = [
            { "Key": "source_ami", "Value": source.image_id, },
            { "Key": "source_region", "Value": source.region, }
        ]
        tags.extend(source.aws_tags)

        to_client.create_tags(Resources=[res["ImageId"]], Tags=tags)

        return get_image(to_client, res["ImageId"])

    def has_incorrect_perms(self, image, perms):
        if (set(image.allowed_groups) != set(perms['groups'])
                or set(image.allowed_users) != set(perms['users'])):
            return True

    def update_image_permissions(self, client, image):
        client.reset_image_attribute(
            Attribute="launchPermission", ImageId=image.image_id)
        client.modify_image_attribute(
            Attribute="launchPermission", ImageId=image.image_id,
            LaunchPermission={"Add": image.aws_permissions})

    def run(self, args, root, log):
        source_perms = {}
        pending_copy = []
        pending_perms = []
        source_region = args.source_region

        log.info(f"Source region {source_region}")
        source_client = self.get_source_region_client(
            args.use_broker, source_region)

        # resolve source ami perms, queue for fixing if necessary
        for ami in args.amis:
            image = get_image(source_client, ami)

            if args.public is True:
                source_perms[ami] = { 'groups': ['all'], 'users': [] }
            elif args.public is False:
                source_perms[ami] = { 'groups': [], 'users': [] }
            elif args.allow_accounts:
                source_perms[ami] = { 'groups': [], 'users': args.allow_accounts }
            else:
                log.warning(f"Will apply {source_region} {ami} permissions to its copies")
                source_perms[ami] = {
                    'groups': image.allowed_groups,
                    'users': image.allowed_users
                }

            if self.has_incorrect_perms(image, source_perms[ami]):
                log.warning(f"Will update source {source_region} {ami} permissions")
                pending_perms.append((source_client, ami, source_perms[ami]))

        # Copy image to regions where it's missing, queue images that need
        # permission fixes
        log.info('')
        for client in iter_regions(args.use_broker, args.regions):
            region_name = region_from_client(client) # For logging

            # Don't bother copying to source region
            if region_name == args.source_region:
                continue

            log.info(f"Destination region {region_name}...")
            for ami in args.amis:
                src_log = f"* source {ami}"
                image = get_image_with_tags(client,
                    source_ami=ami, source_region=args.source_region)
                if not image:
                    log.info(f"{src_log} - copying to {region_name}")
                    ami_copy = self.copy_image(source_client, client, ami)
                    pending_copy.append(
                        (client, ami_copy.image_id, source_perms[ami]))
                elif self.has_incorrect_perms(image, source_perms[ami]):
                    log.info(f"{src_log} - will update {image.image_id} perms")
                    pending_perms.append(
                        (client, image.image_id, source_perms[ami]))
                else:
                    log.info(f"{src_log} - verified {image.image_id}")
            log.info('')

        if pending_copy:
            # seems to take at least 3m
            pending_copy.append(('sleep', 180, ''))

        # Wait for images to copy
        while pending_copy:
            client, id, perms = pending_copy.pop(0) # emulate a FIFO queue
            if client == 'sleep':
                if not pending_copy:
                    continue
                log.info(f"Sleeping {id}s...")
                time.sleep(id)
                # recheck every 30s
                pending_copy.append(('sleep', 30, ''))
                continue

            region_name = region_from_client(client) # For logging
            image = get_image(client, id)
            if image.state != AMIState.AVAILABLE:
                log.info(f"- copying: {id} ({region_name})")
                pending_copy.append((client, id, perms))
            else:
                done_log = f"+ completed: {id} ({region_name})"
                if self.has_incorrect_perms(image, perms):
                    log.info(f"{done_log} - will update perms")
                    pending_perms.append((client, id, perms))
                else:
                    log.info(f"{done_log} - verified perms")

        # Update all permissions
        for client, id, perms in pending_perms:
            region_name = region_from_client(client) # For logging

            log.info(f"% updating perms: {id} ({region_name})")
            image = get_image(client, id)
            image.allowed_groups = perms['groups']
            image.allowed_users = perms['users']
            self.update_image_permissions(client, image)

        if pending_perms:
            log.info('')

        log.info('Release Completed')


class Releases:

    RELEASE_FIELDS = [
        'description', 'profile', 'profile_build', 'version', 'release',
        'arch', 'revision', 'creation_date', 'end_of_life'
    ]

    def __init__(self, profile=None, use_broker=None, regions=None):
        self.profile = profile
        self.tags = { 'profile': profile }
        self.use_broker = use_broker
        self.regions = regions
        self.clients = {}
        self.reset_images()
        self.reset_releases()

    def reset_images(self):
        self.images = defaultdict(list)

    def reset_releases(self):
        self.releases = dictfactory()

    # TODO: separate Clients class?
    def iter_clients(self):
        if not self.clients:
            for client in iter_regions(self.use_broker, self.regions):
                region = region_from_client(client)
                self.clients[region] = client
                yield (region, client)
        else:
            for region, client in self.clients.items():
                yield (region, client)

    # when we're just interested in the profile's images
    def load_profile_images(self, log=None):
        for region, client in self.iter_clients():
            if log: log.info(f"Loading '{self.profile}' profile images from {region}...")
            self.images[region] = get_images_with_tags(client, **self.tags)

    # not belonging to any profile
    def load_unknown_images(self, log=None):
        for region, client in self.iter_clients():
            if log: log.info(f"Loading unknown images from {region}...")
            for image in get_all_images(client):
                if 'profile' not in image.tags:
                    self.images[region].append(image)

    # build profile releases object based on loaded self.images
    def build_releases(self, log=None, trim=None):
        now = datetime.utcnow()
        versions = dictfactory()

        for region, amis in self.images.items():
            if log: log.info(f"{region}")
            for ami in amis:
                eol = datetime.fromisoformat(ami.end_of_life)
                # if we're trimming, we're not interested in EOL images
                if trim and eol < now:
                    continue

                version = ami.version
                release = ami.release
                build = ami.profile_build
                name = ami.name
                id = ami.image_id
                build_time = int(dateutil.parser.parse(ami.creation_date).strftime('%s'))

                if log: log.info(f" * {ami.image_id} {ami.name}")
                version_obj = versions[version][release][build][name]

                for field in self.RELEASE_FIELDS:
                    if field not in version_obj:
                        version_obj[field] = getattr(ami, field)

                # ensure earliest build_time is used
                if ('build_time' not in version_obj or
                        build_time < version_obj['build_time']):
                    version_obj['build_time'] = build_time
                    version_obj['creation_date'] = ami.creation_date

                version_obj['artifacts'][region] = id

        for version, releases in versions.items():
            for release, builds in sorted(releases.items(), reverse=True,
                    key=lambda x: sortable_version(x[0])):
                for build, revisions in builds.items():
                    for revision, info in sorted(revisions.items(), reverse=True,
                            key=lambda x: x[1]['build_time']):
                        self.releases[release][build][revision] = info
                        # if we are trimming, we want only the most recent revisions
                        if trim: break
                # if we are trimming releases, we want only the most recent release
                if trim == 'release': break


class ReleasesYAML:
    """Update releases/<profile>.yaml with profile's currently existing AMIs
    """
    command_name = "release-yaml"

    @staticmethod
    def add_args(parser):
        TRIM_HELP="""
            revision = keep last x.y.z-r# of non-EOL releases,
            release  = keep last x.y.# of non-EOL versions
            """

        rgroup = parser.add_mutually_exclusive_group(required=True)
        rgroup.add_argument("--use-broker", action="store_true",
            help="identity broker provides destination regions and credentials")
        rgroup.add_argument("--region", "-r", action="append", dest="regions",
            metavar="REGION", help="destination region (multiple OK)")
        parser.add_argument("--trim", "-t",
            choices=['revision','release'], help=TRIM_HELP)
        parser.add_argument("profile", metavar="PROFILE", help="profile name")

    def run(self, args, root, log):
        release_dir = os.path.join(root, 'releases')
        if not os.path.exists(release_dir):
            os.makedirs(release_dir)
        release_yaml = os.path.join(release_dir, f"{args.profile}.yaml")

        r = Releases(
            profile = args.profile,
            use_broker = args.use_broker,
            regions = args.regions)
        r.load_profile_images(log)
        r.build_releases(trim=args.trim)

        log.info(f"Writing new {release_yaml}")
        with open(release_yaml, 'w') as data:
            yaml.dump(undictfactory(r.releases), data, sort_keys=False)


class ReleasesReadme:
    """Build releases/README_<profile>.md from releases/<profile>.yaml
    """
    command_name = "release-readme"

    SECTION_TPL = textwrap.dedent("""
    ### Alpine Linux {release} ({date})
    <details><summary><i>click to show/hide</i></summary><p>

    {rows}

    </p></details>
    """)

    AMI_TPL = (
        " [{id}](https://{r}.console.aws.amazon.com/ec2/home"
        "#Images:visibility=public-images;imageId={id}) "
        "([launch](https://{r}.console.aws.amazon.com/ec2/home"
        "#launchAmi={id})) |"
    )

    @staticmethod
    def add_args(parser):
        parser.add_argument("profile", metavar="PROFILE", help="profile name")

    @staticmethod
    def extract_ver(x):
        return sortable_version(x['release'])

    def resolve_sections(self, release_data, log):
        sects = dictfactory()
        for release, builds in sorted(release_data.items(), reverse=True):
            version = '.'.join(release.split('.')[0:2])
            if version in sects:
                continue
            for build, revisions in builds.items():
                ver = sects[version]
                ver['release'] = release
                name, info = sorted(
                    revisions.items(),
                    key=lambda x: x[1]['build_time'],
                    reverse=True)[0]
                if name in ver['builds']:
                    log.warning(
                        f"Duplicate AMI '{name}' in builds "
                        f"'{info['profile_build']} and "
                        f"'{ver['builds'][name]['build']}")
                ver['builds'][name] = {
                    'build': info['profile_build'],
                    'built': int(info['build_time']),
                    'amis': info['artifacts']
                }
        self.sections = sorted(
            undictfactory(sects).values(),
            key=self.extract_ver,
            reverse=True)

    def get_ami_markdown(self):
        ami_list = "## AMIs\n"

        for section in self.sections:
            built = 0
            regions = []
            rows = ["| Region |", "| ------ |"]

            for name, info in sorted(section['builds'].items()):
                rows[0] += f" {name} |"
                rows[1] += " --- |"
                regions = set(regions) | set(info['amis'].keys())
                built = max(built, info['built'])

            for region in sorted(regions):
                row = f"| {region} |"
                for name, info in sorted(section['builds'].items()):
                    amis = info['amis']
                    if region in amis:
                        row += self.AMI_TPL.format(r=region, id=amis[region])
                    else:
                        row += ' |'
                rows.append(row)

            ami_list += self.SECTION_TPL.format(
                release=section['release'].capitalize(),
                date=datetime.utcfromtimestamp(built).date(),
                rows="\n".join(rows))

        return ami_list

    def run(self, args, root, log):
        profile = args.profile
        release_dir = os.path.join(root, "releases")
        profile_file = os.path.join(release_dir, f"{profile}.yaml")
        with open(profile_file, "r") as data:
            self.resolve_sections(yaml.safe_load(data), log)

        ami_markdown = self.get_ami_markdown()

        readme = ""
        readme_md = os.path.join(release_dir, f"README_{profile}.md")
        action = "Updated"
        if os.path.exists(readme_md):
            with open(readme_md, "r") as file:
                readme = file.read()
        else:
            action = "Created"

        re_images = re.compile(r"## AMIs.*\Z", flags=re.DOTALL)
        if re_images.search(readme):
            readme = re_images.sub(ami_markdown, readme)
        else:
            log.warning("appending")
            readme += "\n" + ami_markdown

        with open(readme_md, "w") as file:
            file.write(readme)

        log.info(f"{action} {readme_md}")


class PruneAMIs:
    """Prune Released AMIs
    """
    command_name = "prune"

    @staticmethod
    def add_args(parser):
        LEVEL_HELP = """
            revision    = x.y.z-r#,
            release     = x.y.#,
            end-of-life = EOL versions (#.#),
            UNKNOWN     = AMIs with no profile tag
            """

        parser.add_argument("level",
            choices=["revision", "release", "end-of-life", "UNKNOWN"],
            help=LEVEL_HELP)
        rgroup = parser.add_mutually_exclusive_group(required=True)
        rgroup.add_argument("--use-broker", action="store_true",
            help="use identity broker to obtain per-region credentials")
        rgroup.add_argument("--region", "-r", metavar='REGION', dest='regions',
            action="append", help="regions to prune, may be specified multiple times")
        parser.add_argument("profile", metavar='PROFILE',
            help="profile to prune")
        parser.add_argument("builds", metavar='BUILD',
            nargs="*", help="build(s) within profile to prune")
        agroup = parser.add_mutually_exclusive_group()
        agroup.add_argument(
            '--keep', metavar='NUM', type=int, default=0,
            help='keep NUM most-recent additional otherwise-pruneable per LEVEL')
        agroup.add_argument('--defer-eol', metavar='DAYS', type=int, default=0,
            help='defer end-of-life pruning for additional days')
        parser.add_argument(
            '--no-pretend', action='store_true', help='actually prune images')

    @staticmethod
    def check_args(args):
        if args.level != 'end-of-life' and args.defer_eol != 0:
            return ["--defer-eol may only be used with 'end-of-life' pruning."]
        if args.keep < 0:
            return ["Only non-negative integers are valid for --keep."]

    def __init__(self):
        self.pruneable = dictfactory()

    def find_pruneable(self, r, args, log):
        level = args.level
        builds = args.builds
        keep = args.keep
        defer_eol = args.defer_eol

        now = datetime.utcnow() - timedelta(days=args.defer_eol)

        # build releases from profile images
        r.load_profile_images(log)
        r.build_releases()

        # scan for pruning criteria
        criteria = dictfactory()
        for release, rdata in r.releases.items():
            for build, bdata in rdata.items():
                if builds and build not in builds:
                    continue
                for ami_name, info in bdata.items():
                    version = info['version']
                    built = info['build_time']
                    eol = datetime.fromisoformat(info['end_of_life'])
                    # default: level == 'release'
                    basis = version
                    if level == 'revision':
                        basis = release
                    elif level == 'end-of-life':
                        # not enough in common with revision/release
                        if build not in criteria[version]:
                            criteria[version][build] = [now]
                        if eol < now and eol not in criteria[version][build]:
                            criteria[version][build].append(eol)
                            criteria[version][build].sort(reverse=True)
                        continue
                    # revsion/release have enough commonality
                    if build not in criteria[basis]:
                        criteria[basis][build] = [built]
                    elif built not in criteria[basis][build]:
                        criteria[basis][build].append(built)
                        criteria[basis][build].sort(reverse=True)

        # scan again to determine what doesn't make the cut
        for release, rdata in r.releases.items():
            for build, bdata in rdata.items():
                if builds and build not in builds:
                    continue
                for ami_name, info in bdata.items():
                    version = info['version']
                    built = info['build_time']
                    eol = datetime.fromisoformat(info['end_of_life'])
                    # default: level == 'release'
                    basis = version
                    value = built
                    if level == 'revision':
                        basis = release
                    elif level == 'end-of-life':
                        value = eol
                    c = criteria[basis][build]
                    if keep < len(c) and value < c[keep]:
                        for region, ami in info['artifacts'].items():
                            self.pruneable[region][ami] = {
                                'name': ami_name,
                            }

        # populate AMI creation_date and snapshot_id from Release().images
        for region, images in r.images.items():
            for image in images:
                if image.image_id in self.pruneable[region]:
                    p = self.pruneable[region][image.image_id]
                    p['name'] = image.name
                    p['creation_date'] = dateutil.parser.parse(
                        image.creation_date).strftime('%Y-%m-%d')
                    p['snapshot_id'] = image.snapshot_id

    def all_unknown_pruneable(self, r, log):
        r.load_unknown_images(log)
        for region, images in r.images.items():
            for image in images:
                self.pruneable[region][image.image_id] = {
                    'name': image.name,
                    'creation_date': dateutil.parser.parse(
                        image.creation_date).strftime('%Y-%m-%d'),
                    'snapshot_id': image.snapshot_id
                }

    def run(self, args, root, log):
        # instantiate Releases object
        r = Releases(
            profile=args.profile,
            use_broker=args.use_broker,
            regions=args.regions)

        if args.level == 'UNKNOWN':
            self.all_unknown_pruneable(r, log)
        else:
            self.find_pruneable(r, args, log)

        for region, amis in sorted(self.pruneable.items()):
            r_str = f"{args.level} AMIs in {region}"
            if not amis:
                log.info(f"No pruneable {r_str}.")
                continue
            if args.no_pretend:
                log.error(f"REMOVING {r_str}:")
            else:
                log.warning(f"Removable {r_str}:")
            for ami, info in sorted(amis.items(), key=lambda x: x[1]['creation_date']):
                a_str = f" * {ami} ({info['creation_date']}) {info['name']}"
                if args.no_pretend:
                    log.warning(a_str)
                    r.clients[region].deregister_image(ImageId=ami)
                    r.clients[region].delete_snapshot(SnapshotId=info['snapshot_id'])
                else:
                    log.info(a_str)


def find_repo_root():
    """Find the root of the repo, which contains a .git folder
    """
    path = os.getcwd()

    while ".git" not in set(os.listdir(path)) and path != "/":
        path = os.path.dirname(path)

    if path == "/":
        raise Exception("No repo found, stopping at /")

    return path


def main():
    """An introspective main method

    Just some silly metaprogramming to make commands really easy to write and
    to avoid needing to hand register them. Commands have a specific interface,
    per below, but should be really easy to create and will be auto discovered.

    Commands are objects that have the following attributes:

        __doc__ (python docstring)
            used as help text in the CLI

        command_name (string)
            name of the command as invoked by the cli

        add_args(parser) (class or static method)
            passed an argparse subparser at setup time that will ultimately
            handle the arguments for the command at runtime. Should add any
            configuration necessary for the command to use later. Must not
            rely on object state as it is not invoked with an instance of the
            object.

        check_args(self, args) (class method, optional)
            passed the set of arguments that were parsed before the command was
            invoked. This function should return a list of errors or None.
            Non-empty lists will cause the command to not be executed and help
            being printed.

        run(self, args, root, log) (instance method)
            passed the arguments object as parsed by argparse as well as a
            string indicating the root of the repository (the folder containing
            the .git folder) and an instance of a stanard libary logger for
            output. Should throw exceptions on error and return when completed.
            Should *not* execute sys.exit
    """
    dispatch = {}

    parser = argparse.ArgumentParser()
    subs = parser.add_subparsers(dest="command_name", required=True)

    # Configure logger
    logger = logging.getLogger()
    handler = logging.StreamHandler()
    handler.setFormatter(ColoredFormatter(fmt="%(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

    for command in sys.modules[__name__].__dict__.values():
        if not hasattr(command, "command_name"):
            continue

        dispatch[command.command_name] = command()

        doc = getattr(command, "__doc__", "")
        subparser = subs.add_parser(
            command.command_name, help=doc, description=doc)

        add_args = getattr(command, "add_args", None)
        if add_args:
            command.add_args(subparser)

    args = parser.parse_args()

    command = dispatch[args.command_name]
    errors = getattr(command, "check_args", lambda x: [])(args)
    if errors:
        logger.error("\n".join(errors))
        # Ugly hack, gets the help for the subcommand, no public API for this
        parser._actions[1]._name_parser_map[args.command_name].print_help()
    else:
        command.run(args, find_repo_root(), logger)


if __name__ == "__main__":
    main()
