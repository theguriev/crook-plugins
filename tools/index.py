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
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

# What the index says it is, and what Crook's reader expects.
SCHEMA = 1

# What the registry will clone from.
#
# Not a general URL: this string becomes `git remote add`'s argument, and git
# takes more kinds of thing there than anybody means — `ext::sh -c …` runs a
# command, a local path clones a directory on the runner, and a leading dash is
# an option. A pull request is a stranger's text, so the shape is the narrow
# one the registry actually lists.
REPOSITORY = re.compile(r"\Ahttps://github\.com/[A-Za-z0-9._-]{1,64}/[A-Za-z0-9._-]{1,64}\Z")

# And what it will check out. A full commit, because a tag can be moved after
# somebody reviewed it — which CONTRIBUTING says and nothing checked.
COMMIT = re.compile(r"\A[0-9a-f]{40}\Z")

# The id shape the host parses, and refuses: lowercase letters, digits, `-` and
# `_`, in two parts. An index row the terminal drops is a plugin nobody can
# install and a build nobody needed.
PLUGIN_ID = re.compile(r"\A[a-z0-9_-]{1,64}/[a-z0-9_-]{1,64}\Z")

# Lowercase hex, which is what `hashlib` writes and what the terminal compares.
SHA256 = re.compile(r"\A[0-9a-f]{64}\Z")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plugins", default="plugins", type=Path,
                        help="the directory of plugin.toml directories")
    parser.add_argument("--out", default="build", type=Path,
                        help="where index.json and artifacts/ are written")
    parser.add_argument("--work", default=".work", type=Path,
                        help="where sources are checked out and built")
    parser.add_argument(
        "--base-url",
        default="https://github.com/theguriev/crook-plugins/releases/download/index",
        help="where the artifacts will be served from; one name is joined to it")
    parser.add_argument("--reader", default="crook-plugin-info",
                        help="the command that reads a module's manifest")
    parser.add_argument("--only", default=None,
                        help="build one plugin, by id")
    parser.add_argument("--keep", default=None, type=Path,
                        help="an index already published: every version in it is carried over "
                             "rather than built again")
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

    # What is already published, if this run was given it. A version anybody
    # could have installed keeps the artifact and the hash it went out with:
    # two builds of one commit on different toolchain patch versions produce
    # different modules, so rebuilding is not "the same thing again", it is a
    # new artifact under an old version's name.
    kept = {}
    named = {}
    if arguments.keep and arguments.keep.exists():
        for plugin in json.loads(arguments.keep.read_text()).get("plugins", []):
            named[plugin["id"]] = plugin
            for version in plugin.get("versions", []):
                # Keyed by the commit rather than by the version, because that
                # is what a `plugin.toml` names and what this can compare
                # before deciding whether to build anything.
                if "ref" in version:
                    kept[(plugin["id"], version["ref"])] = version
        print(f"{len(kept)} versions already published")

    listed = []
    for entry in entries:
        try:
            plugin = tomllib.loads(entry.read_text())
        except tomllib.TOMLDecodeError as why:
            print(f"{entry}: is not TOML: {why}", file=sys.stderr)
            return 1
        if arguments.only and plugin.get("id") != arguments.only:
            continue
        try:
            listed.append(one(plugin, entry, arguments, artifacts, kept, named))
        except Failed as failure:
            print(f"{entry}: {failure}", file=sys.stderr)
            return 1

    if not listed:
        # Either `--only` named something that is not here, or every entry was
        # skipped. Writing an index at that point would publish a list with
        # nothing in it, which every Crook reading it would believe.
        print(f"nothing to index{f' for {arguments.only}' if arguments.only else ''}",
              file=sys.stderr)
        return 1

    index = {"schema": SCHEMA, "plugins": listed}
    if arguments.only:
        # One plugin is a build, not an index: publishing this would take every
        # other plugin off the list.
        print(f"built {arguments.only} and wrote no index: --only is for trying one entry")
        return 0

    check(index)
    written = arguments.out / "index.json"
    written.write_text(json.dumps(index, indent=1, sort_keys=False) + "\n")
    print(f"{written}: {len(listed)} plugins, "
          f"{sum(len(one['versions']) for one in listed)} versions")
    return 0


