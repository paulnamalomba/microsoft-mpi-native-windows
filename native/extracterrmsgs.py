"""Python port of src/mpi/common/extracterrmsgs and parse.sub (upstream f2d849f).

It reproduces the Perl generator's defmsg.h and errtest.c byte for byte. The
Perl-on-Windows behavior it mirrors: CRLF translation on input and output,
directory-enumeration order, greedy quote extraction, and the trailing-digit
routine-name fallback. Unlike upstream's MSBuild step, which ignored the exit
code, any "error :" diagnostic makes the port exit with status 1.

Usage (same arguments as the Perl script):
  python -X utf8 native/extracterrmsgs.py -skip=FILE -outpath=DIR -outfile=defmsg.h -testfile=errtest.c DIR...
"""
import os
from pathlib import Path
import re
import sys

ENCODING = 'latin-1'  # byte-transparent, like Perl without a decoding layer
KNOWN_ERR_ROUTINES = {
    'MPIR_Err_create_code': (3, -1, 1, 1),
    'MPIR_ERRTEST_VALID_HANDLE': (4, -1, 0, 1),
    'MPIU_ERR_FATAL_GET': (2, -1, 0, 1),
    'MPIU_ERR_GET': (1, -1, 0, 1),
    'MPIU_ERR_CLASS_GET': (2, -1, 0, 1),
    'MPIU_ERR_CREATE': (1, -1, 0, 1),
    'MPIU_E_ERR': (0, -1, 0, 1),
}
SOURCE_NAME = re.compile(r'(.*\.[chi][xp]*)$')
ROUTINE = re.compile(r'(MPI[OUR]_E[A-Za-z0-9_]+)\s*(\(.*)$')
TEST_TYPES = {
    'x': 'int', 'd': 'int', 'i': 'int', 't': 'int', 'A': 'int', 'l': 'int',
    's': 'string', 'p': 'pointer', 'C': 'MPI_COMM_WORLD', 'I': 'MPI_INFO_NULL', 'D': 'MPI_INT',
    'F': 'file', 'W': 'MPI_WIN_NULL', 'G': 'MPI_GROUP_NULL', 'O': 'MPI_SUM', 'R': 'MPI_REQUEST_NULL',
    'E': 'MPI_ERRORS_RETURN', 'g': 'guid',
}


class Lines:
    """A Perl file handle opened in text mode on Windows: CRLF becomes LF; lines keep their LF."""

    def __init__(self, path):
        data = Path(path).read_bytes().decode(ENCODING).replace('\r\n', '\n')
        self.lines = data.splitlines(keepends=True)
        self.index = 0

    def readline(self):
        if self.index >= len(self.lines):
            return None
        line = self.lines[self.index]
        self.index += 1
        return line

    def eof(self):
        return self.index >= len(self.lines)


def ReadMore(handle):
    line = handle.readline()
    return '' if line is None else line


