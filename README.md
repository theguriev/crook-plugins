# The Crook plugin registry

Every plugin [Crook](https://github.com/theguriev/crook) can offer, built from source by CI and
published as one static file.

```
https://github.com/theguriev/crook-plugins/releases/download/index/index.json
```

That URL is the whole of the store. There is **no server**: the terminal fetches one JSON file
and, when somebody presses Install, one `.wasm`. It sends no account, no machine id, no version
and no list of what is installed — a `GET` and an `If-None-Match` carrying a tag this registry
itself wrote. Offline, Crook shows the copy it already has.

## What is in it

| Plugin | What it does | What it asks for |
| --- | --- | --- |
| [pirate](https://github.com/theguriev/crook-pirate) | How much of the Claude Code session budget is spent, and when it resets | One host, two paths |
| [chips](https://github.com/theguriev/crook-chips) | Where the pane is, what branch it is on, and what a chord would do | The working directory, and two commands it may type |
| [markdown](https://github.com/theguriev/crook-markdown) | A command and its output, as Markdown ready to paste | The block its menu is open on, and the clipboard |
| [dziling](https://github.com/theguriev/crook-dziling) | Rings a short sound when something finishes | To hear commands and bells, and to play a sound |
| [emoji](https://github.com/theguriev/crook-emoji) | An emoji at the head of every tab, instead of the status dot | Nothing at all |
| [worktree](https://github.com/theguriev/crook-worktree) | A mark on every tab whose directory is a git worktree | The working directory |

## Installing one

From inside Crook: the **Plugins** section in the sidebar. Nothing is fetched until you open it.

By hand, which is the same thing without the list:

```sh
crook --install-plugin ~/Downloads/plugin.wasm
crook --plugins                          # what is installed, at which version, from where
crook --uninstall-plugin theguriev/emoji # and its permissions with it
```

A plugin that has just been installed may do **nothing**. What it asked for is on its card, in
the sentence a permission dialog says, and until somebody answers it reaches none of it.

## What the registry promises

- **Every artifact is built here, from the commit in the plugin's `plugin.toml`.** A pull
  request contains no binaries; if it did, a reviewer would be approving bytes nobody read.
  Building and publishing are separate jobs: the one that runs a stranger's `build.rs` has a
  read-only token and nothing to publish with, and the one that publishes runs no plugin code.
- **The description is the module's own.** The id, the version, the ABI and the capability list
  in the index are read out of the built artifact by `crook-plugin-info`, which is Crook's own
  reader — the same code the terminal decides with. A `plugin.toml` whose id disagrees with the
  module's fails the build.
- **The index is checked against what the terminal reads.** A row whose id the host cannot
  parse, a hash it cannot compare, a URL it will not fetch: each of those is a plugin quietly
  missing from somebody's list, so the build refuses rather than publishing it.
- **The hash is what was built.** `sha256` catches a truncated download, a moved URL and a stale
  mirror. It is not a signature and nothing here calls it verification: the same run builds the
  artifact and writes the hash.
- **A version is offered to the Crooks that can run it.** Each artifact carries the plugin ABI
  it speaks, a host loads one ABI and refuses the rest by name, and every version stays in the
  index — so a Crook on ABI 8 goes on being offered the last build for 8 after everything else
  has moved on.
- **Withdrawing is a reason, not a deletion.** `yanked = "why"` on a release stops it being
  offered and tells anybody already running it what happened. The artifact stays where it is:
  a URL that stops resolving is a mystery, and a sentence is not.

## Adding a plugin

Write one — [crook-plugin-template](https://github.com/theguriev/crook-plugin-template) is the
shortest way in, [`crook_plugin_api`](https://crates.io/crates/crook_plugin_api) is the
vocabulary, and the plugins above are worked examples — then open a pull request adding one
directory here. [`CONTRIBUTING.md`](CONTRIBUTING.md) is the whole of the process.

## Building the index yourself

```sh
cargo install crook_wasm --version '^0.8'  # crook-plugin-info, the reader for ABI 8
tools/index.py --out build                 # clone, build, read, hash, write index.json
```

One release, rewritten in place by CI on `main`, and the tag never moves: that URL is what every
Crook reads, so it is the one thing here that has to be stable forever. A release rather than a
published site because a registry should be a repository with a workflow in it and nothing else
configured — somebody forking this to run their own list changes one line in their Crook.

`--from <directory>` builds from checkouts you already have instead of cloning, and `--keep
<index.json>` carries over every version an index already published rather than building it
again — which is what CI does, and why an ordinary push rebuilds nothing.

Trying a `plugin.toml` before pushing it is `--only <id>`, which builds that one entry and
deliberately writes no index.