def check(index):
    """Refuses an index the terminal would read differently than it was meant.

    Everything here is something `app/src/plugins/store/index.rs` either drops
    on the floor or refuses outright — a row with a name it cannot parse, a
    hash it cannot compare, a URL it will not fetch. Catching it here is the
    difference between a build that fails and a list that quietly lost a
    plugin.
    """
    seen = set()
    for plugin in index["plugins"]:
        if plugin["id"] in seen:
            raise Failed(f"{plugin['id']} is in the index twice")
        seen.add(plugin["id"])

        for version in plugin["versions"]:
            where = f"{plugin['id']} {version['version']}"
            if not version["url"].startswith("https://"):
                raise Failed(f"{where} is served over {version['url']!r}, and the terminal "
                             "fetches https and nothing else")
            if not SHA256.fullmatch(version["sha256"]):
                raise Failed(f"{where} has {version['sha256']!r} for a hash")
            if version["bytes"] <= 0:
                raise Failed(f"{where} is {version['bytes']} bytes")
            if not isinstance(version["abi"], int):
                raise Failed(f"{where} says its ABI is {version['abi']!r}")


class Failed(Exception):
    """Something a person has to fix in a plugin.toml or in a plugin."""


def one(plugin, entry, arguments, artifacts, kept, published):
    """Every release of one plugin, built and described."""
    plugin_id = plugin.get("id")
    if plugin.get("schema") != SCHEMA:
        raise Failed(f"schema {plugin.get('schema')!r}, and this builds {SCHEMA}")
    if not isinstance(plugin_id, str) or not PLUGIN_ID.fullmatch(plugin_id):
        raise Failed(f"names {plugin_id!r}, which is not an `owner/name` the terminal parses: "
                     "lowercase letters, digits, `-` and `_`")
    if entry.parent.name != plugin_id.replace("/", "."):
        raise Failed(f"is {plugin_id}, so its directory should be "
                     f"{plugin_id.replace('/', '.')}")

    repository = plugin.get("repository", "")
    if not REPOSITORY.fullmatch(repository):
        raise Failed(f"points at {repository!r}, and a repository here is "
                     "https://github.com/<owner>/<name>")
    if not plugin.get("license"):
        raise Failed("names no licence")

    versions = []
    named = None
    for release in plugin.get("release", []):
        ref = release.get("ref", "")
        if not COMMIT.fullmatch(ref):
            raise Failed(f"lists {ref!r}, and a release here is a 40-character commit: what is "
                         "built is what is indexed, and a tag can be moved after it is reviewed")

        # Already out there, so it is carried over rather than built: the
        # artifact and the hash it went out with are what somebody may already
        # have installed, and a rebuild of the same commit is a *different*
        # module under an old version's name. Only what a person can change
        # without changing the artifact — whether it is withdrawn — is taken
        # from the file rather than from the index.
        if (plugin_id, ref) in kept:
            carried = dict(kept[(plugin_id, ref)])
            carried["yanked"] = release.get("yanked")
            versions.append(carried)
            print(f"  {plugin_id} {carried['version']} (published already)")
            continue

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
            "url": f"{arguments.base_url}/{name}",
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

    if not versions:
        raise Failed("lists no releases")

    # A plugin whose every version was carried over built nothing, so what it
    # is *called* comes from the index that carried them.
    if named is None:
        named = published.get(plugin_id)
    if named is None:
        raise Failed("was carried over from an index that does not describe it")

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
    elif not (at / ".git" / "FETCHED").exists():
        # A fetch that failed half way leaves a directory that is neither empty
        # nor a checkout, and the next run would build whatever is in it — or
        # fail with a line about the wrong thing. So the marker is written last
        # and anything without it is thrown away first.
        if at.exists():
            shutil.rmtree(at)
        at.mkdir(parents=True)
        run(["git", "-C", str(at), "init", "--quiet"])
        run(["git", "-C", str(at), "remote", "add", "origin", "--", repository])
        run(["git", "-C", str(at), "fetch", "--quiet", "--depth", "1", "origin", ref])
        run(["git", "-C", str(at), "checkout", "--quiet", "FETCH_HEAD"])
        (at / ".git" / "FETCHED").write_text(ref)

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
