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

from collections import defaultdict
from datetime import datetime, timedelta
from distutils.version import StrictVersion
from urllib.request import Request, urlopen

import yaml
import boto3
import pyhocon


class IdentityBrokerClient:

    _DEFAULT_ENDPOINT = "https://aws-access.crute.us/api/account"
    _DEFAULT_ACCOUNT = "alpine-amis-user"

    def __init__(self, endpoint=None, key=None, account=None):
        self.endpoint = endpoint or self._DEFAULT_ENDPOINT
        self.account = account or self._DEFAULT_ACCOUNT
        self.key = key
        self._logger = logging.getLogger(__class__.__name__)

        if override_endpoint := os.environ.get("IDENTITY_BROKER_ENDPOINT"):
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

            if res.status == 429:
                self._logger.warning(
                    "Rate-limited by identity broker, sleeping 30 seconds")
                time.sleep(30)
                continue

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


class ReleaseReadmeUpdater:

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

    def __init__(self, repo_root, profile, archs=None):
        self.repo_root = repo_root
        self.profile = profile
        self.archs = archs or ["x86_64", "aarch64"]

    @staticmethod
    def extract_ver(x):
        return StrictVersion("0.0" if x["release"] == "edge" else x["release"])

    def get_sorted_releases(self, release_data):
        sections = defaultdict(lambda: {
            "release": "",
            "built": {},
            "name": {},
            "ami": defaultdict(dict)
        })

        for build, releases in release_data.items():
            for release, amis in releases.items():
                for name, info in amis.items():
                    arch = info["arch"]
                    built = info["build_time"]
                    ver = sections[info["version"]]

                    if arch not in ver["built"] or ver["built"][arch] < built:
                        ver["release"] = release
                        ver["name"][arch] = name
                        ver["built"][arch] = built

                        for region, ami in info["artifacts"].items():
                            ver["ami"][region][arch] = ami

        return sorted(sections.values(), key=self.extract_ver, reverse=True)

    def make_ami_list(self, sorted_releases):
        ami_list = "## AMIs\n"

        for info in sorted_releases:
            rows = ["| Region |", "| ------ |"]

            for arch in self.archs:
                if arch in info["name"]:
                    rows[0] += f" {info['name'][arch]} |"
                    rows[1] += " --- |"

            for region, amis in info["ami"].items():
                row = f"| {region} |"
                for arch in self.archs:
                    if arch in amis:
                        row += self.AMI_TPL.format(r=region, id=amis[arch])
                rows.append(row)

            ami_list += self.SECTION_TPL.format(
                release=info["release"].capitalize(),
                date=datetime.utcfromtimestamp(
                    max(info["built"].values())).date(),
                rows="\n".join(rows))

        return ami_list

    def update_markdown(self):
        release_dir = os.path.join(self.repo_root, "releases")
        profile_file = os.path.join(release_dir, f"{self.profile}.yaml")

        with open(profile_file, "r") as data:
            sorted_releases = self.get_sorted_releases(yaml.safe_load(data))

        readme_md = os.path.join(release_dir, "README.md")

        with open(readme_md, "r") as file:
            readme = file.read()

        with open(readme_md, "w") as file:
            file.write(
                re.sub("## AMIs.*\Z", self.make_ami_list(sorted_releases),
                    readme, flags=re.S))


class GenReleaseReadme:
    """Update release README
    """

    command_name = "gen-release-readme"

    @staticmethod
    def add_args(parser):
        parser.add_argument("profile", help="name of profile to update")

    def run(self, args, root):
        ReleaseReadmeUpdater(root, args.profile).update_markdown()


class MakeAMIs:
    """Build Packer JSON variable files from HOCON build profiles
    """

    command_name = "make-amis"

    @staticmethod
    def add_args(parser):
        parser.add_argument("profile", help="name of profile to build")
        parser.add_argument("builds", nargs="*",
            help="name of builds within a profile to build")

    def run(self, args, root):
        os.chdir(os.path.join(root, "build"))

        builds = args.builds or os.listdir(
            os.path.join("profile", args.profile))

        for build in builds:
            print(f"\n*** Building {args.profile}/{build} ***\n\n")

            build_dir = os.path.join("profile", args.profile, build)
            if not os.path.exists(build_dir):
                print(f"Build dir '{build_dir}' does not exist")
                break

            out = io.StringIO()

            res = subprocess.Popen([
                    os.environ.get("PACKER", "packer"),
                    "build",
                    f"-var-file={build_dir}/vars.json",
                    "packer.json"
                ], stdout=subprocess.PIPE, encoding="utf-8")

            while res.poll() is None:
                text = res.stdout.readline()
                out.write(text)
                print(text, end="")

            if res.returncode == 0:
                subprocess.run([os.path.join(root, "build", "builder"),
                    "update-releases", args.profile, build])
            else:
                if "is used by an existing AMI" in out.getvalue():
                    continue
                else:
                    sys.exit(res.returncode)

        print("\n=== DONE ===\n")


