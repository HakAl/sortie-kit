# A complete example

Everything from an empty directory to a running shift, to copy from top to
bottom into your own terminal. It makes a disposable repository and a virtual
environment under `~/dev/sortie-try`, and starts a 30 minute shift with a
Claude driver and a Codex builder; the reviewer is picked, and with those two
installed it is `claude`, of another family than the Codex builder. Thirty minutes and not
fewer: the last third of a window shorter than an hour is wind-down, in which
no new builder turn starts, so this leaves twenty minutes of work, which is
room for the task and one repair round if the plan or the review sends it
back. A 20 minute shift has run out during such a round with the task not
accepted. Set `SORTIE_KIT` to your
checkout of this repository first. The model IDs are the ones this pairing
was run with; use the IDs your own `claude` and `codex` accept. If you
already have the signing key and the trust file, leave out the `ssh-keygen`
and `sortie trust init` lines: neither overwrites what is there, and `trust
init` says so and stops.
The two `git config` lines give the disposable repository an example-only
identity for its commits; they touch no other repository and not your own
Git configuration.

```sh
SORTIE_KIT="$HOME/dev/sortie-kit"

mkdir -p "$HOME/dev/sortie-try"
cd "$HOME/dev/sortie-try"
python3 -m venv venv
venv/bin/python -m pip install "$SORTIE_KIT"

git init repo
git -C repo config user.name "Sortie Try"
git -C repo config user.email "sortie-try@example.invalid"
printf 'print("hello")\n' > repo/hello.py
git -C repo add hello.py
git -C repo commit -m "Start"

mkdir -p "$HOME/.config/sortie-kit"
chmod 700 "$HOME/.config/sortie-kit"
ssh-keygen -t ed25519 -C sortie-kit -f "$HOME/.config/sortie-kit/signing-key"
venv/bin/sortie trust init

printf '%s\n' 'Add a --version option to hello.py that prints hello 0.1.0 and exits 0.' \
  'Add a standard-library unittest for it. Make one local commit.' > direction.md

venv/bin/sortie start --repo repo --direction direction.md --for 30m \
  --trust "$HOME/.config/sortie-kit/trust.json" --key "$HOME/.config/sortie-kit/signing-key" \
  --agent driver=claude:claude-sonnet-5-5 --agent builder=codex:gpt-6.1-sol
```

Keep the repository under a normal project directory such as `~/dev`, not
under a temporary one; the note on [your hooks](machine.md#your-hooks) says
why. The guided form in [your first shift](../README.md#your-first-shift)
asks for the same choices one at a time.