class Generator:
    def __init__(self, testfile):
        self.testfile = testfile
        self.skip = set()
        self.errname_files = []
        self.longnames = {}
        self.longnames_defined = {}
        self.longnames_used = {}
        self.specific_msgs = {}
        self.specific_loc = {}
        self.test_specific_msg = {}
        self.bad_syntax = set()
        self.filename = ''
        self.errors = 0
        self.test = []

    def Diagnostic(self, text):
        sys.stderr.write(text + '\n')
        if ' : error : ' in text:
            self.errors += 1

    # parse.sub ---------------------------------------------------------------------------------
    def GetBalancedParen(self, handle, curline):
        paren_count, result, count = 1, '', 0
        curline = re.sub(r'\\$', '', curline, count=1)
        match = re.match(r'^([^\(]*)\((.*)$', curline)
        if not match:
            return curline, '', ''
        leading, curline, result = match.group(1), match.group(2), '('
        while count < 200 and paren_count > 0:
            opened = re.match(r'^([^\(\)]*\()(.*$)', curline)
            closed = None if opened else re.match(r'^([^\(\)]*\))(.*$)', curline)
            if opened:
                result += opened.group(1)
                curline = opened.group(2)
                paren_count += 1
            elif closed:
                result += closed.group(1)
                curline = closed.group(2)
                paren_count -= 1
            else:
                result += curline
                curline = re.sub(r'[\r]*\n', '', ReadMore(handle), count=1)
                curline = re.sub(r'\\$', '', curline, count=1)
            count += 1
        return leading, result, curline

    def GetString(self, handle, curline):
        result, count = '', 0
        match = re.match(r'^([^\"]*)\"(.*)$', curline)
        if not match:
            return curline, '', ''
        leading, curline, result = match.group(1), match.group(2), '"'
        while count < 200:
            escaped = re.match(r'^([^\"]*\\\")(.*$)', curline)
            closing = None if escaped else re.match(r'^([^\"]*\")(.*$)', curline)
            if escaped:
                result += escaped.group(1)
                curline = escaped.group(2)
            elif closing:
                result += closing.group(1)
                curline = closing.group(2)
                break
            else:
                result += curline
                curline = re.sub(r'[\r]*\n', '', ReadMore(handle), count=1)
            count += 1
        return leading, result, curline

    def GetSubArgs(self, handle, curline):
        args = []
        curline = re.sub(r'[\r\n]', '', curline)
        if not curline.startswith('('):
            raise RuntimeError('No initial paren')
        leader, outer, remainder = self.GetBalancedParen(handle, curline)
        match = re.match(r'^\s*\(\s*(.*)\s*\)\s*$', outer)
        outer = match.group(1) if match else ''
        trailing = re.match(r'(.*)\s+$', outer)
        if trailing:
            outer = trailing.group(1)
        arg = ''
        while outer != '':
            simple = re.match(r'^([^,\(\"]*)\s*,\s*(.*$)', outer)
            if simple:
                arg += simple.group(1)
                args.append(arg)
                outer = simple.group(2)
                arg = ''
                continue
            if re.match(r'^([^,\"]*)\((.*$)', outer):
                start, balanced, outer = self.GetBalancedParen(handle, outer)
                arg = arg + start + balanced
                continue
            if re.match(r'^([^,\(]*)\"(.*$)', outer):
                start, string, outer = self.GetString(handle, outer)
                arg = arg + start + string
                continue
            arg += outer
            outer = ''
        if arg != '':
            args.append(arg)
        return leader, remainder, args

    @staticmethod
    def StripComments(handle, curline):
        if '//' in curline:
            return re.sub(r'//.*', '', curline, count=1)
        while '/*' in curline:
            if re.search(r'/\*.*?\*/', curline, re.S):
                curline = re.sub(r'/\*.*?\*/', '', curline, count=1, flags=re.S)
            else:
                if handle.eof():
                    line = curline
                    first = re.match(r'(.*)\n', line)
                    if first:
                        line = first.group(1)
                    sys.stdout.write('Unterminated comment found!\nComment begins with ' + line + '\n')
                    return curline
                curline += ReadMore(handle)
        return curline

    # extracterrmsgs ----------------------------------------------------------------------------
    def ExpandDir(self, directory):
        files, other_dirs = [], []
        with os.scandir(directory) as entries:
            for entry in entries:
                name = entry.name
                if name.startswith('.') or name == 'CVS' or name == self.testfile:
                    continue
                path = f'{directory}/{name}'
                if os.path.isdir(path):
                    if name not in ('objd', 'obj'):
                        other_dirs.append(path)
                elif SOURCE_NAME.search(name):
                    if path not in self.skip and f'{directory}\\{name}' not in self.skip:
                        files.append(path)
                elif name == 'errnames.txt':
                    self.errname_files.append(path)
        for other in other_dirs:
            files += self.ExpandDir(other)
        return files

    def ReadErrnamesFile(self, source):
        try:
            handle = Lines(source)
        except OSError:
            return
        linecount = 0
        while True:
            line = handle.readline()
            if line is None:
                break
            linecount += 1
            if re.match(r'^\s*#', line):
                continue
            match = re.match(r'^\s*(\*\*[^:]*):(.*)$', line)
            if not match:
                continue
            name, repl = match.group(1), match.group(2)
            repl = re.sub(r'\r*\n*$', '', repl)
            while re.search(r'\\\s*$', repl):
                repl = re.sub(r'\\\s*$', '', repl, count=1)
                inline = ReadMore(handle)
                linecount += 1
                inline = re.sub(r'^\s*', '', inline, count=1)
                repl += inline
                repl = re.sub(r'[\r\n]*$', '', repl)
            if ('%' in name) != ('%' in repl):
                self.Diagnostic(f'{source}({linecount}) : error : format control usage in {name} and {repl} do not agree')
            quote = re.search(r'(.)\"', repl)
            if quote and quote.group(1) != '\\':
                self.Diagnostic(f'{source}({linecount}) : error : replacement text for {name} contains an unescaped double quote: {repl}')
            if name not in self.longnames:
                self.longnames[name] = repl
                self.longnames_defined[name] = f'{source}:{linecount}'
            else:
                self.Diagnostic(f'{source}({linecount}) : warning : attempt to redefine {name}.  Duplicate ignored.')

    def AddTestCall(self, generic_arg_loc, args):
        specific_msg = args[generic_arg_loc]
        quoted = re.search(r'(\".*\")', specific_msg)
        if quoted:
            specific_msg = quoted.group(1)
        last_errcode, fatal_flag, errclass = 'MPI_SUCCESS', 'MPIR_ERR_RECOVERABLE', 'MPI_ERR_OTHER'
        if specific_msg in self.test_specific_msg:
            return
        self.test_specific_msg[specific_msg] = self.filename
        out = self.test
        out.append('    {\n')
        out.append(f'    /* {self.filename} */\n')
        fmt, full_format, narg, names = specific_msg, specific_msg, 0, []
        while True:
            match = re.search(r'[^%]*%(.)(.*)', fmt)
            if not match:
                break
            kind, fmt = match.group(1), match.group(2)
            narg += 1
            meaning = TEST_TYPES.get(kind)
            if meaning == 'int':
                out.append(f'    int i{narg} = {narg};\n'); names.append(f'i{narg}')
            elif meaning == 'string':
                out.append(f'    char s{narg}[] = "string{narg}";\n'); names.append(f's{narg}')
            elif meaning == 'pointer':
                out.append(f'    char s{narg}[] = "pointer{narg}";\n'); names.append(f's{narg}')
            elif meaning == 'file':
                out.append(f'    MPI_File i{narg} = MPI_FILE_NULL;\n    OACR_USE_PTR( i{narg} );\n'); names.append(f'i{narg}')
            elif meaning == 'guid':
                out.append(f'    GUID g{narg} = {{ 0x4d36e96e, 0xe325, 0x11c3, {{ 0xbf, 0xc1, 0x08, 0x00, 0x2b, 0xe1, 0x03, 0x18 }} }};\n')
                names.append(f'g{narg}')
            elif meaning is not None:
                out.append(f'    int i{narg} = {meaning};\n'); names.append(f'i{narg}')
            else:
                self.Diagnostic(f'{self.filename} : error : unrecognized format type {kind} for {full_format}')
        actual = len(args) - generic_arg_loc - 1
        if actual != narg:
            self.Diagnostic(f'{self.filename} : error : format {full_format} provides {narg} arguments but call has {actual}')
        out.append(f'    err = MPIR_Err_create_code({last_errcode}, {fatal_flag}, {errclass}, {specific_msg}'
                   + ''.join(f', {name}' for name in names) + ');\n')
        out.append(f'    ChkMsg( err, {errclass}, {specific_msg} );\n    }}\n')

    def ProcessFile(self, filename):
        self.filename = filename
        handle = Lines(filename)
        linecount = 0
        while True:
            line = handle.readline()
            if line is None:
                break
            linecount += 1
            if re.search(r'/\*\s+--BEGIN ERROR MACROS--\s+\*/', line):
                while True:
                    skipped = handle.readline()
                    if skipped is None:
                        break
                    linecount += 1
                    if re.search(r'/\*\s+--END ERROR MACROS--\s+\*/', skipped):
                        break
                continue
            text = self.StripComments(handle, line)
            if re.search(r'MPI_RESULT\s+MPI[OUR]_Err_create_code', text):
                continue
            while True:
                found = ROUTINE.search(text)
                if not found:
                    break
                name, arglist = found.group(1), found.group(2)
                if name not in KNOWN_ERR_ROUTINES:
                    if re.search(r'[1-9]$', name) and name[:-1] in KNOWN_ERR_ROUTINES:
                        name = name[:-1]
                    else:
                        break
                generic_arg_loc, has_line, _, only_indirect = KNOWN_ERR_ROUTINES[name]
                _, remainder, args = self.GetSubArgs(handle, arglist)
                text = remainder  # Perl's continue block: $_ = $remainder
                if len(args) - 1 < generic_arg_loc:
                    if filename not in self.bad_syntax:
                        self.bad_syntax.add(filename)
                        self.Diagnostic(f'{filename}({linecount}) : error : {name} call with too few arguments')
                    continue
                specific_msg = args[generic_arg_loc]
                quoted = re.search(r'(\".*\")', specific_msg)
                if quoted:
                    specific_msg = quoted.group(1)
                if re.search(r'\s"', specific_msg):
                    self.Diagnostic(f"{filename}({linecount}) : warning : trailing blank in error key '{specific_msg}'")
                if only_indirect and not re.search(r'^\"\*\*.+\"$', specific_msg):
                    self.Diagnostic(f"{filename}({linecount}) : error : error key '{specific_msg}' has incorrect format")
                    continue
                if '%' in specific_msg:
                    nargs, tmp = 0, specific_msg
                    while True:
                        match = re.search(r'[^%]*%(.)(.*)', tmp)
                        if not match:
                            break
                        tmp, follow = match.group(2), match.group(1)
                        if follow != '%':
                            nargs += 1
                        if not re.match(r'[%xsditpDCRWOEIGFAgl]', follow):
                            self.Diagnostic(f'{filename}({linecount}) : error : Unrecognized format specifier in error key {specific_msg}')
                    actual = len(args) - 1 - generic_arg_loc
                    if nargs != actual:
                        self.Diagnostic(f'{filename}({linecount}) : error : wrong number of arguments for instance error key '
                                        f'{specific_msg}; expected {nargs} but found {actual}')
                self.AddTestCall(generic_arg_loc, args)
                key = re.search(r'^\"(\*\*.*)\"', specific_msg)
                if key:
                    key = key.group(1)
                    self.specific_msgs[key] = self.specific_msgs.get(key, 0) + 1
                    self.specific_loc[key] = self.specific_loc.get(key, '') + f':{filename}({linecount})'

    def Header(self):
        return ('/* -*- Mode: C; c-basic-offset:4 ; -*- */\n/*  \n *  (C) 2001 by Argonne National Laboratory.\n'
                ' *      See COPYRIGHT in top-level directory.\n *\n * This file automatically created by extracterrmsgs\n'
                ' * DO NOT EDIT\n */\n'
                'typedef struct msgpair {\n    const char* key;\n    const char* fmt;\n} msgpair;\n\n')

    def Mapping(self, srcroot):
        class_msgs = []
        handle = Lines(f'{srcroot}/baseerrnames.txt')
        while True:
            line = handle.readline()
            if line is None:
                break
            line = re.sub(r'#.*$', '', line, count=1)
            fields = re.split(r'\s\s*', line)
            while fields and fields[-1] == '':
                fields.pop()  # Perl split drops trailing empty fields
            if len(fields) < 2:
                continue
            number = int(fields[1]) if re.match(r'^\d+$', fields[1]) else 0
            short = fields[2] if len(fields) > 2 else ''
            if short != '':
                if '%' in short:
                    self.Diagnostic(f'{srcroot}/baseerrnames.txt(1) : error : message {short} in baseerrnames.txt contains format control')
                self.specific_msgs[short] = self.specific_msgs.get(short, 0) + 1
                self.specific_loc[short] = ':baseerrnames.txt(1)'
                while len(class_msgs) <= number:
                    class_msgs.append(None)
                class_msgs[number] = short
        out, short_to_num, number = [], {}, 0
        for key in sorted(self.specific_msgs):
            if key not in self.longnames:
                self.Diagnostic(f'{self.specific_loc[key]} : error : shortname {key} for specific messages has no expansion')
                continue
            self.longnames_used[key] = self.longnames_used.get(key, 0) + 1
            out.append(f'extern const __declspec(selectany) char short_spc{number}[] = "{key}";\n')
            out.append(f'extern const __declspec(selectany) char long_spc{number}[]  = "{self.longnames[key]}";\n')
            short_to_num[key] = number
            number += 1
        out.append('\n\nextern const __declspec(selectany) msgpair errors_map[] = {\n')
        for index in range(number):
            out.append(f'{{ short_spc{index}, long_spc{index} }}' + (',' if index < number - 1 else '') + '\n')
        out.append('};\n\n')
        out.append('extern const __declspec(selectany) int class_to_index[] = {\n')
        last = len(class_msgs) - 1
        for index, short in enumerate(class_msgs):
            value = short_to_num.get(short, '') if short is not None else ''
            out.append(str(value) + (',' if index < last else '') + ('' if (index + 1) % 10 else '\n'))
        out.append('};\n')
        return ''.join(out)


