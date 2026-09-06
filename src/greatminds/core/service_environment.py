"""EnvironmentFile serialization and static inspection (no shell expansion).

See systemd.exec(5), EnvironmentFile. Malformed quoting is rejected rather than
silently turning an incomplete credential into a different value.
"""
import re

_NAME = re.compile(r'[A-Za-z_][A-Za-z0-9_]*\Z')
_SPACE = ' \t\r'


def encode_environment(values: dict[str, str]) -> str:
    lines = []
    for name, value in sorted(values.items()):
        if not _NAME.fullmatch(name) or '\0' in value:
            raise ValueError('invalid environment name or NUL in value')
        value.encode('utf-8')
        escaped = value.replace('\\', '\\\\').replace('"', '\\"')
        lines.append(f'{name}="{escaped}"\n')
    return ''.join(lines)


def decode_environment(text: str) -> dict[str, str]:
    if '\0' in text:
        raise ValueError('NUL in environment file')
    result = {}
    pos = 0
    while pos < len(text):
        end = text.find('\n', pos)
        end = len(text) if end < 0 else end
        line = text[pos:end].lstrip(_SPACE)
        if not line or line.startswith(('#', ';')) or '=' not in line:
            pos = end + 1
            continue
        split = text.index('=', pos, end)
        name = text[pos:split].strip(_SPACE)
        pos = split + 1
        value = []
        mode = 'leading'
        trim = None
        while pos < len(text):
            char = text[pos]
            pos += 1
            if mode in ('leading', 'plain') and char == '\n':
                break
            if mode == 'leading':
                if char in _SPACE:
                    continue
                if char in "'\"":
                    mode = char
                    continue
                mode = 'plain'
            elif mode in ("'", '"') and char == mode:
                mode = 'leading'
                continue
            if char == '\\' and mode != "'":
                if pos == len(text):
                    raise ValueError('incomplete environment escape')
                following = text[pos]
                pos += 1
                if following == '\n':
                    continue
                if mode == '"' and following not in '\\"$`':
                    value.append('\\')
                value.append(following)
                trim = None
            else:
                if mode == 'plain' and char in _SPACE:
                    if trim is None:
                        trim = len(value)
                else:
                    trim = None
                value.append(char)
        if mode in ("'", '"'):
            raise ValueError('unclosed environment quote')
        if trim is not None and mode == 'plain':
            value = value[:trim]
        if _NAME.fullmatch(name):
            result[name] = ''.join(value)
    return result
