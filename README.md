# Alpine Linux OMI Builder

This is an unofficial builder for Outscale Alpine Linux Images.

## Custom OMIs

Using the scripts and configuration in this project, you can build your own
custom Alpine Linux OMIs.  If you experience any problems building custom OMIs,
please open an [issue](https://github.com/mcrute/alpine-ec2-ami/issues) and
include as much detailed information as possible.

### Build Requirements

* [Packer](https://packer.io) >= 1.4.1
* [Python 3.x](https://python.org) (3.7 is known to work)
* an Outscale account

### Profile Configuration

Target profile config files reside in the [profiles](profiles) subdirectory,
where you will also find the [config](profiles/alpine.conf) we use for our
pre-built AMIs.  Refer to the [README](profiles/README.md) in that subdirectory
for more details and information about how OMI profile configs work.

### Outscale Credentials

These scripts use the `boto3` library to interact with AWS, enabling you to
provide your AWS account credentials in a number of different ways.  see the
offical `boto3` documentation's section on
[configuring credentials](https://boto3.amazonaws.com/v1/documentation/api/latest/guide/credentials.html#guide-credentials)
for more details.  *Please note that these scripts do not implement the first
two methods on the list.*

### Building OMIs

To build all build targets in a target profile, simply...
```
./scripts/builder.py omis <profile>
```

You can also build specfic build targets within a profile:
```
./scripts/builder.py omis <profile> <build1> <build2> ...
```

Before each build, new Alpine Linux *releases* are detected and the version's
core profile is updated.

If there's already an OMI with the same name as the profile build's, that build
will be skipped and the process moves on to build the other profile's build
targets (if any).

After each successful build, `releases/<profile>.yaml` is updated with the
build's details, including (most importantly) the ids of the AMI artifacts that
were built.

Additional information about using your custom AMIs can be found in the
[README](releases/README.md) in the [releases](releases) subdirectory.

### Pruning AMIs

Every now and then, you may want to clean up old AMIs from your EC2 account and
your profile's `releases/<profile>.yaml`.  There are three different levels of
pruning:
* `revision` - keep only the latest revision for each release
* `release` - keep only the latest release for each version
* `end-of-life` - remove any end-of-life versions

To prune a profile (or optionally one build target of a profile)...
```
./scripts/builder.py prune-amis <profile> [<build>]
```

### Updating the Release README

This make target updates the [releases README](releases/README.md), primarily
for updating the list of our pre-built AMIs.  This may-or-may-not be useful for
other target profiles.
```
./scripts/builder.py gen-release-readme <profile>
```

### Cleaning up the Build Environment

The build process is careful to place all temporary files in the `build`
subdirectory. Remove the temporary `build` subdirectory, which contains the
resolved profile and Packer configs, the Python virtual environment, and other
temporary build-related artifacts.

## Caveats

* New Alpine Linux *versions* are currently not auto-detected and added as a
  core version profile; this process is, at the moment, still a manual task.
