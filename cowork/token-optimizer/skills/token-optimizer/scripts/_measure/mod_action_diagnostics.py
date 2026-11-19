"""Small local diagnostic log for mod action failures."""
import json
import os
import time

MAX_BYTES = 65536


def log(directory, details):
    try:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / 'mod-actions.log'
        line = json.dumps({'timestamp': time.time(), **details}, separators=(',', ':')) + '\n'
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        with os.fdopen(fd, 'a', encoding='utf-8') as stream:
            if stream.tell() + len(line.encode('utf-8')) > MAX_BYTES:
                stream.close()
                fd = os.open(str(path), os.O_WRONLY | os.O_TRUNC | getattr(os, 'O_NOFOLLOW', 0))
                with os.fdopen(fd, 'w', encoding='utf-8') as replacement:
                    replacement.write(line)
                return
            stream.write(line)
    except OSError:
        pass
