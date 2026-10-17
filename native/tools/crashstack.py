"""Run a program under the Win32 debug API and report the stack of its first fatal event.

Usage: python -X utf8 native/tools/crashstack.py [--symbols DIR ...] [--timeout S] -- program [args]

A diagnostic for failures that end a process without a report, such as
__fastfail (0xC0000409). It needs no installed debugger: it uses the debug API
and System32 dbghelp.dll with the build's PDBs. The first stopping event is a
breakpoint after the loader breakpoint, 0xC0000409, or any second-chance
exception. The harness prints the exception code and fast-fail code, then a
symbolized stack walk, and terminates the target. Output is JSON on stdout.
"""
import argparse
import ctypes
from ctypes import wintypes
import json
import os
import sys
import time

kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
dbghelp = ctypes.WinDLL('dbghelp', use_last_error=True)

DEBUG_ONLY_THIS_PROCESS = 0x00000002
CREATE_UNICODE_ENVIRONMENT = 0x00000400
DBG_CONTINUE = 0x00010002
DBG_EXCEPTION_NOT_HANDLED = 0x80010001
EXCEPTION_DEBUG_EVENT, CREATE_THREAD_DEBUG_EVENT, CREATE_PROCESS_DEBUG_EVENT = 1, 2, 3
EXIT_THREAD_DEBUG_EVENT, EXIT_PROCESS_DEBUG_EVENT, LOAD_DLL_DEBUG_EVENT = 4, 5, 6
STATUS_BREAKPOINT, STATUS_WX86_BREAKPOINT, STATUS_STACK_BUFFER_OVERRUN = 0x80000003, 0x4000001F, 0xC0000409
CONTEXT_FULL = 0x0010000B
CONTEXT_SIZE, CONTEXT_FLAGS_OFFSET, RSP_OFFSET, RBP_OFFSET, RIP_OFFSET = 0x4D0, 0x30, 0x98, 0xA0, 0xF8
IMAGE_FILE_MACHINE_AMD64 = 0x8664
SYMOPT_UNDNAME, SYMOPT_DEFERRED_LOADS, SYMOPT_LOAD_LINES, SYMOPT_FAIL_CRITICAL_ERRORS = 0x2, 0x4, 0x10, 0x200


class EXCEPTION_RECORD(ctypes.Structure):
    pass


EXCEPTION_RECORD._fields_ = [('ExceptionCode', wintypes.DWORD), ('ExceptionFlags', wintypes.DWORD),
                             ('ExceptionRecord', ctypes.POINTER(EXCEPTION_RECORD)), ('ExceptionAddress', ctypes.c_void_p),
                             ('NumberParameters', wintypes.DWORD), ('ExceptionInformation', ctypes.c_uint64 * 15)]


class EXCEPTION_DEBUG_INFO(ctypes.Structure):
    _fields_ = [('ExceptionRecord', EXCEPTION_RECORD), ('dwFirstChance', wintypes.DWORD)]


class CREATE_PROCESS_DEBUG_INFO(ctypes.Structure):
    _fields_ = [('hFile', wintypes.HANDLE), ('hProcess', wintypes.HANDLE), ('hThread', wintypes.HANDLE),
                ('lpBaseOfImage', ctypes.c_void_p), ('dwDebugInfoFileOffset', wintypes.DWORD),
                ('nDebugInfoSize', wintypes.DWORD), ('lpThreadLocalBase', ctypes.c_void_p),
                ('lpStartAddress', ctypes.c_void_p), ('lpImageName', ctypes.c_void_p), ('fUnicode', wintypes.WORD)]


class CREATE_THREAD_DEBUG_INFO(ctypes.Structure):
    _fields_ = [('hThread', wintypes.HANDLE), ('lpThreadLocalBase', ctypes.c_void_p), ('lpStartAddress', ctypes.c_void_p)]


class LOAD_DLL_DEBUG_INFO(ctypes.Structure):
    _fields_ = [('hFile', wintypes.HANDLE), ('lpBaseOfDll', ctypes.c_void_p), ('dwDebugInfoFileOffset', wintypes.DWORD),
                ('nDebugInfoSize', wintypes.DWORD), ('lpImageName', ctypes.c_void_p), ('fUnicode', wintypes.WORD)]


class EXIT_PROCESS_DEBUG_INFO(ctypes.Structure):
    _fields_ = [('dwExitCode', wintypes.DWORD)]


