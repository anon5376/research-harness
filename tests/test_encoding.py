import ast
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
NOT_FILES = {"tarfile", "webbrowser", "zipfile", "os"}  # their .open is not a text file open (os.open is raw)
MODULE_OPENS = {"os", "gzip", "io"}  # X.open(file, mode) and os.fdopen(fd, mode): the mode is the second argument


def text_io_without_encoding(path):
    """Calls that turn bytes into text with the platform's default encoding, cp1252 on Windows, not UTF-8:
    read_text and write_text, open and os.fdopen in text mode, gzip.open in "rt" or "wt" mode (it defaults to
    binary), and subprocess pipes with text=True or universal_newlines=True."""
    found = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if not isinstance(node, ast.Call):
            continue
        f, kw = node.func, {k.arg: k.value for k in node.keywords}
        if "encoding" in kw:
            continue
        name = f.attr if isinstance(f, ast.Attribute) else f.id if isinstance(f, ast.Name) else None
        owner = f.value.id if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) else None
        args = node.args[1:] if isinstance(f, ast.Name) or owner in MODULE_OPENS else node.args
        mode = kw.get("mode", args[0] if args else None)
        mode = mode.value if isinstance(mode, ast.Constant) and isinstance(mode.value, str) else None
        if name in ("read_text", "write_text"):
            found.append(node.lineno)
        elif any(isinstance(kw.get(k), ast.Constant) and kw[k].value is True for k in ("text", "universal_newlines")):
            found.append(node.lineno)
        elif owner == "gzip" and name == "open":
            if mode is not None and "t" in mode:
                found.append(node.lineno)
        elif name == "fdopen" or name == "open" and owner not in NOT_FILES:
            if mode is None or "b" not in mode:
                found.append(node.lineno)
    return sorted(found)


class EncodingTest(unittest.TestCase):
    def test_every_text_file_and_pipe_in_rh_is_utf8(self):
        # Windows defaults to cp1252: a job brief with "é" came back as "Ã©" until contract.load said UTF-8.
        bad = {str(p.relative_to(ROOT)): lines for p in sorted((ROOT / "rh").rglob("*.py"))
               if (lines := text_io_without_encoding(p))}
        self.assertEqual(bad, {})

    def test_the_scan_catches_a_default_encoding(self):
        sample = ROOT / "tests" / "fakes" / "_encoding_sample.py"
        sample.write_text('from pathlib import Path\nPath("a").read_text()\nopen("b", "w")\nopen("c", "rb")\n'
                          'Path("d").read_text(encoding="utf-8")\n'
                          'subprocess.run(["git"], capture_output=True, text=True)\n'
                          'subprocess.run(["git"], text=True, encoding="utf-8")\n'
                          'gzip.open("e", "rt")\ngzip.open("f")\nos.fdopen(3, "w")\nos.open("g", 0)\n'
                          'Popen(["git"], universal_newlines=True)\n', encoding="utf-8")
        try:
            self.assertEqual(text_io_without_encoding(sample), [2, 3, 6, 8, 10, 12])
        finally:
            sample.unlink()


if __name__ == "__main__":
    unittest.main()