TEST_PREAMBLE = r'''/* -*- Mode: C++; c-basic-offset:4 ; -*- */
/*
 *  (C) 2004 by Argonne National Laboratory.
 *      See COPYRIGHT in top-level directory.
 *
 * This file is automatically generated by maint/extracterrmsgs
 * DO NOT EDIT
 */

#include <stdio.h>
#include <stdlib.h>
#include <stdarg.h>
#include <oacr.h>
#include <windows.h>
#include "mpi.h"
#include "mpierror.h"
#include "errcodes.h"

#pragma warning(disable:4100) // unreferenced formal parameter

void MPID_Type_get_envelope(MPI_Datatype datatype, int* num_integers, int* num_addresses, int* num_datatypes, int* combiner)
{
    *combiner = MPI_COMBINER_NAMED;
}

typedef struct MPID_Comm MPID_Comm;

_Analysis_noreturn_
int
MPID_Abort(
    _Inout_opt_ MPID_Comm* comm,
    _In_ BOOL intern,
    _In_ int exit_code,
    _In_z_ const char* error_msg
    )
{
    printf("MPID_Abort called. exit code (%d); msg '%s'", exit_code, error_msg);
    exit(exit_code);
}

_Success_(return>=0)
int
MPIU_Internal_error_printf(
    _In_z_ _Printf_format_string_params_(...) const char *str,
    ...
    )
{
    int n;
    va_list list;

    va_start(list, str);
    n = vfprintf(stderr, str, list);
    va_end(list);

    fflush(stderr);

    return n;
}

void ChkMsg( int err, int msgclass, const char msg[] )
{
    char errmsg[MPI_MAX_ERROR_STRING];

    MPIR_Err_get_string( err, errmsg, MPI_MAX_ERROR_STRING );

    printf( "[0x%08x] [0x%08x] %2d %s\n%s\n", err, MPIR_Err_get_user_error_code(err), msgclass, msg, errmsg );
}


'''


