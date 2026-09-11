"""What `tools/index.py` does with a release, without a toolchain in the room.

    python3 -m unittest discover -s tools

A real run clones a repository and runs `cargo build` for `wasm32` in it,
which is forty minutes nobody wants to spend to learn that a dict was rebound
to a path. So `build()` is stood in for by a function that writes a file
where the module would be, and the reader is a script that answers with
whatever that file holds — a test says what a commit "builds" by writing the
line the reader should print. Everything after that point is the real thing:
the version row, the plugin row, the carry-over, and `check()`.
"""

import argparse
import contextlib
import hashlib
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import index

# The stand-in for `crook-plugin-info`, invoked exactly as the real one is —
# one path, one line on stdout — so that `describe()` runs unchanged. A module
# whose first byte is `!` is one the reader refuses, with the rest as the
# sentence: how a bad picture reaches this script.
READER = """\
#!/usr/bin/env python3
import sys
held = open(sys.argv[1]).read()
if held.startswith("!"):
    sys.exit(held[1:].strip())
print(held, end="")
"""

# Forty hex characters each, which is the shape the script insists on before
# it builds anything.
OLDER = "a" * 40
NEWER = "b" * 40

# What the reader prints for a PNG's first eight bytes: enough to be base64,
# and short enough to read.
ICON = "iVBORw0KGgo="


def line(version, icon=ICON, previews=({"width": 640, "height": 128},), plugin_id="you/hello"):
    """One line the reader would print for a module at `version`.

    `icon=None` and `previews=None` leave the keys out altogether, which is
    what a reader from before pictures prints.
    """
    described = {
        "abi": 8,
        "id": plugin_id,
        "name": f"Hello {version}",
        "description": "Says hello.",
        "version": version,
        "capabilities": [],
        "asks": [],
    }
    if icon is not None:
        described["icon"] = icon
    if previews is not None:
        described["previews"] = list(previews)
    return json.dumps(described)


def builds(answers):
    """A `build()` that writes what the reader should say, keyed by commit."""

    def build(plugin, release, arguments):
        at = arguments.work / release["ref"]
        at.mkdir(parents=True, exist_ok=True)
        module = at / "plugin.wasm"
        module.write_text(answers[release["ref"]])
        return module

    return build


def never_builds(plugin, release, arguments):
    """A `build()` for a run that must carry everything over."""
    raise AssertionError(f"built {release['ref']}, which is published")


def entry(releases, plugin_id="you/hello"):
    """A parsed `plugin.toml` naming `releases`, newest last."""
    return {
        "schema": 1,
        "id": plugin_id,
        "repository": f"https://github.com/{plugin_id.split('/')[0]}/crook-hello",
        "license": "MIT",
        "release": list(releases),
    }


def row(**changes):
    """One version row the way `one()` writes it, with `changes` applied."""
    version = {
        "version": "0.1.0",
        "abi": 8,
        "url": "https://example.test/index/you.hello-0.1.0.wasm",
        "sha256": "0" * 64,
        "bytes": 1,
        "capabilities": [],
        "asks": [],
        "yanked": None,
        "ref": OLDER,
        "previews": [],
    }
    version.update(changes)
    return version


def listed(icon=None, versions=None, **changes):
    """One plugin the way `one()` returns it, with `changes` applied."""
    plugin = {
        "id": "you/hello",
        "name": "Hello",
        "description": "Says hello.",
        "icon": icon,
        "repository": "https://github.com/you/crook-hello",
        "license": "MIT",
        "versions": [row()] if versions is None else versions,
    }
    plugin.update(changes)
    return plugin


def an_index(*plugins):
    """An index of `plugins`, or of one plugin without pictures."""
    return {"schema": index.SCHEMA, "plugins": list(plugins) or [listed()]}


