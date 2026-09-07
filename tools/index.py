#!/usr/bin/env python3
"""Build every plugin this registry lists, and write the index Crook reads.

    tools/index.py --out build

For each `plugins/<owner>.<name>/plugin.toml`, for each release in it: check
out the commit it names, build it to `wasm32-unknown-unknown` with the
toolchain that repository pins, ask `crook-plugin-info` what was built, and
write the artifact and one line of the index.

Three rules are enforced here rather than trusted:

* **Nothing is indexed that was not built here.** A plugin's repository can
  publish whatever it likes; what the registry serves is what this script
  produced from the commit in `plugin.toml`, so a person installing from the
  store is running bytes that came out of source a reviewer could read.
* **The module says what it is.** The id, version, ABI and capability list are
  read out of the artifact by Crook's own reader, never from the TOML. A
  `plugin.toml` whose `id` disagrees with the module's is a registry entry
  pointing at the wrong repository, and it fails the build.
* **A version is built once.** Two releases that produce the same version are a
  mistake in the TOML rather than something to resolve quietly.

The reader is `crook-plugin-info`, from `cargo install crook_wasm --version
0.<abi>`. It is Crook's own, which is the point: an index that described a
plugin differently from the terminal that loads it would be an index nobody
could act on.
"""

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

# What the index says it is, and what Crook's reader expects.
SCHEMA = 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plugins", default="plugins", type=Path,
                        help="the directory of plugin.toml directories")
    parser.add_argument("--out", default="build", type=Path,
                        help="where index.json and artifacts/ are written")
    parser.add_argument("--work", default=".work", type=Path,
                        help="where sources are checked out and built")
    parser.add_argument("--base-url", default="https://theguriev.github.io/crook-plugins",
                        help="what the artifact URLs are relative to")
    parser.add_argument("--reader", default="crook-plugin-info",
                        help="the command that reads a module's manifest")
    parser.add_argument("--only", default=None,
                        help="build one plugin, by id")
    parser.add_argument("--from", dest="local", default=None, type=Path,
                        help="a directory of checkouts to build from instead of cloning, "
                             "for trying this before anything is pushed")
    arguments = parser.parse_args()

    entries = sorted(arguments.plugins.glob("*/plugin.toml"))
    if not entries:
        print(f"no plugins under {arguments.plugins}", file=sys.stderr)
        return 1

    artifacts = arguments.out / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)

    listed = []
    for entry in entries:
        plugin = tomllib.loads(entry.read_text())
        if arguments.only and plugin["id"] != arguments.only:
            continue
        try:
            listed.append(one(plugin, entry, arguments, artifacts))
        except Failed as failure:
            print(f"{entry}: {failure}", file=sys.stderr)
            return 1

    index = {"schema": SCHEMA, "plugins": listed}
    written = arguments.out / "index.json"
    written.write_text(json.dumps(index, indent=1, sort_keys=False) + "\n")
    print(f"{written}: {len(listed)} plugins, "
          f"{sum(len(one['versions']) for one in listed)} versions")
    return 0


class Failed(Exception):
    """Something a person has to fix in a plugin.toml or in a plugin."""


def one(plugin, entry, arguments, artifacts):
    """Every release of one plugin, built and described."""
    plugin_id = plugin["id"]
    if plugin.get("schema") != SCHEMA:
        raise Failed(f"schema {plugin.get('schema')!r}, and this builds {SCHEMA}")
    if entry.parent.name != plugin_id.replace("/", "."):
        raise Failed(f"is {plugin_id}, so its directory should be "
                     f"{plugin_id.replace('/', '.')}")

    versions = []
    named = None
    for release in plugin.get("release", []):
        built = build(plugin, release, arguments)
        described = describe(built, arguments.reader)

        if described["id"] != plugin_id:
            raise Failed(f"names {plugin_id} and the module at {release['ref']} "
                         f"says it is {described['id']}")
        if any(version["version"] == described["version"] for version in versions):
            raise Failed(f"has {described['version']} twice")

        name = f"{plugin_id.replace('/', '.')}-{described['version']}.wasm"
        shutil.copyfile(built, artifacts / name)
        bytes_ = (artifacts / name).read_bytes()

        version = {
            "version": described["version"],
            "abi": described["abi"],
            "url": f"{arguments.base_url}/artifacts/{name}",
            "sha256": hashlib.sha256(bytes_).hexdigest(),
            "bytes": len(bytes_),
            "capabilities": described["capabilities"],
            "asks": described["asks"],
            "yanked": release.get("yanked"),
            "ref": release["ref"],
        }
        versions.append(version)
        # The name and the line under it live in a manifest, so they are the
        # newest built version's rather than the TOML's: a plugin is entitled
        # to rename itself, and a registry entry must not be able to describe
        # one as something other than what it says it is.
        named = described
        print(f"  {plugin_id} {described['version']} "
              f"(abi {described['abi']}, {len(bytes_)} bytes)")

    if not versions or named is None:
        raise Failed("lists no releases")

    return {
        "id": plugin_id,
        "name": named["name"],
        "description": named["description"],
        "repository": plugin.get("repository", ""),
        "license": plugin.get("license", ""),
        "versions": versions,
    }


def build(plugin, release, arguments):
    """Checks out one commit, builds it, and answers with the module."""
    repository = plugin["repository"]
    ref = release["ref"]
    at = arguments.work / plugin["id"].replace("/", ".") / ref

    if arguments.local:
        source = arguments.local / Path(repository).name
        if not source.is_dir():
            raise Failed(f"{source} is not a checkout to build from")
        # Copied rather than built in place: a build in somebody's own
        # checkout is a build that can pick up whatever they have uncommitted.
        if at.exists():
            shutil.rmtree(at)
        at.parent.mkdir(parents=True, exist_ok=True)
        run(["git", "clone", "--quiet", "--no-hardlinks", str(source), str(at)])
        run(["git", "-C", str(at), "checkout", "--quiet", ref])
    elif not at.exists():
        at.mkdir(parents=True)
        run(["git", "-C", str(at), "init", "--quiet"])
        run(["git", "-C", str(at), "remote", "add", "origin", repository])
        run(["git", "-C", str(at), "fetch", "--quiet", "--depth", "1", "origin", ref])
        run(["git", "-C", str(at), "checkout", "--quiet", "FETCH_HEAD"])

    # The target the sandbox links. Not `wasm32-wasip1`, which imports things a
    # module is refused for importing.
    run(["rustup", "target", "add", "wasm32-unknown-unknown"], at)
    run(["cargo", "build", "--release", "--locked", "--target", "wasm32-unknown-unknown"], at)

    modules = sorted((at / "target/wasm32-unknown-unknown/release").glob("*.wasm"))
    if len(modules) != 1:
        raise Failed(f"built {len(modules)} modules at {ref} and a plugin is one: "
                     f"{[module.name for module in modules]}")
    return modules[0]


def describe(module, reader):
    """What Crook's own reader says the module is."""
    answered = subprocess.run([reader, str(module)], capture_output=True, text=True)
    if answered.returncode == 2:
        raise Failed(f"is built for another plugin ABI: {answered.stderr.strip()}")
    if answered.returncode != 0:
        raise Failed(f"could not be read: {answered.stderr.strip()}")
    return json.loads(answered.stdout)


def run(command, at=None):
    """One command, or the reason it did not work."""
    outcome = subprocess.run(command, cwd=at, capture_output=True, text=True)
    if outcome.returncode != 0:
        raise Failed(f"`{' '.join(command)}` failed:\n{outcome.stdout}{outcome.stderr}")
    return outcome.stdout


if __name__ == "__main__":
    sys.exit(main())
