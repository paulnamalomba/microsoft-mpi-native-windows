"""Compare a generated defmsg.h against strings in a reference msmpi.dll.

Usage: python -X utf8 native/check_errmsgs.py DEFMSG_H REFERENCE_DLL

Each short key and long format in defmsg.h is a C string literal compiled into
msmpi.dll. Exact presence in Microsoft's binary is independent evidence that the
generator reproduced the catalog. The report lists anything missing in either
direction instead of averaging it away.
"""
import json
from pathlib import Path
import re
import sys

# Reference strings that are not catalog keys, with the reason each is expected.
NON_CATALOG_KEYS = {'**user': 'direct literal prefix comparison in src/mpi/common/errutil.cpp (MPIR_Err user-class codes)'}
LITERAL = re.compile(r'char (short|long)_spc(\d+)\[\]\s+= "((?:[^"\\]|\\.)*)";')
ESCAPES = {'n': '\n', 't': '\t', 'r': '\r', '\\': '\\', '"': '"', "'": "'", '0': '\0', 'a': '\a'}


def Unescape(text):
    return re.sub(r'\\(.)', lambda match: ESCAPES.get(match.group(1), '\\' + match.group(1)), text)


def Main(defmsg, reference):
    header = Path(defmsg).read_bytes().decode('latin-1')
    binary = Path(reference).read_bytes()
    strings = set(re.findall(rb'[\x20-\x7e\t\n]{4,}(?=\x00)', binary))
    pairs = {}
    for kind, number, value in LITERAL.findall(header):
        pairs.setdefault(int(number), {})[kind] = Unescape(value)
    missing = {'short': [], 'long': []}
    empty_long = []
    for number, pair in sorted(pairs.items()):
        for kind in ('short', 'long'):
            if kind == 'long' and pair[kind] == '':
                empty_long.append(pair['short'])  # an empty literal cannot be located by content
                continue
            if pair[kind].encode('latin-1') not in strings:
                missing[kind].append(pair[kind])
    keys_in_reference = {s.decode('latin-1') for s in strings if s.startswith(b'**')}
    generated_keys = {pair['short'] for pair in pairs.values()}
    unexplained = sorted(keys_in_reference - generated_keys - set(NON_CATALOG_KEYS))
    report = dict(entries=len(pairs), missing_short=len(missing['short']), missing_long=len(missing['long']),
                  empty_long_not_checkable=empty_long, reference_keys=len(keys_in_reference),
                  reference_non_catalog_keys=NON_CATALOG_KEYS, reference_keys_not_generated=unexplained,
                  missing_short_examples=missing['short'][:20], missing_long_examples=missing['long'][:20])
    print(json.dumps(report, indent=2))
    return 0 if not missing['short'] and not missing['long'] and not report['reference_keys_not_generated'] else 1


if __name__ == '__main__':
    sys.exit(Main(*sys.argv[1:3]))