class Scratch(unittest.TestCase):
    """A directory to build in, a reader in it, and the arguments to `one()`."""

    def setUp(self):
        self.scratch = Path(tempfile.mkdtemp(prefix="index-test-"))
        self.addCleanup(shutil.rmtree, self.scratch, ignore_errors=True)
        reader = self.scratch / "reader.py"
        reader.write_text(READER)
        reader.chmod(0o755)
        self.artifacts = self.scratch / "artifacts"
        self.artifacts.mkdir()
        self.arguments = argparse.Namespace(
            reader=str(reader),
            base_url="https://example.test/index",
            work=self.scratch / "work",
            local=None,
        )
        # `one()` reads only the directory's name from the entry, but `main()`
        # reads the file, so both are real.
        self.entry = self.scratch / "plugins" / "you.hello" / "plugin.toml"
        self.entry.parent.mkdir(parents=True)

    def one(self, plugin, kept=None, published=None, published_versions=None):
        """`index.one()` for `plugin`, with nothing published unless given.

        Its progress lines go nowhere: they are for a person watching CI, and
        here they would only interleave with the runner's own.
        """
        with contextlib.redirect_stdout(io.StringIO()):
            return index.one(plugin, self.entry, self.arguments, self.artifacts,
                             kept or {}, published or {}, published_versions or set())


class ANewRelease(Scratch):
    """A `[[release]]` the index has never seen, built here."""

    def test_is_built_described_and_listed_with_its_pictures(self):
        # The regression: the first release not carried over used to raise
        # `TypeError` on `built[...]`, because `built` had become the module.
        with mock.patch.object(index, "build", builds({OLDER: line("0.1.0")})):
            plugin = self.one(entry([{"ref": OLDER}]))

        self.assertEqual(plugin["id"], "you/hello")
        self.assertEqual(plugin["name"], "Hello 0.1.0")
        self.assertEqual(plugin["icon"], ICON)
        [version] = plugin["versions"]
        self.assertEqual(version["version"], "0.1.0")
        self.assertEqual(version["previews"], [{"width": 640, "height": 128}])
        self.assertEqual(version["ref"], OLDER)
        self.assertEqual(version["url"], "https://example.test/index/you.hello-0.1.0.wasm")

        # The artifact is the module, copied, and the hash is of that copy.
        artifact = self.artifacts / "you.hello-0.1.0.wasm"
        self.assertTrue(artifact.exists())
        self.assertEqual(version["bytes"], len(artifact.read_bytes()))
        self.assertEqual(version["sha256"], hashlib.sha256(artifact.read_bytes()).hexdigest())

    def test_names_the_plugin_after_the_newest_version_built(self):
        # Releases are listed newest last, but the name, the description and
        # the icon follow the newest *version*, whatever the file order. Two
        # builds, because `max(built, …)` used to iterate a path.
        answers = {OLDER: line("0.10.0", icon="bmV3ZXI="), NEWER: line("0.9.0", icon="b2xkZXI=")}
        with mock.patch.object(index, "build", builds(answers)):
            plugin = self.one(entry([{"ref": OLDER}, {"ref": NEWER}]))

        self.assertEqual(plugin["name"], "Hello 0.10.0")
        self.assertEqual(plugin["icon"], "bmV3ZXI=")
        self.assertEqual([version["version"] for version in plugin["versions"]],
                         ["0.10.0", "0.9.0"])

    def test_described_by_a_reader_without_pictures_still_indexes(self):
        # A reader from before pictures prints neither key; the index it
        # builds is one the terminal reads, with nothing to draw.
        with mock.patch.object(index, "build", builds({OLDER: line("0.1.0", None, None)})):
            plugin = self.one(entry([{"ref": OLDER}]))

        self.assertIsNone(plugin["icon"])
        self.assertEqual(plugin["versions"][0]["previews"], [])
        index.check({"schema": index.SCHEMA, "plugins": [plugin]})

    def test_the_reader_refuses_is_a_build_that_fails_with_its_sentence(self):
        # The picture rules are the reader's, and its refusal is the whole of
        # what the registry has to say about one.
        refused = "!crook.icon is 300×200 and an icon is square"
        with mock.patch.object(index, "build", builds({OLDER: refused})):
            with self.assertRaisesRegex(index.Failed, "could not be read: crook.icon is 300×200"):
                self.one(entry([{"ref": OLDER}]))


