---
name: publish-release
description: >-
  Cut and publish a new version release (patch or minor) of the batch
  simulation runner to GitHub. Handles the full sequence: pick the
  version, tag it, rebuild the PyInstaller binaries, write release notes, and
  create the GitHub release with all the right assets. Critically, it covers
  two easy-to-miss gotchas specific to this repo — the dist/ exes are
  gitignored and go stale, and the README embeds screenshots from the LATEST
  release, so every release must re-carry those screenshots or the README
  images break.
---

# Publish a GitHub release

This repo ships two PyInstaller onefile Windows binaries (`batch_simu_tui.exe`,
`batch_simu_cli.exe`) attached to a GitHub release. The README also pulls its
TUI screenshots from the **latest** release. A release is therefore not just a
git tag — it must carry fresh binaries and the screenshots, or things break.

Repo: `https://github.com/Lazy-Beee/simulation-batch-runner`
Tags are **lightweight** (`git tag vX.Y.Z`, no `-a`), matching every prior tag.

## Two gotchas that cause silent breakage

1. **Stale binaries.** `dist/`, `build/`, and `*.spec` are gitignored (see
   `.gitignore`). The exes in `dist/` are whatever was last built locally —
   they do **not** track the source and are usually older than the commits you
   are about to release. Always rebuild before attaching, then confirm the
   timestamps are fresh.
2. **README depends on the latest release's screenshots.** `README.md` embeds
   three images via `releases/latest/download/tui-queue.png`,
   `tui-running.png`, and `tui-case-log.png`. The `latest/download/` path
   always resolves against whatever release is newest. The moment your new
   release becomes "Latest", those URLs point at it — and 404 if it doesn't
   carry the PNGs. So every release must include the screenshots; whether to
   reuse the previous release's or capture fresh ones is the user's call (see
   step 8).

## Workflow

### 1. Confirm a clean, pushed working tree

```powershell
git status              # must be clean (config.json is gitignored — ignore it)
git push origin main    # the release commit must exist on the remote first
```

If there are uncommitted release-worthy changes, commit and push them before
tagging — the tag should point at a commit that is already on the remote.

### 2. Find the last version and review what changed

```powershell
git describe --tags --abbrev=0          # last tag reachable from HEAD, e.g. v1.2.0
git tag --sort=-v:refname               # full version-sorted tag list
git log --no-merges --format="%h  %ad  %s" --date=short <last>..HEAD
git diff --stat <last>..HEAD
```

### 3. Decide the new version (confirm with the user)

Semver against the changes since the last tag:
- **patch** (`Z+1`) — bug fixes / robustness only.
- **minor** (`Y+1`, `Z=0`) — any new user-facing feature or config key.

State your recommendation and the reasoning, but let the user pick the number —
they may deliberately choose patch for a small feature, etc.

### 4. Tag and push

```powershell
git tag vX.Y.Z
git push origin vX.Y.Z
git describe --tags     # should now print vX.Y.Z
```

### 5. Rebuild the binaries (do not trust dist/)

`build.bat` wipes `build/`, `dist/`, and the `.spec` files, then runs
PyInstaller twice. It needs `pyinstaller` on PATH (`pyinstaller --version`).
From PowerShell, invoke it with a full path (cmd won't find a bare `build.bat`):

```powershell
$bat = Join-Path (Get-Location) 'build.bat'
cmd /c "`"$bat`""        # takes a few minutes; the TUI exe is ~35 MB
```

Verify the result — fresh timestamps and a working CLI exe:

```powershell
Get-ChildItem dist -File | Format-Table Name, Length, LastWriteTime -Auto
.\dist\batch_simu_cli.exe --help      # should print argparse usage
```

(`--help` piped through `Select-Object -First N` reports a non-zero exit from
the early-closed pipe — that's a pipeline artifact, not an exe failure.)

Because `dist/` is gitignored, none of this touches git — no commit needed.

### 6. Write release notes

Match the house style: a `## What's new` heading with **bold section** labels
and terse bullets. Look at the previous release for the exact tone:

```powershell
gh release view <last>
```

Group the notes by theme (new feature first, then fixes/robustness, then a
`Config` note for any new keys). Write them to a temp file so multi-line
markdown survives PowerShell quoting:

- Write notes to `$env:TEMP\vX.Y.Z-notes.md` (use the Write tool, not a
  here-string — `-m @'...'@` after a flag does not parse as a here-string and
  leaks the `@` delimiters into the message).

### 7. Create the release with binaries + config

The tag already exists, so `gh release create` reuses it. Attach the two exes
and the example config (the README tells users to grab these from the latest
release). The newest tag becomes "Latest" automatically.

```powershell
gh release create vX.Y.Z --title "vX.Y.Z" `
  --notes-file "$env:TEMP\vX.Y.Z-notes.md" `
  "dist\batch_simu_cli.exe" "dist\batch_simu_tui.exe" "config.example.json"
```

### 8. Re-attach the screenshots (gotcha #2)

The screenshots are not in the repo — they live only as release assets. Either
way, all three (`tui-queue.png`, `tui-running.png`, `tui-case-log.png`) must end
up on the new release or the README 404s.

**Ask the user which to do — do not decide silently.** A code diff does not
reliably reveal whether the TUI's *appearance* changed (a styling tweak can be
invisible in the diff, and the user may want fresh shots regardless), so this is
the user's call:

> "Screenshots for the release: reuse the previous release's, or do you want to
> provide updated ones?"

**If reusing** — copy them forward from the previous release:

```powershell
$tmp = Join-Path $env:TEMP 'release-shots'
New-Item -ItemType Directory -Force -Path $tmp | Out-Null
gh release download <last> --pattern "tui-*.png" --dir $tmp
gh release upload vX.Y.Z "$tmp\tui-queue.png" "$tmp\tui-running.png" "$tmp\tui-case-log.png"
```

**If updating** — have the user point you at the new PNGs (or capture them),
confirm they are named exactly `tui-queue.png` / `tui-running.png` /
`tui-case-log.png`, then `gh release upload vX.Y.Z` all three from wherever they
are. For any the user did not refresh, fall back to copying that one forward so
the set is always complete.

### 9. Verify

```powershell
gh release view vX.Y.Z --json assets --jq '.assets[].name'   # expect 2 exes, config, 3 png
gh release list --limit 3                                    # vX.Y.Z marked "Latest"
```

Confirm the README images actually resolve against the new latest release
(PS 5.1 needs `-UseBasicParsing` or it errors in non-interactive mode):

```powershell
foreach ($f in 'tui-queue.png','tui-running.png','tui-case-log.png') {
  $u = "https://github.com/Lazy-Beee/simulation-batch-runner/releases/latest/download/$f"
  $r = Invoke-WebRequest -Uri $u -Method Head -MaximumRedirection 5 -UseBasicParsing -ErrorAction Stop
  Write-Output ("{0,-18} -> HTTP {1}" -f $f, [int]$r.StatusCode)   # want HTTP 200
}
```

Clean up the temp notes/screenshot files when done.

## Final asset checklist

A correct release for this repo carries exactly:
- `batch_simu_tui.exe`, `batch_simu_cli.exe` — freshly rebuilt this release
- `config.example.json`
- `tui-queue.png`, `tui-running.png`, `tui-case-log.png`