class PruneAMIs:
    """Prune AMIs from AWS
    """

    command_name = "prune-amis"

    @staticmethod
    def add_args(parser):
        LEVEL_HELP = textwrap.dedent("""\
        revision  - keep only the latest revision per release
        release   - keep only the latest release per version
        version   - keep only the versions that aren't end-of-life
        """)

        parser.add_argument(
            "level", choices=["revision", "release", "version"],
            help=LEVEL_HELP)
        parser.add_argument("profile", help="profile to prune")
        parser.add_argument(
            "build", nargs="?", help="build within profile to prune")

    @staticmethod
    def delete_image(ec2, image):
        ec2.deregister_image(ImageId=image["ImageId"])

        for blockdev in image["BlockDeviceMappings"]:
            if "Ebs" not in blockdev:
                continue

            ec2.delete_snapshot(SnapshotId=blockdev["Ebs"]["SnapshotId"])

    def run(self, args, root):
        now = datetime.utcnow()
        release_yaml = os.path.join(root, "releases", f"{args.profile}.yaml")

        with open(release_yaml, "r") as data:
            before = yaml.safe_load(data)

        known = defaultdict(list)
        prune = defaultdict(list)
        after = defaultdict(lambda: defaultdict(dict))

        # for all builds in the profile...
        for build_name, releases in before.items():
            # this is not the build that was specified
            if args.build is not None and args.build != build_name:
                print(f"< skipping {args.profile}/{build_name}")
                # ensure its release data remains intact
                after[build_name] = before[build_name]
                continue
            else:
                print(f"> PRUNING {args.profile}/{build_name} for {args.level}")

            criteria = {}

            # scan releases for pruning criteria
            for release, amis in releases.items():
                for ami_name, info in amis.items():
                    version = info["version"]
                    built = info["build_time"]

                    if eol := info.get("end_of_life"):
                        eol = datetime.fromisoformat(info["end_of_life"])

                    for region, ami_id in info["artifacts"].items():
                        known[region].append(ami_id)

                    if args.level == "revision":
                        # find build timestamp of most recent revision, per release
                        if release not in criteria or built > criteria[release]:
                            criteria[release] = built
                    elif args.level == "release":
                        # find build timestamp of most recent revision, per version
                        if version not in criteria or built > criteria[version]:
                            criteria[version] = built
                    elif args.level == "version":
                        # find latest EOL date, per version
                        if (
                            version not in criteria or
                            (not criteria[version]) or
                            (eol and eol > criteria[version])
                        ):
                            criteria[version] = eol

            # rescan again to determine what doesn't make the cut
            for release, amis in releases.items():
                for ami_name, info in amis.items():
                    version = info["version"]

                    if eol := info.get("end_of_life"):
                        eol = datetime.fromisoformat(info["end_of_life"])

                    if args.level == "revision":
                        if info["build_time"] < criteria[release]:
                            for region, ami_id in info["artifacts"].items():
                                prune[region].append(ami_id)
                    elif args.level == "release":
                        if info["build_time"] < criteria[version]:
                            for region, ami_id in info["artifacts"].items():
                                prune[region].append(ami_id)
                    elif args.level == "version":
                        if criteria[version] and (
                            (version != "edge" and criteria[version] < now) or
                            (version == "edge" and ((not eol) or (eol < now)))
                        ):
                            for region, ami_id in info["artifacts"].items():
                                prune[region].append(ami_id)
                    else:
                        after[build_name][release][ami_name] = info

        for session in IdentityBrokerClient().iter_regions():
            region = session.region_name

            print(f"* scanning: {region} ...")

            ec2 = session.client("ec2")
            for image in ec2.describe_images(Owners=["self"])["Images"]:
                image_name, image_id = image["Name"], image["ImageId"]

                if region in prune and image["ImageId"] in prune[region]:
                    print(f"REMOVE: {image_name} = {image_id}")
                    self.delete_image(image)
                elif region in known and image["ImageId"] in known[region]:
                    print(f"KEEP: {image_name} = {image_id}")
                else:
                    print(f"UNKNOWN: {image_name} = {image_id}")

        # update releases/<profile>.yaml
        with open(release_yaml, "w") as data:
            yaml.dump(after, data, sort_keys=False)


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
    def resolve_tomorrow(cls, input):
        return cls.tomorrow.isoformat(timespec="seconds")

    @classmethod
    def resolve_now(cls, input):
        return cls.now.strftime("%Y%m%d%H%M%S")

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
            "ami_access"     : self.fold_comma,
            "ami_regions"    : self.fold_comma,
            "kernel_modules" : self.fold_comma,
            "kernel_options" : self.fold_space,
            "repos"          : self.fold_repos,
            "pkgs"           : self.fold_packages,
            "svcs"           : self.fold_services,
            "revision"       : self.resolve_now,
            "end_of_life"    : lambda x: \
                self.force_iso_date(self.resolve_tomorrow(x)),
        }

    def build_all(self):
        for file in glob.glob(os.path.join(self.config_path, "*.conf")):
            profile = os.path.splitext(os.path.split(file)[-1])[0]
            self.build_profile(profile)

    def build_profile(self, profile):
        build_config = pyhocon.ConfigFactory.parse_file(
            os.path.join(self.config_path, f"{profile}.conf"))

        for build, cfg in build_config["BUILDS"].items():
            build_dir = os.path.join(self.out_dir, profile, build)

            # Always start fresh
            shutil.rmtree(build_dir, ignore_errors=True)
            os.makedirs(build_dir)

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