class ACarriedPlugin(Scratch):
    """A plugin whose every version the published index already holds."""

    def test_takes_its_icon_from_the_published_index(self):
        published = listed(icon=ICON, versions=[row(previews=[{"width": 560, "height": 1010}])])
        kept = {("you/hello", OLDER): published["versions"][0]}

        with mock.patch.object(index, "build", never_builds):
            plugin = self.one(entry([{"ref": OLDER}]), kept, {"you/hello": published},
                              {("you/hello", "0.1.0")})

        self.assertEqual(plugin["icon"], ICON)
        self.assertEqual(plugin["name"], "Hello")
        [version] = plugin["versions"]
        # The row is what was published; only the URL and `yanked` are this run's.
        self.assertEqual(version["previews"], [{"width": 560, "height": 1010}])
        self.assertEqual(version["sha256"], "0" * 64)
        self.assertEqual(version["url"], "https://example.test/index/you.hello-0.1.0.wasm")

    def test_published_before_pictures_has_no_icon_and_the_row_no_previews(self):
        # An index from before pictures names neither, and a carried version
        # is not rebuilt to find out: the keys stay absent until the plugin's
        # next release, and `check()` reads absent as none.
        published = listed(versions=[row()])
        del published["icon"]
        del published["versions"][0]["previews"]
        kept = {("you/hello", OLDER): published["versions"][0]}

        with mock.patch.object(index, "build", never_builds):
            plugin = self.one(entry([{"ref": OLDER}]), kept, {"you/hello": published},
                              {("you/hello", "0.1.0")})

        self.assertIsNone(plugin["icon"])
        self.assertNotIn("previews", plugin["versions"][0])
        index.check({"schema": index.SCHEMA, "plugins": [plugin]})

    def test_with_a_new_release_takes_its_icon_from_what_was_built(self):
        # One carried, one built: the plugin's face is the newest version's,
        # which is the one built, and the carried row keeps its own sizes.
        published = listed(icon="b2xkZXI=", versions=[row()])
        kept = {("you/hello", OLDER): published["versions"][0]}

        with mock.patch.object(index, "build", builds({NEWER: line("0.2.0", icon="bmV3ZXI=")})):
            plugin = self.one(entry([{"ref": OLDER}, {"ref": NEWER}]), kept,
                              {"you/hello": published}, {("you/hello", "0.1.0")})

        self.assertEqual(plugin["icon"], "bmV3ZXI=")
        self.assertEqual([version["version"] for version in plugin["versions"]],
                         ["0.1.0", "0.2.0"])