class DEBUG_EVENT_UNION(ctypes.Union):
    _fields_ = [('Exception', EXCEPTION_DEBUG_INFO), ('CreateThread', CREATE_THREAD_DEBUG_INFO),
                ('CreateProcessInfo', CREATE_PROCESS_DEBUG_INFO), ('ExitProcess', EXIT_PROCESS_DEBUG_INFO),
                ('LoadDll', LOAD_DLL_DEBUG_INFO), ('Padding', ctypes.c_byte * 160)]


class DEBUG_EVENT(ctypes.Structure):
    _fields_ = [('dwDebugEventCode', wintypes.DWORD), ('dwProcessId', wintypes.DWORD), ('dwThreadId', wintypes.DWORD),
                ('u', DEBUG_EVENT_UNION)]


class STARTUPINFOW(ctypes.Structure):
    _fields_ = [('cb', wintypes.DWORD), ('lpReserved', wintypes.LPWSTR), ('lpDesktop', wintypes.LPWSTR),
                ('lpTitle', wintypes.LPWSTR), ('dwX', wintypes.DWORD), ('dwY', wintypes.DWORD), ('dwXSize', wintypes.DWORD),
                ('dwYSize', wintypes.DWORD), ('dwXCountChars', wintypes.DWORD), ('dwYCountChars', wintypes.DWORD),
                ('dwFillAttribute', wintypes.DWORD), ('dwFlags', wintypes.DWORD), ('wShowWindow', wintypes.WORD),
                ('cbReserved2', wintypes.WORD), ('lpReserved2', ctypes.c_void_p), ('hStdInput', wintypes.HANDLE),
                ('hStdOutput', wintypes.HANDLE), ('hStdError', wintypes.HANDLE)]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [('hProcess', wintypes.HANDLE), ('hThread', wintypes.HANDLE), ('dwProcessId', wintypes.DWORD),
                ('dwThreadId', wintypes.DWORD)]


class ADDRESS64(ctypes.Structure):
    _fields_ = [('Offset', ctypes.c_uint64), ('Segment', wintypes.WORD), ('Mode', ctypes.c_int)]


class STACKFRAME64(ctypes.Structure):
    _fields_ = [('AddrPC', ADDRESS64), ('AddrReturn', ADDRESS64), ('AddrFrame', ADDRESS64), ('AddrStack', ADDRESS64),
                ('AddrBStore', ADDRESS64), ('FuncTableEntry', ctypes.c_void_p), ('Params', ctypes.c_uint64 * 4),
                ('Far', wintypes.BOOL), ('Virtual', wintypes.BOOL), ('Reserved', ctypes.c_uint64 * 3),
                ('KdHelp', ctypes.c_byte * 256)]


class SYMBOL_INFOW(ctypes.Structure):
    _fields_ = [('SizeOfStruct', wintypes.ULONG), ('TypeIndex', wintypes.ULONG), ('Reserved', ctypes.c_uint64 * 2),
                ('Index', wintypes.ULONG), ('Size', wintypes.ULONG), ('ModBase', ctypes.c_uint64), ('Flags', wintypes.ULONG),
                ('Value', ctypes.c_uint64), ('Address', ctypes.c_uint64), ('Register', wintypes.ULONG),
                ('Scope', wintypes.ULONG), ('Tag', wintypes.ULONG), ('NameLen', wintypes.ULONG),
                ('MaxNameLen', wintypes.ULONG), ('Name', wintypes.WCHAR * 1024)]


class IMAGEHLP_LINEW64(ctypes.Structure):
    _fields_ = [('SizeOfStruct', wintypes.DWORD), ('Key', ctypes.c_void_p), ('LineNumber', wintypes.DWORD),
                ('FileName', wintypes.LPWSTR), ('Address', ctypes.c_uint64)]


kernel32.WaitForDebugEvent.argtypes = [ctypes.POINTER(DEBUG_EVENT), wintypes.DWORD]
kernel32.ContinueDebugEvent.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.DWORD]
kernel32.GetThreadContext.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.GetFinalPathNameByHandleW.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
kernel32.CreateProcessW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.c_void_p, ctypes.c_void_p, wintypes.BOOL,
                                    wintypes.DWORD, ctypes.c_void_p, wintypes.LPCWSTR, ctypes.POINTER(STARTUPINFOW),
                                    ctypes.POINTER(PROCESS_INFORMATION)]
