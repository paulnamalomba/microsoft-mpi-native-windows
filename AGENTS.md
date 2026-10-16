# AGENTS.md

Rules for people and coding agents working in this repository.

## Native Windows Fork Contract

This repository is the user's [microsoft-mpi-native-windows](https://github.com/paulnamalomba/microsoft-mpi-native-windows) fork of Microsoft MPI (MS-MPI). SESKAFEA integrates it as `dependencies/msmpi/`. All intentional MS-MPI source, build-system, packaging and test changes belong here. The suite owns orchestration and the exact fork gitlink. Publish fork changes before advancing that gitlink. Create a working branch before committing from a detached submodule checkout.

This is an independently maintained product fork. It starts from upstream `microsoft/Microsoft-MPI` master `f2d849ff483b36add9bc2d18c6d7bbc9ce38f4ad` (2024-03-06). The last upstream tag is `v10.1.1` (`c8daf40`); the source declares `10.1.12498.16` in `src/mpi.props`. Upstream is a read/fetch reference only:

- Do not add an upstream remote with a push URL.
- Do not push branches or tags there, and do not open pull requests, issues or comments there.
- Upstream tags are not republished here; this fork's own tags start with its first accepted build.
- GitHub records this repository as a fork, so tools may default to the upstream base. Every push and any requested pull request must explicitly target `paulnamalomba/microsoft-mpi-native-windows`. Do not create pull requests unless the user asks.

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

- The seed is `10.2.0.0`. This exact seed is unbuilt; it is not a release or a passing-build claim. The first qualifying successful build advances it to `10.2.0.1`. The `10.2` family follows the last upstream release line (10.1) and cannot collide with Microsoft's published 10.1.x binaries. Build zero after a later rollover can identify a successful build.
- Build numbers range from 0 through 99, minor from 0 through 9, and major from 0 through 99. A successful build increments the fourth field. Overflow carries to the next field and resets the following fields to zero: `10.2.0.99 -> 10.2.1.0`, `10.2.9.99 -> 10.3.0.0`, `10.99.9.99 -> 11.0.0.0`. Coordinate allocation across branches and CI, and never publish different artifacts under the same complete identity.
- A build qualifies only when its declared compile/link and packaging stages succeed and its required checks pass. Record the actual scope. Failed, cancelled, skipped, configure-only, documentation-only and no-op builds do not advance any field.
- Explicit breaking, major or minor decisions select a candidate family. They do not change `MSMPI_INTERNAL_VERSION` until an artifact-producing build and its required checks accept that candidate.
- Do not encode the internal identity into upstream version fields or `MSMPI_VER` merely to store it. How shipped DLL and EXE file-version resources identify a fork build, so that support can tell them apart from Microsoft's binaries, is an open decision; record it before the first release.
- Group notes by the first three fields: `.commits/<breaking>.<major>.<minor>.txt`, so the 10.2.0 family shares `.commits/10.2.0.txt`. Use the `spec-driven-design` `create-commit-notes` skill (PETSc-style profile) at seed initialization and at every minor, major or breaking change, including carries. Append every successful build. Never collapse or rewrite earlier transitions.
- Each accepted build gets `.commits/builds/<version>.json` and a family-note record. The record holds UTC time, previous and accepted versions, source commit, toolchain and SDK pins, exact commands, checks with results, artifact hashes and remaining limits. Persist the accepted version and its record in the same change.
- `.commits/manage-repo.ps1` reads `MSMPI_INTERNAL_VERSION` and never changes it. Any `-Version` must equal it. Mutating actions require an attached branch. Tags use the `native/v<internal version>` namespace and are never replaced.

### Required checks (initial; extend as the port matures)

1. A native build of every shipped component from a clean checkout with the pinned toolchain: `msmpi.dll`, `msmpi.lib`, `mpiexec.exe`, `smpd.exe`, the Fortran import libraries and modules, and headers.
2. ABI comparison against Microsoft MS-MPI 10.1.3 (the SDK and runtime pinned by the PETSc fork): export tables, header constants, Fortran module interfaces and handle widths.
3. Launch-race regression: a deterministic test that widens the resume/registration window and proves ranks are registered before they can connect, plus repeated multi-rank launches under starved CPU allocations.
4. MPI interoperability probes (C/Fortran, collectives, special constants, communicators) at 1, 2 and 4 ranks.
5. Downstream requalification before SESKA adopts a build: PETSc fork full profiles, both sparse solvers, and SESKA numerical, lifecycle and packaging checks.

A zero exit is never success evidence by itself.

## Shared Instruction Entry Points

`CLAUDE.md`, `ANTIGRAVITY_INSTRUCTIONS.md` and `GEMINI.md` reference `@AGENTS.md`. Keep substantive rules here. Tools that do not expand imports must read this file explicitly. Upstream's `README.md` and source conventions remain in force except where this file supersedes them for the fork.
