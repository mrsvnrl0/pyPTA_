# Reproducible source baseline

The source release contains the current allowlisted working-tree files, an embedded
`SOURCE_MANIFEST.json`, an identical manifest beside the archive, and a `.sha256`
file covering the archive and manifest. It does not depend on a commit or the Git
index. Creating it does not stage files, commit changes, restart the dashboard,
read live settings or copy trading records.

## Create and verify

From the project root, using Python 3.12:

```powershell
.\.venv\Scripts\python.exe -B tools\source_release.py --label baseline
Get-FileHash release\source\pyPTA-Source-baseline.zip -Algorithm SHA256
Get-Content release\source\pyPTA-Source-baseline.sha256
```

Outputs go to `release/source/` by default. `--output-dir` and `--label` select a
different destination and name. Repeating the command replaces those three
generated files. The label is part of the manifest identity. With identical input
bytes and label, the ZIP and manifest are byte-for-byte identical: names are
sorted, member timestamps and permissions are fixed, and members are stored
without compression. Source timestamps, absolute paths and wall-clock time are
excluded. SHA256 detects changes; it is not a publisher signature.

`tools/source_release.py` is the authoritative allowlist. It includes application
Python, templates/static files, the Parente model and notice, launchers, release
and candidate experiment tools, all root `test_*.py` / `test_*.js` files, the two
public-market test fixtures, selected documentation, Windows packaging inputs
and dependency locks. Required files must exist, and source symlinks/junctions
are rejected. Versioned NN candidate bundles are added only when their manifest
identity, referenced ONNX hashes, report hash, feature/target contract and
ensemble component references validate. The archive includes only the
manifest, referenced `.onnx` files and report for each accepted bundle.

Personal settings, databases, sidecars, credentials, environment files, logs,
recovered state, audit trade snapshots, copied backup directories, `.venv`, `.git`,
test output and old releases are excluded. A new file outside the allowlist does
not enter an archive automatically. Review changes to the allowlist and any
new source or test fixture before publishing. The source archive is intentionally
readable for maintenance; it is separate from an obfuscated Windows distribution.
Candidate training CSVs, checkpoints, prediction tables and credentials are not
archive inputs, even if they sit beside a model bundle. A malformed versioned
bundle stops release creation instead of silently shipping a partial model.

`.gitignore` prevents newly generated/local artifacts from appearing as new source
candidates. It does not remove already tracked or staged files. Existing staged
content is deliberately preserved; a later initial commit requires reviewing the
index separately from this source archive.

## Run an extracted source release

The verified source environment is CPython 3.12.14 on Windows x64. The runtime lock
records the dependency versions for a candidate-capable install. ONNX Runtime
1.30.0 and its pinned flatbuffers/protobuf dependencies were validated with the
candidate CPU exports in a separate training environment; a clean extracted
release still requires its full regression check. The original range-based
requirements remain available, and `requirements-candidate-runtime.txt` adds
ONNX inference to the Parente-only `requirements-neural.txt` for environments
that do not use the full lock.
Package indexes and wheel files are external prerequisites, so pins alone do not
make dependency installation or an installer binary byte-reproducible.