class Check(unittest.TestCase):
    """What `check()` lets through, and what it stops."""

    def refuses(self, index_, why):
        with self.assertRaisesRegex(index.Failed, why):
            index.check(index_)

    def test_accepts_a_plugin_without_pictures(self):
        index.check(an_index(listed(icon=None, versions=[row(previews=[])])))

    def test_accepts_a_plugin_published_before_pictures(self):
        plugin = listed()
        del plugin["icon"]
        del plugin["versions"][0]["previews"]
        index.check(an_index(plugin))

    def test_accepts_an_icon_and_previews(self):
        previews = [{"width": 640, "height": 128}, {"width": 2048, "height": 1}]
        index.check(an_index(listed(icon=ICON, versions=[row(previews=previews)])))

    def test_accepts_an_icon_at_the_cap(self):
        index.check(an_index(listed(icon="A" * index.MAX_ICON_CHARS)))

    def test_accepts_six_previews(self):
        previews = [{"width": 1, "height": 1}] * index.MAX_PREVIEWS
        index.check(an_index(listed(versions=[row(previews=previews)])))

    def test_refuses_an_icon_that_is_not_base64(self):
        self.refuses(an_index(listed(icon="not base64!")), "not base64")

    def test_refuses_an_icon_that_is_not_text(self):
        self.refuses(an_index(listed(icon=["iVBOR"])), "not base64")

    def test_refuses_an_icon_past_the_cap(self):
        # One character over: the cap is 32 KiB of PNG, and the check is on
        # the characters that carry it.
        self.assertEqual(index.MAX_ICON_CHARS, 43_692)
        self.refuses(an_index(listed(icon="A" * (index.MAX_ICON_CHARS + 1))),
                     "43693 characters")

    def test_refuses_seven_previews(self):
        previews = [{"width": 1, "height": 1}] * 7
        self.refuses(an_index(listed(versions=[row(previews=previews)])), "names 7 previews")

    def test_refuses_previews_that_are_not_a_list(self):
        self.refuses(an_index(listed(versions=[row(previews={"width": 1})])), "for previews")

    def test_refuses_a_preview_that_is_not_a_size(self):
        self.refuses(an_index(listed(versions=[row(previews=[640])])), "for a preview")

    def test_refuses_a_width_of_zero(self):
        previews = [{"width": 0, "height": 128}]
        self.refuses(an_index(listed(versions=[row(previews=previews)])), "width of 0")

    def test_refuses_a_side_past_the_edge(self):
        previews = [{"width": 640, "height": 2049}]
        self.refuses(an_index(listed(versions=[row(previews=previews)])), "height of 2049")

    def test_refuses_a_side_that_is_not_a_whole_number(self):
        for edge in (True, 12.5, "640", None):
            with self.subTest(edge=edge):
                previews = [{"width": edge, "height": 128}]
                self.refuses(an_index(listed(versions=[row(previews=previews)])), "width of")


class ARun(Scratch):
    """`main()` end to end: the arguments, the carry-over, and the file."""

    def test_writes_an_index_with_pictures_and_carries_the_rest(self):
        # Two plugins: one entirely published, one with a release to build.
        # What comes out is the file CI uploads, so this reads it back the way
        # the terminal would.
        self.entry.write_text(f'schema = 1\nid = "you/hello"\n'
                              f'repository = "https://github.com/you/crook-hello"\n'
                              f'license = "MIT"\n\n[[release]]\nref = "{OLDER}"\n')
        other = self.scratch / "plugins" / "you.other" / "plugin.toml"
        other.parent.mkdir()
        other.write_text(f'schema = 1\nid = "you/other"\n'
                         f'repository = "https://github.com/you/crook-other"\n'
                         f'license = "MIT"\n\n[[release]]\nref = "{NEWER}"\n')

        published = self.scratch / "published.json"
        published.write_text(json.dumps(an_index(listed(icon=ICON))))

        out = self.scratch / "out"
        argv = ["index.py", "--plugins", str(self.entry.parent.parent), "--out", str(out),
                "--work", str(self.arguments.work), "--keep", str(published),
                "--reader", self.arguments.reader, "--base-url", self.arguments.base_url]
        answers = {NEWER: line("0.3.0", icon="b3RoZXI=", plugin_id="you/other")}
        with mock.patch.object(sys, "argv", argv), \
                mock.patch.object(index, "build", builds(answers)), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(index.main(), 0)

        written = json.loads((out / "index.json").read_text())
        self.assertEqual(written["schema"], index.SCHEMA)
        hello, other = written["plugins"]
        self.assertEqual((hello["id"], hello["icon"]), ("you/hello", ICON))
        self.assertEqual((other["id"], other["icon"]), ("you/other", "b3RoZXI="))
        self.assertEqual(other["versions"][0]["previews"], [{"width": 640, "height": 128}])
        # Only the built module is an artifact; the carried one is up already.
        self.assertEqual([artifact.name for artifact in sorted((out / "artifacts").iterdir())],
                         ["you.other-0.3.0.wasm"])


if __name__ == "__main__":
    unittest.main()
