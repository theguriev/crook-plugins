# Adding a plugin

Writing one first: [crook-plugin-template](https://github.com/theguriev/crook-plugin-template) is
five exports, a manifest that asks for nothing and one shape to change, and
`crook --dev-plugin .` runs it out of `target/` while you work on it.

One plugin per pull request, one directory:

```
plugins/<owner>.<name>/plugin.toml
```

```toml
schema = 1

id = "you/thing"
repository = "https://github.com/you/crook-thing"
license = "MIT"

# Newest last. A ref is a commit rather than a tag: what is built is what is
# indexed, and a tag can be moved after it is reviewed.
[[release]]
ref = "0123456789abcdef0123456789abcdef01234567"
```

That is the whole file, and everything not in it is on purpose. The name, the description, the
version, the ABI and the list of what the plugin asks to be allowed to do are read out of the
**artifact** by Crook's own reader — so there is nothing here to keep in step with the plugin,
and nothing a registry entry can say about a plugin that the plugin does not say about itself.

Every one of those is checked before anything is cloned or built, so a pull request that gets
one wrong fails with a line naming it rather than with something about git.

## What has to be true

- **`id` is `owner/name`**, lowercase letters, digits, `-` and `_`. The owner is you: the
  directory is yours under `CODEOWNERS`, and a change to it needs your approval.
- **The directory is the id with the slash turned into a dot**, which is what the same plugin's
  directory is called on disk once it is installed.
- **The repository builds `wasm32-unknown-unknown` with `cargo build --release --locked`**, and
  produces exactly one `.wasm`. Not `wasm32-wasip1`: the sandbox links seven host functions and
  refuses a module that imports anything else.
- **No binaries in the pull request.** Every artifact is built by CI from the commit you named.
- **The licence is named**, and it is in the repository the entry points at.
- **The `ref` is a full 40-character commit**, and the `repository` is
  `https://github.com/<owner>/<name>`. Both of those become git's arguments, and git takes more
  kinds of thing there than anybody means: `ext::` runs a command, a path clones a directory on
  the runner, and a tag can be moved after somebody has reviewed what it pointed at.

## What CI does with it

Checks out the commit, builds it with the toolchain that repository pins, runs
`crook-plugin-info` on what came out, and refuses the lot if the module's id is not the id in
the file. A pull request builds only the entries it changed; `main` builds everything.

**Building and publishing are two jobs, and the split is the point.** Building runs strangers'
code — every plugin brings its own `build.rs` and its own proc macros, and both execute on the
runner — so that job has a read-only token, no checkout credentials left behind for one of them
to find, and nothing to publish with. What it produces is an artifact. The second job downloads
that artifact and uploads it, and runs no code it did not bring. It writes the `.wasm` files
first and `index.json` last, so a publish that stops half way leaves a list that names only
files that are already there.

## What gets a human look, every time

A plugin asking for **`TypeCommands`** or **`RunCommands`** — what it types, your shell runs, as
you. Both are lists of exact commands rather than a flag, so the review is short and concrete:
is `git switch {}` what this plugin needs, and is the plugin what it says it is.

`ReadFiles` and `Network` are read the same way, one path and one host at a time. A plugin that
asks for a directory of somebody's home with `/**` should say in its description why.

Nothing here is a judgement about whether a plugin is *good*. What the registry is answerable
for is that the bytes came from the source in the entry, and that what a plugin asks for is
legible before anybody installs it.

## Updating a plugin

Add a `[[release]]` with the new commit. Leave the old ones: every version stays in the index,
which is what lets a Crook on an older plugin ABI go on installing the last build that speaks
it.

## A version that is published is not rebuilt

Adding a `[[release]]` builds a new artifact; changing the `ref` of one that is already in the
index rebuilds an old one, and **the bytes will not be the same**. That is not a bug in this
registry, it is what a Rust build is: two runs of the same commit on different toolchain patch
versions produce different modules. Measured here on the day it was set up — four of six
artifacts hashed differently between a laptop and CI, from the same commits.

What follows is a rule rather than a warning, and CI keeps it rather than trusting anybody to:
every run reads the published index first and **carries over every version already in it**,
matched by the commit its entry records. Only a release the index has never seen is built. So a
push that changes nothing but a README rebuilds nothing, and the `ref` of a release already in
the index can be edited without effect — the artifact and the hash that went out stay out.

Which means a change to what a version *is* is a new version, and adding one is adding a
`[[release]]`.

What the hash is worth, said precisely: the bytes that arrive are the bytes CI built from the
commit in this file. It is not a claim that anybody else rebuilding that commit gets the same
bytes, and nothing here has ever said it was.

## Withdrawing a version

```toml
[[release]]
ref = "0123456789abcdef0123456789abcdef01234567"
yanked = "it read the wrong file and asked for the whole home directory"
```

The version stops being offered, the one under it takes its place, and anybody already running
it is told that sentence. The artifact stays where it is: a URL that stops resolving is a
mystery, and a sentence is not.

## How long a review takes

Three weeks, and after that a pull request nobody has answered is closed rather than left open
looking like it might still land. Reopen it whenever.
