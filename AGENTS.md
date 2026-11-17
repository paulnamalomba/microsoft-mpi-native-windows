# AGENTS.md

Rules for people and coding agents working in this repository.

## Native Windows Fork Contract

This repository is the user's [microsoft-mpi-native-windows](https://github.com/paulnamalomba/microsoft-mpi-native-windows) fork of Microsoft MPI (MS-MPI). SESKAFEA integrates it as `dependencies/msmpi/`. All intentional MS-MPI source, build-system, packaging and test changes belong here. The suite owns orchestration and the exact fork gitlink. Publish fork changes before advancing that gitlink. Create a working branch before committing from a detached submodule checkout.

This is an independently maintained product fork. It starts from upstream `microsoft/Microsoft-MPI` master `f2d849ff483b36add9bc2d18c6d7bbc9ce38f4ad` (2024-03-06). The last upstream tag is `v10.1.1` (`c8daf40`); the source declares `10.1.12498.16` in `src/mpi.props`. Upstream is a read/fetch reference only:

- Do not add an upstream remote with a push URL.
- Do not push branches or tags there, and do not open pull requests, issues or comments there.
- Upstream tags are not republished here; this fork's own tags start with its first accepted build.
- GitHub records this repository as a fork, so tools may default to the upstream base. Every push and any requested pull request must explicitly target `paulnamalomba/microsoft-mpi-native-windows`. Create pull requests only when the user asks or the [agent workflow](#agent-workflow) below requires one.

### Purpose and scope

The fork provides the MPI runtime and SDK for SESKAFEA on native Windows x64.

- **Required:** fix the observed manager launch race. `gle_create_mpi_process()` resumes a rank (`src/mpi/smpd/smpd_launch_process.cpp`, `ResumeThread`) before the launch loop registers it with `smpd_add_process()`. A rank that reaches its PMI connect first is rejected by `smpd_find_process_by_id()` in `src/mpi/pmilib/SmpdMgrRpcSrv.cpp`, and the job aborts. Hosted evidence: SESKA backend runs 38937044789 and 38940307295.
- **Required:** build with the suite's pinned native toolchain: Visual Studio Build Tools, the Windows SDK, and Intel `ifx` for the Fortran interfaces. MinGW, MSYS2, Cygwin, WSL and Strawberry Perl are excluded from the build and runtime. Upstream's GFortran (mingw-w64) Fortran build and Perl-based generators therefore need native replacements.
- **Proposed:** SESKA ships the resulting `msmpi.dll`, `mpiexec.exe` and `smpd.exe` app-locally, so end users need no separate MS-MPI installer or administrator rights. Users never need compilers, SDKs or build tools.
- Preserve the MPI application binary interface that consumers link against unless the user explicitly decides otherwise. That covers `mpi.h`, `mpif.h` and the Fortran modules, the `msmpi.dll` exports and calling conventions, `MPI_Fint`/handle widths, and the `msmpifec`/`msmpifmc` import libraries. Qualify ABI changes separately from bug fixes.
- Keep changes minimal and local. Reuse existing MS-MPI structure. A behavior change needs source-grounded evidence, a regression test that exercises the corrected path, and full downstream requalification (PETSc, MUMPS, SuperLU_DIST, ScaLAPACK/BLACS and SESKA).

### Internal Version And Build Records

`MSMPI_INTERNAL_VERSION` is the fork's authoritative internal four-part version, independent of upstream's `ProductMajorVersion`, `ProductMinorVersion`, `BuildMajorVersion`, `BuildMinorVersion` and `MSMPI_VER`:

```text
<breaking>.<major>.<minor>.<build-number>
```

- The seed is `10.1.4.0`, chosen by the user on 2026-10-17 to follow Microsoft's last published release, 10.1.3 (10.1.12498.52). It replaces the earlier unbuilt seed `10.2.0.0`; no build was accepted under that seed. This exact seed is unbuilt; it is not a release or a passing-build claim. The first qualifying successful build advances it to `10.1.4.1`. Build zero after a later rollover can identify a successful build.
- Major and minor each range from 0 through 9. **Required (user, 17 November 2026):** the build field ranges from 0 through 99 without zero padding, as in every owned repository; a build field of 100 is refused. Fields are ASCII digits only (`0`–`9`; other Unicode digits are refused), and a version value has no trailing newline or other surrounding whitespace; a reader of the version file strips the file's line end before it parses. A successful build increments the fourth field: `10.1.4.9 -> 10.1.4.10`. Overflow carries to the next field and resets the following fields to zero: `10.1.4.99 -> 10.1.5.0`, `10.1.9.99 -> 10.2.0.0`, `10.9.9.99 -> 11.0.0.0`. This supersedes the earlier one-digit build field; existing records stay valid and are not renumbered. Coordinate allocation across branches and CI, and never publish different artifacts under the same complete identity.
- A build qualifies only when its declared compile/link and packaging stages succeed and its required checks pass. Record the actual scope. The per-PR focused evidence (the incremental build, the focused checks and the council review) qualifies a merged PR for its own build number and focused build record; full acceptance stays at the family or batch run and remains open until then. Failed, cancelled, skipped, configure-only, documentation-only and no-op builds do not advance any field.
- Explicit breaking, major or minor decisions select a candidate family. They do not change `MSMPI_INTERNAL_VERSION` until an artifact-producing build and its required checks accept that candidate.
- Do not encode the internal identity into upstream version fields or `MSMPI_VER`. `ProductMajorVersion`, `BuildMinorVersion`, `MSMPI_VER` and `MSMPI_VER_EX` stay as upstream declares them, because they feed the `mpi.h` ABI, the NetworkDirect connect handshake and trace fields.
- Shipped files report the internal version (user decision, 2026-10-17). Native builds stamp the candidate version into the `msmpi.dll`, `mpiexec.exe` and `smpd.exe` VERSIONINFO resources, `MPI_Get_library_version` and the `mpiexec` banner, together with the source commit. The resources never name Microsoft as company or product, so support can tell fork binaries from Microsoft's even if Microsoft later publishes a 10.1.4.
- Group notes by the first three fields: `.commits/<breaking>.<major>.<minor>.txt`, so the 10.1.4 family shares `.commits/10.1.4.txt`. Use the `spec-driven-design` `create-commit-notes` skill (PETSc-style profile) at seed initialization and at every minor, major or breaking change, including carries. Append every successful build. Never collapse or rewrite earlier transitions.
- Each accepted build gets `.commits/builds/<version>.json` and a family-note record. The record holds UTC time, previous and accepted versions, source commit, toolchain and SDK pins, exact commands, checks with results, artifact hashes and remaining limits. Persist the accepted version and its record in the same change.
- `.commits/manage-repo.ps1` reads `MSMPI_INTERNAL_VERSION` and never changes it. Any `-Version` must equal it. Mutating actions require an attached branch. Tags use the `native/v<internal version>` namespace and are never replaced.

### Required checks (initial; extend as the port matures)

1. A native build of every shipped component from a clean checkout with the pinned toolchain: `msmpi.dll`, `msmpi.lib`, `mpiexec.exe`, `smpd.exe`, the Fortran import libraries and modules, and headers.
2. ABI comparison against Microsoft MS-MPI 10.1.3 (the SDK and runtime pinned by the PETSc fork): export tables, header constants, Fortran module interfaces and handle widths.
3. Launch-race regression: a deterministic test that widens the resume/registration window and proves ranks are registered before they can connect, plus repeated multi-rank launches under starved CPU allocations.
4. MPI interoperability probes (C/Fortran, collectives, special constants, communicators) at 1, 2 and 4 ranks.
5. Downstream requalification before SESKA adopts a build: PETSc fork full profiles, both sparse solvers, and SESKA numerical, lifecycle and packaging checks.

A zero exit is never success evidence by itself.

## Agent Workflow

These rules apply to the orchestrator and its subagents. The suite `AGENTS.md` holds the same common rules. In this fork `master` follows upstream and the product branch is `feature/native-seska-integration`, whose commit the suite pins. Until the user names a different target, "the default branch" below means that product branch.

- **Required (user, 16 November 2026), main stays current:** once a pull request merges, its work must be on the default branch. When a PR merges into an intermediate or stacked branch, a merge agent carries that branch into the default branch at once: it merges with `--no-ff`, resolves conflicts by intent, verifies, pushes to this fork, and merges the merge PR under the user's standing authorisation for this step. Agents never merge a PR the user has not merged. Open stacked PRs against the default branch where possible, because `Closes #n` links and closes issues only for PRs into GitHub's default branch.
- **Required (user, 16 November 2026), subagent models:** implementers and clerks run Claude Fable 5.2 at low effort; reviewers and council seats run Claude Opus 5.5 at high effort (owner, 17 November 2026, replacing the 16 November assignments). The orchestrator works as chief of staff: it hands work to background subagents through context pointers (brief files, commits, issues) and improves the agent environment before feature work, with no workarounds.
- **Required (user, 16 November 2026), skill routing:** use `planning-and-task-breakdown` for features and new implementations (the broad points of the implementation roadmap) before issues are written, `multi-opinion` for architecture decisions and for `research`, and `issue-and-pr-based-development` for the tracker, branch and PR flow.
- **Required (user, 17 November 2026), work per issue or PR:** each issue or PR gets an incremental build of the whole tree with no warnings, focused tests for the modules, checks and fixtures the change touches, mutation evidence that each new test catches its defect, and a council review. Then it merges. A PR runs no full suite and no build tier, and nothing is rerun after a clean merge of the default branch. This supersedes the 16 November "test scope per step" rule and the 11 November per-build tier rule, and the rebuild sentence of the merge verification reuse rule for clean merges of the default branch.
- **Required (user, 17 November 2026), build number per merged PR:** each merged PR that changes source, tests, build files, tools or dependencies gets its own build number and a build record that holds its focused evidence: the incremental build, the focused tests, the mutation evidence and the council review. A PR that changes no build-affecting file (only docs, notes or evidence) takes no build number, with the `git diff --name-only` output as evidence; documentation-only work does not advance a build number. This fork allocates the number under [Internal Version And Build Records](#internal-version-and-build-records): only a qualifying build advances the counter, and the build field runs 0–99. A change to tools only produces no artifact build of its own: it takes no number at merge and rides on the next qualifying build, because the counter advances only on a qualifying artifact build.
- **Required (user, 17 November 2026), phase-end full run:** the full required checks and the build or acceptance run happen once per batch, after the implementation of the phase is done, on the batch head. Their failures are fixed then. The record of that run lists the PR build numbers it covers. Every full run keeps its complete logs. This rule saves time only. It does not relax any numerical or acceptance check at the phase-end run.
- **Required (user, 17 November 2026), build trees:** a build tree is kept until its branch merges and is rebuilt incrementally.
- **Required (user, 17 November 2026), council review per PR:** every PR gets a council review before it merges, with scores out of 10 and a "Council review" table in the PR body. A normal PR is reviewed by one Claude Opus 5.5 (high) and one Gemini 3.8 Flash (High). A big or risky PR is reviewed by one Claude Opus 5.5 (high) and two Gemini 3.8 Flash (High); big or risky means a vendor or fork patch (any change to the MS-MPI source in this fork), a whole package, a storage-format change, or a solver or numerics change. This replaces the 11 and 12 November rule of one independent code review per PR. Issue acceptance criteria plus tests remain the contract, with no prose contract freezes.
- **Required (user, 17 November 2026), research council:** a research council is three Gemini 3.8 Flash (High) and one Claude Opus 5.5 (high). Decision councils stay as before.
- **Required (user, 16 November 2026), merge verification reuse:** a merge that changes no build-affecting file (only verification evidence, notes or docs) reuses the verified parent's results, with the `git diff --name-only` output as evidence. Any change to source, tests, build files, tools or dependencies is rebuilt and verified.

## Shared Instruction Entry Points

`CLAUDE.md`, `ANTIGRAVITY_INSTRUCTIONS.md` and `GEMINI.md` reference `@AGENTS.md`. Keep substantive rules here. Tools that do not expand imports must read this file explicitly. Upstream's `README.md` and source conventions remain in force except where this file supersedes them for the fork.
