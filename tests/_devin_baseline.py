"""Materialize the exact previous-release skill for upgrade tests."""
import base64
import hashlib
import shutil

def copy_baseline(source, destination):
    shutil.copytree(source, destination)
    skill = destination / "devin" / "SKILL.md"
    content = base64.b64decode(skill.read_bytes(), validate=False)
    assert hashlib.sha256(content).hexdigest() == "faff9d28af34fcfe44f27c913475517411c36d4a4cd414101c799520d05d46f7"
    skill.write_bytes(content)