dbghelp.SymInitializeW.argtypes = [wintypes.HANDLE, wintypes.LPCWSTR, wintypes.BOOL]
dbghelp.SymSetOptions.argtypes = [wintypes.DWORD]
dbghelp.SymLoadModuleExW.argtypes = [wintypes.HANDLE, wintypes.HANDLE, wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_uint64,
                                     wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
dbghelp.SymLoadModuleExW.restype = ctypes.c_uint64
dbghelp.SymFromAddrW.argtypes = [wintypes.HANDLE, ctypes.c_uint64, ctypes.POINTER(ctypes.c_uint64), ctypes.POINTER(SYMBOL_INFOW)]
dbghelp.SymGetLineFromAddrW64.argtypes = [wintypes.HANDLE, ctypes.c_uint64, ctypes.POINTER(wintypes.DWORD),
                                          ctypes.POINTER(IMAGEHLP_LINEW64)]
dbghelp.SymFunctionTableAccess64.argtypes = [wintypes.HANDLE, ctypes.c_uint64]
dbghelp.SymFunctionTableAccess64.restype = ctypes.c_void_p
dbghelp.SymGetModuleBase64.argtypes = [wintypes.HANDLE, ctypes.c_uint64]
dbghelp.SymGetModuleBase64.restype = ctypes.c_uint64
dbghelp.StackWalk64.argtypes = [wintypes.DWORD, wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(STACKFRAME64), ctypes.c_void_p,
                                ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]


def HandlePath(handle):
    buffer = ctypes.create_unicode_buffer(1024)
    length = kernel32.GetFinalPathNameByHandleW(handle, buffer, 1024, 0)
    return buffer.value[4:] if length and buffer.value.startswith('\\\\?\\') else (buffer.value if length else '')


def Symbolize(process, address):
    # SizeOfStruct must equal sizeof(SYMBOL_INFOW) as declared, with Name[1]: 88 bytes on x64.
    symbol = SYMBOL_INFOW(SizeOfStruct=88, MaxNameLen=1024)
    displacement = ctypes.c_uint64()
    name = symbol.Name if dbghelp.SymFromAddrW(process, address, ctypes.byref(displacement), ctypes.byref(symbol)) else None
    line = IMAGEHLP_LINEW64(SizeOfStruct=ctypes.sizeof(IMAGEHLP_LINEW64))
    line_displacement = wintypes.DWORD()
    where = (f'{line.FileName}:{line.LineNumber}'
             if dbghelp.SymGetLineFromAddrW64(process, address, ctypes.byref(line_displacement), ctypes.byref(line)) else None)
    return dict(address=hex(address), symbol=f'{name}+{hex(displacement.value)}' if name else None, line=where)


def StackWalk(process, thread, modules, depth=48):
    context = (ctypes.c_byte * (CONTEXT_SIZE + 16))()
    aligned = (ctypes.addressof(context) + 15) & ~15
    ctypes.c_uint32.from_address(aligned + CONTEXT_FLAGS_OFFSET).value = CONTEXT_FULL
    if not kernel32.GetThreadContext(thread, aligned):
        return dict(error=f'GetThreadContext failed: {ctypes.get_last_error()}')
    frame = STACKFRAME64()
    for field, offset in (('AddrPC', RIP_OFFSET), ('AddrStack', RSP_OFFSET), ('AddrFrame', RBP_OFFSET)):
        getattr(frame, field).Offset = ctypes.c_uint64.from_address(aligned + offset).value
        getattr(frame, field).Mode = 3
    frames = []
    access = ctypes.cast(dbghelp.SymFunctionTableAccess64, ctypes.c_void_p)
    base = ctypes.cast(dbghelp.SymGetModuleBase64, ctypes.c_void_p)
    while len(frames) < depth and dbghelp.StackWalk64(IMAGE_FILE_MACHINE_AMD64, process, thread, ctypes.byref(frame),
                                                     aligned, None, access, base, None):
        if not frame.AddrPC.Offset:
            break
        entry = Symbolize(process, frame.AddrPC.Offset)
        start, name = next(((start, name) for start, name in sorted(modules.items(), reverse=True)
                            if start <= frame.AddrPC.Offset), (0, None))
        entry.update(module=name, rva=hex(frame.AddrPC.Offset - start) if name else None)
        frames.append(entry)
    return frames


def Main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--symbols', action='append', default=[], help='Extra symbol (PDB) directory')
    parser.add_argument('--timeout', type=float, default=60)
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    if not command:
        parser.error('program required')
    startup, information = STARTUPINFOW(cb=ctypes.sizeof(STARTUPINFOW)), PROCESS_INFORMATION()
    line = ctypes.create_unicode_buffer(' '.join(f'"{part}"' if ' ' in part else part for part in command))
    if not kernel32.CreateProcessW(os.path.abspath(command[0]), line, None, None, True, DEBUG_ONLY_THIS_PROCESS, None, None,
                                   ctypes.byref(startup), ctypes.byref(information)):
        raise OSError(ctypes.get_last_error(), 'CreateProcessW')
    process, threads, modules, events, failures = information.hProcess, {}, {}, [], []
    search = ';'.join([os.path.dirname(os.path.abspath(command[0])), *args.symbols])
    dbghelp.SymSetOptions(SYMOPT_UNDNAME | SYMOPT_DEFERRED_LOADS | SYMOPT_LOAD_LINES | SYMOPT_FAIL_CRITICAL_ERRORS)
    if not dbghelp.SymInitializeW(process, search, False):
        raise OSError(ctypes.get_last_error(), 'SymInitializeW')
    report, seen_loader_breakpoint, deadline = None, False, time.monotonic() + args.timeout
    event = DEBUG_EVENT()
    while time.monotonic() < deadline:
        if not kernel32.WaitForDebugEvent(ctypes.byref(event), 500):
            continue
        status, code = DBG_CONTINUE, event.dwDebugEventCode
        if code == CREATE_PROCESS_DEBUG_EVENT:
            info = event.u.CreateProcessInfo
            threads[event.dwThreadId] = info.hThread
            path = HandlePath(info.hFile)
            modules[info.lpBaseOfImage] = os.path.basename(path)
            if not dbghelp.SymLoadModuleExW(process, info.hFile, path, None, info.lpBaseOfImage, 0, None, 0):
                failures.append(dict(module=path, error=ctypes.get_last_error()))
            kernel32.CloseHandle(info.hFile)
        elif code == CREATE_THREAD_DEBUG_EVENT:
            threads[event.dwThreadId] = event.u.CreateThread.hThread
        elif code == LOAD_DLL_DEBUG_EVENT:
            info = event.u.LoadDll
            path = HandlePath(info.hFile)
            modules[info.lpBaseOfDll] = os.path.basename(path)
            if not dbghelp.SymLoadModuleExW(process, info.hFile, path, None, info.lpBaseOfDll, 0, None, 0):
                failures.append(dict(module=path, error=ctypes.get_last_error()))
            kernel32.CloseHandle(info.hFile)
        elif code == EXCEPTION_DEBUG_EVENT:
            record, first = event.u.Exception.ExceptionRecord, event.u.Exception.dwFirstChance
            exception = record.ExceptionCode
            events.append(dict(code=hex(exception), first_chance=bool(first), address=hex(record.ExceptionAddress or 0)))
            if exception in (STATUS_BREAKPOINT, STATUS_WX86_BREAKPOINT) and not seen_loader_breakpoint:
                seen_loader_breakpoint = True
            elif exception in (STATUS_BREAKPOINT, STATUS_STACK_BUFFER_OVERRUN) or not first:
                parameters = [hex(record.ExceptionInformation[index]) for index in range(min(record.NumberParameters, 15))]
                report = dict(exception=hex(exception), first_chance=bool(first), thread=event.dwThreadId,
                              exception_address=Symbolize(process, record.ExceptionAddress or 0), parameters=parameters,
                              fast_fail_code=int(parameters[0], 16) if exception == STATUS_STACK_BUFFER_OVERRUN and parameters else None,
                              stack=StackWalk(process, threads.get(event.dwThreadId), modules))
                kernel32.TerminateProcess(process, 0xDEAD)
            else:
                status = DBG_EXCEPTION_NOT_HANDLED
        elif code == EXIT_PROCESS_DEBUG_EVENT:
            exit_code = event.u.ExitProcess.dwExitCode
            kernel32.ContinueDebugEvent(event.dwProcessId, event.dwThreadId, DBG_CONTINUE)
            print(json.dumps(dict(exit_code=hex(exit_code), report=report, exceptions=events[-20:], symbol_load_failures=failures,
                                  modules={hex(start): name for start, name in sorted(modules.items())}), indent=2))
            return 0 if report else 2
        kernel32.ContinueDebugEvent(event.dwProcessId, event.dwThreadId, status)
    kernel32.TerminateProcess(process, 0xDEAD)
    print(json.dumps(dict(timeout=args.timeout, report=report, exceptions=events[-20:]), indent=2))
    return 3


if __name__ == '__main__':
    sys.exit(Main())
