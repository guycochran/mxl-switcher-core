# Immutable Core deployment bundle

This is the boring, repeatable way to build and start MXL Switcher Core, either
from EasyMXL or directly by an engineer. One tool,
[`packaging/core-bundle/mxl_core_bundle.py`](../packaging/core-bundle/mxl_core_bundle.py),
does five things, and every one is a **dry run unless `--apply` is given**:

| Command | Purpose |
| --- | --- |
| `tag` | Print the immutable image reference for a version and revision. |
| `build` | Build from a clean commit, label the image, and write a release manifest and pinned catalog entry. |
| `verify` | Check an image's OCI labels against its tag. |
| `launch` | Validate a facility config, then print or run the exact `docker run`. |
| `cleanup` | Remove exactly one candidate container (and optionally its image). |

The tool never stops, renames, replaces, or prunes anything implicitly.

## Image identity

The immutable tag is `<semver>.g<first 12 hex digits of the Git commit>`, for
example `mxl-switcher-core:1.0.0.g0123456789ab`. It is derived only from the
version and the commit, so the same inputs always give the same tag. A tag is
never rebuilt: `build --apply` refuses if it already exists.

Images carry these OCI labels:

- `org.opencontainers.image.version`: the semantic version.
- `org.opencontainers.image.revision`: the full 40-hex commit.
- `org.opencontainers.image.source`, `.title`, `.description`.
- `org.opencontainers.image.base.name` and `.base.digest`: the licensed local
  base image and the exact image ID it resolved to at build time. The build
  refuses to finish if the base tag moved during the build.

`verify` (and every production `launch`) refuses an image whose labels do not
match its tag. Production mode refuses `latest`, `local`, version-only tags
such as `0.2.0-candidate`, untagged references, and bare digests.

## Build

From a clean checkout on a host that has the licensed MXL base image:

```sh
packaging/easy-mxl/build-local-image.sh 1.0.0            # dry run: prints the exact build
packaging/easy-mxl/build-local-image.sh 1.0.0 --apply    # builds, verifies, writes the release
```

A checkout with uncommitted changes is refused, because the revision label
would not describe the image. A successful build writes
`build/release/<tag>/`, which contains:

- `manifest.json`: image reference, image ID, version, revision, and base
  image and digest;
- `mxl-switcher-core.catalog.json`: the repository's EasyMXL catalog entry
  with `image` pinned to the immutable tag.

No image is pushed to a registry. MXL SDK redistribution terms must be resolved
before publishing one.

## Facility config

Core starts from a **caller-supplied** facility config and output definitions.
Neither the image nor the catalog contains facility flow IDs, host paths,
addresses, meeting data, or credentials. A synthetic example lives in
[`packaging/core-bundle/example-facility/`](../packaging/core-bundle/example-facility/).

Inside the container:

| Path | Contents |
| --- | --- |
| `/mxl-domain` | the MXL domain (an explicit volume or bind mount) |
| `/etc/mxl-switcher-core.conf` | the facility config (read-only) |
| `/etc/mxl-switcher-core/definitions/` | Program and Preview output definitions (read-only) |

`launch` validates, before anything starts, that:

- the config is well formed and complete, with `domain=/mxl-domain`,
  `control_host=0.0.0.0`, `control_port=18085`, and at least two sources;
- the Program and Preview definition files exist and are `video/v210`, and
  their IDs equal `program_id` and `preview_id`;
- the Program and Preview IDs are distinct and do not reuse a source flow ID;
- exactly one explicit MXL domain mount is given (`--domain-volume` or
  `--domain-path`);
- no container with the requested name exists. The check uses the exact
  name, never a prefix or pattern;
- the host port is free on the bind address, which defaults to loopback;
- in production mode, the image is immutable and its labels verify, and a
  `--rollback-container` is named.

Production launches must supply their own output definitions. The acceptance
definitions bundled in the image are accepted only with `--mode test`.

## Preflight and launch

```sh
python3 packaging/core-bundle/mxl_core_bundle.py launch \
  --image mxl-switcher-core:1.0.0.g0123456789ab \
  --name mxl-core-candidate-1 \
  --config packaging/core-bundle/example-facility/core.conf \
  --definitions packaging/core-bundle/example-facility/definitions \
  --domain-volume example-mxl-domain \
  --host-port 18195 \
  --rollback-container mxl-core-operator
```

The dry run prints JSON with:

- the verified image identity;
- the container name and restart policy;
- the loopback publish mapping;
- the domain, config, and definitions mounts;
- both output IDs;
- the rollback target (inspected read-only and left untouched);
- the exact cleanup command;
- `bolt_ons_started: []`;
- the full `docker run` command.

Re-run with `--apply` to start exactly that container. Only one Docker
mutation occurs.

## Cleanup and rollback

Cleanup is exact. It removes only the named container, and only if that
container runs the expected image. It never runs a prune, a pattern match, or
a volume deletion:

```sh
python3 packaging/core-bundle/mxl_core_bundle.py cleanup \
  --name mxl-core-candidate-1 \
  --expected-image mxl-switcher-core:1.0.0.g0123456789ab \
  --remove-image mxl-switcher-core:1.0.0.g0123456789ab        # optional; dry run without --apply
```

Because a launch never replaces an existing service, **rollback of a parallel
candidate is just the cleanup above**. The service named by
`--rollback-container` was never touched.

Promoting a candidate to operator service is a manual step, done in a declared
maintenance window and outside this tool:

1. Record the operator container's name, image ID, and config path. This is
   the `rollback` block from the dry run.
2. Stop the operator container. Do not remove it.
3. Launch the immutable candidate under a new name on the operator port. Then
   verify state, health, routing, and every bolt-on against it.
4. To roll back, stop and clean up the candidate exactly, then start the
   recorded operator container again.
5. Only after an agreed soak, remove the old operator container by its exact
   name.

## Bolt-ons stay explicit

Starting Core starts only Core. Each of these is launched, versioned, and
rolled back on its own, against Core's control API or MXL flows, and never as
a hidden side effect of Core startup:

- the reference Skin and the EasyMXL Source Recovery panel;
- Companion and the NMOS node;
- Program Audio;
- confidence monitoring;
- destination adapters.

## EasyMXL catalog gap

EasyMXL's `imagePolicy: local` lets the launch dialog offer **every** loaded
tag of `imageRepository`. A pinned catalog entry from `build/release/<tag>/`
sets the default `image` to the immutable tag, but the catalog schema cannot
forbid choosing another tag. Until EasyMXL can express that (for example, an
allowed-tag pattern or a required image ID), there are two options:

- use `launch` for production starts, which enforces immutability and labels;
  or
- remove superseded and mutable tags of `mxl-switcher-core` from the host
  before using the EasyMXL dialog.

The companion `manifest.json` records the exact image ID to compare against
after an EasyMXL launch. Run `verify --image <tag>` before trusting it.