In a fresh extracted directory:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-runtime-lock.txt
Copy-Item adaptive_crypto_settings.example.json adaptive_crypto_settings.json
.\.venv\Scripts\python.exe -B adaptive_crypto_dashboard.py --host 127.0.0.1 --port 5000
```

Copy the example only in a new profile, never over an existing settings file. It
contains public `DEFAULT_ASSETS` and `Rules(strategy_model="neural_network")`
defaults, with no local model path or service credentials. No personal settings or
records are included. The application creates its own runtime state on first run.

## Required regression checks

Run these from the extracted source root before accepting a release:

```powershell
.\.venv\Scripts\python.exe -B -m unittest discover -q
node --test test_gex_ui.js test_nn_charts_ui.js
node --test test_live_refresh_ui.js
```

The Python suite includes source-byte determinism, checksum verification, private
artifact exclusion, candidate-bundle hash/reference checks, missing-input
failures and clean Windows build preparation.
The formula consistency test reads the checked-in example, so it never needs the
operator's live settings. Both checked-in market fixtures and the neural model
are needed for the full suite. The JavaScript suites require Node.js with its
test runner. The live-refresh suite also requires Playwright and Chromium.

If Playwright is not already provided by the verification environment, install
it in a disposable, ignored verification directory and expose that package to
Node. Record the Playwright and browser versions with the release test results:

```powershell
npm install --prefix .qa playwright
$env:NODE_PATH = (Resolve-Path .qa\node_modules).Path
npx --prefix .qa playwright install chromium
node --test test_live_refresh_ui.js
```

For an existing compatible Chrome/Chromium installation, set
`$env:PYPTA_BROWSER_PATH` to that executable before the browser test. This setup is
performed only when preparing a verification environment; creating the source ZIP
does not download packages or browsers. A skipped browser test is not a completed
browser validation. Retain actual command results separately from source files.

## Independent Windows build preparation

The root `build_windows_installers.ps1` is a legacy wrapper for a separate sibling
`pyACCS/windows_installer_work` workspace. It is retained for existing users, but
is not the standalone-source build entry point. The included spec and NSIS files
expect a staged directory with packaging files directly under `packaging/` and a
Windows launcher installed into the Python package.

Prepare that layout using only archive inputs:

```powershell
.\.venv\Scripts\python.exe -B tools\prepare_windows_build.py --destination .release-work\windows
```

The destination must not exist; the tool never deletes or reuses another build
tree. It copies allowlisted sources, installs the desktop launcher and inference
self-test fixture into their expected staged paths, flattens the packaging files,
creates installer README files, copies example defaults as staged settings, and
creates the separate `release/pyPTA-Source-1.0.1.zip` required by the existing
installer script. It never copies live settings or trading state.

The current packaging inputs retain version 1.0.1. A future release should update
the NSIS/spec version metadata and source archive name together. This preparation
does not build installers and does not establish that an old installer contains
the current source fixes.

The Windows spec selects model files through the same checked allowlist as the
source archive. It no longer copies the entire `adaptive_crypto/models` directory.
If candidate bundles are present, the frozen build also requires the validated,
pinned ONNX Runtime CPU dependency; the spec collects its binary payload. For a
release containing candidates, run frozen self-tests and a separate inference
check for each bundled architecture and supported asset on the target Windows
machine. Source hashes and ONNX tensor validation serve different purposes;
neither alone demonstrates that a frozen executable can run every candidate.

For a future build on a dedicated Windows x64 machine, install pinned tooling
from `packaging/windows/requirements-lock.txt` into a separate build environment.
NSIS and a suitable Python/Tk runtime are additional prerequisites. From the
prepared directory, an Open payload can be built with:

```powershell
$env:PYPTA_BUILD_FLAVOR = 'Open'
python -m PyInstaller --noconfirm --clean packaging\pyPTA.spec
```

For an Obfuscated payload, run the pinned PyArmor tool over `adaptive_crypto` and
`windows_launcher.py` into `obfuscated/`, then use flavor `Obfuscated` with the
same spec. Retain its runtime and license notices.

There is an existing packaging limitation to resolve before release: the frozen
self-test unconditionally imports the optional `openai` SDK, but that SDK is not
present in the recorded source runtime or pinned installer requirements. The
ordinary NN application does not need it. Choose and validate a pinned SDK build,
or deliberately revise that optional packaging check, before claiming the frozen
self-test passes. The installer toolchain has not been reinstalled or rebuilt as
part of this source baseline.

Before shipping any newly built Windows binary, run its `--self-test --report`
check, verify the report passed (including model inference, Tk, pages, empty data,
credential handling and complete backups), and perform install, launch, upgrade
and uninstall smoke tests with disposable profiles on a clean Windows machine.
For the protected edition, verify readable application modules are absent. Run
NSIS from the staged `packaging/` directory with `/DFLAVOR=Open /DOPEN_SOURCE` or
`/DFLAVOR=Obfuscated` only after these checks. Archive build logs, tool versions,
source manifest and final binary hashes together. Installer reproducibility and
Store/signing validation are separate release work.