def WriteText(path, text):
    """Perl on Windows writes through the :crlf layer."""
    Path(path).write_bytes(text.replace('\n', '\r\n').encode(ENCODING))


def Main(argv):
    outfile, testfile, outpath, build_test = '', 'errtest.c', '.', True
    srcroot = str(Path(__file__).resolve().parents[1] / 'src/mpi/common')
    skip, inputs = set(), []
    for arg in argv:
        if arg.startswith('-notest'):
            build_test = False
        elif arg.startswith('-outfile='):
            outfile = arg.split('=', 1)[1]
        elif arg.startswith('-outpath='):
            outpath = arg.split('=', 1)[1]
        elif arg.startswith('-testfile='):
            testfile = arg.split('=', 1)[1]
        elif arg.startswith('-srcroot='):
            srcroot = arg.split('=', 1)[1]
        elif arg.startswith('-skip='):
            skip.add(arg.split('=', 1)[1])
        elif arg.startswith('-'):
            raise SystemExit('Unsupported option ' + arg)
        else:
            inputs.append(arg)
    generator = Generator(testfile)
    generator.skip = skip
    files = []
    for item in inputs:
        files += generator.ExpandDir(item) if os.path.isdir(item) else [item]
    for name in files:
        generator.ProcessFile(name)
    for source in generator.errname_files:
        generator.ReadErrnamesFile(source)
    text = generator.Header() + generator.Mapping(srcroot)
    if not outfile:
        raise SystemExit('-outfile is required')
    WriteText(f'{outpath}\\{outfile}', text)
    if build_test:
        test = (TEST_PREAMBLE + 'int __cdecl main(int argc, char **argv)\n{\n    int err;\n\n'
                + '    printf("mpi_errno    user_errno  class  error id string\\n");\n'
                + ''.join(generator.test) + '    printf("---------- end ----------\\n");\n\n    return 0;\n}\n')
        WriteText(f'{outpath}\\{testfile}', test)
    print(f'extracterrmsgs: {len(files)} files, {len(generator.longnames)} names, '
          f'{len(generator.specific_msgs)} keys, {generator.errors} errors')
    return 1 if generator.errors else 0


if __name__ == '__main__':
    sys.exit(Main(sys.argv[1:]))
