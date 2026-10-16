# Native Windows build port

This directory will hold the fork's native build driver and its checks. Nothing here is built or verified yet; each phase below records its own evidence when it completes. Rules: [`../AGENTS.md`](../AGENTS.md).

## Why the upstream build cannot be used as-is

The findings were observed at base `f2d849f`.

| Upstream step | Where | Problem for the suite's native rules |
| --- | --- | --- |
| CommonBuildToolSet (CBT) | `Directory.Build.props` imports `.build/CBT/build.props` | At property evaluation, the build loads the checked-in `CBT.Core.dll` by reflection, downloads `NuGet.exe` and restores `CBT.Traversal` 2.0.53 and `CBT.NuGet` 2.2.2 from the network. These are opaque, unpinned build-time inputs. |
| Toolset pins | `Directory.Build.props:116-119` | `v141`, VC tools `14.15.26726`, SDK `10.0.16299.0`; none is installed under the suite's pins (VS Build Tools 17.14, toolset `14.44.35207`, SDK `10.0.26100.0`). |
| Error-message catalog | `src/mpi/common/mpicommon.vcxproj:94` runs `perl extracterrmsgs` (799 lines, with `parse.sub`) | Perl on Windows usually means Strawberry Perl, which is MinGW-based and excluded. Upstream runs the step with `IgnoreExitCode="true"`. |
| Fortran objects | `src/mpi/msmpi/fortran/lib/mpifort.vcxproj` runs mingw-w64 `gfortran.exe -fno-underscoring` on `setbotf.f` | MinGW is excluded. |
| Fortran symbol aliases | `src/mpi/msmpi/fortran/fortran.props` runs `$(PUBLIC_RAZZLE_TOOLS)\amd64\aliasobj` on `amd64.{cdecl,stdcall}.alias` (2,494 and 2,598 `alias <name> <target>` lines) | `aliasobj` is a Microsoft-internal tool that is not in the repository. The step also runs with `IgnoreExitCode="true"`, so a public build silently omits the aliases. |

## Phases

1. **Pinned environment and CBT gate.** Add a fork-owned toolchain pin file and vendor-environment capture. Remove `NoDefaultCurrentDirectoryInExePath` only for the vendor-script session, because Intel `setvars.bat` needs it absent. Gate the CBT import behind a native-build property and drive the `.vcxproj` projects in dependency order with toolset `v143` and the pinned SDK. The upstream path stays unchanged when the property is unset.
2. **Generators without Perl or internal tools.** Port `extracterrmsgs`/`parse.sub` to Python. Replace `aliasobj` with a Python COFF writer that emits weak externals (`IMAGE_WEAK_EXTERN_SEARCH_ALIAS`) for every `.alias` line. Never ignore generator exit codes.
3. **Fortran with Intel `ifx`.** Compile `setbotf.f` with flags whose external and common-block names match gfortran's `-fno-underscoring` objects.
4. **ABI parity with Microsoft MS-MPI 10.1.3** (the runtime and SDK pinned by the PETSc fork). Compare `msmpi.dll` exports, import libraries, Fortran library symbol tables including weak aliases, header constants and handle widths, and the error-message catalog against strings in Microsoft's `msmpi.dll`. Treat any difference as a finding, never as noise.
5. **Launch-race fix.** Register each rank (`smpd_add_process`) before `ResumeThread` in `smpd_launch_process.cpp`. Add a deterministic regression test that widens the window and proves an early rank connect is accepted, plus repeated multi-rank launches under restricted CPU masks.
6. **Packaging and downstream.** Produce an app-local runtime (`msmpi.dll`, `mpiexec.exe`, `smpd.exe`) and an SDK. Decide file-version identity, signing and licence notices. Requalify the PETSc fork, MUMPS, SuperLU_DIST, oneMKL BLACS and SESKA before SESKAFEA adopts it.

A build that passes phases 1–4 may be accepted as `10.2.0.1` with that scope recorded. Behavior changes (phase 5) follow as later builds.
