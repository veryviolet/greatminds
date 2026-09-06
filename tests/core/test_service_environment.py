import pytest

from greatminds.core.service_environment import decode_environment, encode_environment


@pytest.mark.parametrize('value', ['', 'plain', ' two words ', "it's quoted", '"double"',
    '$HOME `command` %h # literal', 'one\ntwo\n', 'a\\b\\', '\t\r\n', 'токен', '\\"\'\nNEXT=literal'])
def test_captured_values_roundtrip_without_expansion(value):
    assert decode_environment(encode_environment({'TOKEN': value})) == {'TOKEN': value}


def test_systemd_file_rules_differ_from_shell_words():
    text = """# ignored
; ignored
NO_ASSIGNMENT
INVALID KEY=ignored
A= two words # literal comment   
B=foo"bar"
C='one
 two'   
D="literal\\q and \\$HOME and \\`cmd\\`"
E=one\\
two
F=escaped\\ space\\ 
A=last value
"""
    assert decode_environment(text) == {
        'A': 'last value', 'B': 'foo"bar"', 'C': 'one\n two',
        'D': 'literal\\q and $HOME and `cmd`', 'E': 'onetwo', 'F': 'escaped space ',
    }


@pytest.mark.parametrize('text', ['A="unfinished', "A='unfinished", 'A=value\\', 'A=x\0y'])
def test_invalid_input_is_not_silently_reinterpreted(text):
    with pytest.raises(ValueError):
        decode_environment(text)


def test_serialization_with_systemd_255_parser(tmp_path):
    """Independent native parser; optional on hosts without the versioned library."""
    import ctypes
    from pathlib import Path
    import random
    library = Path('/usr/lib/x86_64-linux-gnu/systemd/libsystemd-shared-255.so')
    if not library.exists():
        pytest.skip('systemd 255 shared parser unavailable')
    lib = ctypes.CDLL(str(library))
    vector = ctypes.POINTER(ctypes.c_char_p)
    lib.load_env_file.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.POINTER(vector)]
    lib.load_env_file.restype = ctypes.c_int
    lib.strv_free.argtypes = [vector]
    lib.strv_free.restype = ctypes.c_void_p
    rng = random.Random(255)
    alphabet = 'abc \t\r\n\\\'"$`#=%т'
    values = {f'VALUE_{i}': ''.join(rng.choice(alphabet) for _ in range(80)) for i in range(200)}
    path = tmp_path/'synthetic.env'
    path.write_bytes(encode_environment(values).encode('utf-8'))
    result = vector()
    assert lib.load_env_file(None, str(path).encode(), ctypes.byref(result)) >= 0
    actual = {}
    try:
        i = 0
        while result[i] is not None:
            key, value = result[i].decode().split('=', 1)
            actual[key] = value
            i += 1
    finally:
        lib.strv_free(result)
    assert actual == values
    assert decode_environment(path.read_bytes().decode()) == actual