class ResolveProfiles:
    """Build Packer JSON variable files from HOCON build profiles
    """

    command_name = "resolve-profiles"

    @staticmethod
    def add_args(parser):
        parser.add_argument(
            "profile", help="name of profile to build", nargs="*")

    def run(self, args, root):
        builder = ConfigBuilder(
            os.path.join(root, "profiles"),
            os.path.join(root, "build", "profile"))

        if args.profile:
            for profile in args.profile:
                builder.build_profile(profile)
        else:
            builder.build_all()


class UpdateReleases:
    """Update release YAML
    """

    command_name = "update-releases"

    @staticmethod
    def add_args(parser):
        parser.add_argument("profile", help="name of profile to update")
        parser.add_argument("build", help="name of build to update")

    @staticmethod
    def parse_ids(ids):
        parsed = re.split(":|,", ids)
        return dict(zip(parsed[0::2], parsed[1::2]))

    def run(self, args, root):
        release_dir = os.path.join(root, "releases")
        if not os.path.exists(release_dir):
            os.makedirs(release_dir)

        release_yaml = os.path.join(release_dir, f"{args.profile}.yaml")
        releases = {}
        if os.path.exists(release_yaml):
            with open(release_yaml, "r") as data:
                releases = yaml.safe_load(data)

        manifest_json = os.path.join(
            root, "build", "profile", args.profile, args.build,
            "manifest.json")
        with open(manifest_json, "r") as data:
            manifest = json.load(data)

        data = manifest["builds"][0]["custom_data"]
        release = data["release"]

        if args.build not in releases:
            releases[args.build] = {}

        if release not in releases[args.build]:
            releases[args.build][release] = {}

        releases[args.build][release][data["ami_name"]] = {
            "description": data["ami_desc"],
            "profile": args.profile,
            "profile_build": args.build,
            "version": data["version"],
            "release": release,
            "arch": data["arch"],
            "revision": data["revision"],
            "end_of_life": data["end_of_life"],
            "build_time": manifest["builds"][0]["build_time"],
            "artifacts": self.parse_ids(manifest["builds"][0]["artifact_id"]),
        }

        with open(release_yaml, "w") as data:
            yaml.dump(releases, data, sort_keys=False)


class ConvertPackerJSON:
    """Convert packer.conf to packer.json
    """

    command_name = "convert-packer-config"

    @staticmethod
    def add_args(parser):
        pass

    def run(self, args, root):
        source = os.path.join(root, "packer.conf")
        dest = os.path.join(root, "build", "packer.json")

        logging.getLogger().setLevel(logging.INFO)

        pyhocon.converter.HOCONConverter.convert_from_file(
            source, dest, "json", 2, False)


def find_repo_root():
    path = os.getcwd()

    while ".git" not in set(os.listdir(path)) and path != "/":
        path = os.path.dirname(path)

    if path == "/":
        raise Exception("No repo found, stopping at /")

    return path


def main():
    dispatch = {}

    parser = argparse.ArgumentParser()
    subs = parser.add_subparsers(dest="command_name", required=True)

    for command in sys.modules[__name__].__dict__.values():
        if not hasattr(command, "command_name"):
            continue

        dispatch[command.command_name] = command()

        doc = getattr(command, "__doc__", "")
        subparser = subs.add_parser(
            command.command_name, help=doc, description=doc)

        if add_args := getattr(command, "add_args", None):
            command.add_args(subparser)

    args = parser.parse_args()
    dispatch[args.command_name].run(args, find_repo_root())


if __name__ == "__main__":
    main()
