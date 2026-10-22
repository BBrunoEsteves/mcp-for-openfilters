"""Port the OpenFilters calculation core from Python 2 to Python 3.

Usage: python tools/port_from_py2.py ORIGINAL_DIR PY3_FORK_DIR OUTPUT_DIR

Starts from the original OpenFilters 1.1.1 sources, drops the GUI and
packaging files, applies mechanical Python 3 conversions, then a small list
of targeted manual fixes. The C++ extension sources are taken from the
Python 3 fork (EmilKleijn/OpenFilters), whose C API port is reused as-is.

Every change is listed here so the port can be audited and re-run.
"""

import pathlib
import re
import shutil
import sys

EXCLUDE = {"GUI", "installer.py", "setup.py", "Filters.py", "about.py",
           "tests.py", "test_derivatives.py",
           "make_dmg.applescript", "dmg_background.png", "OpenFilters.icns", ".git"}

# --- mechanical conversions, applied to every .py file ---------------------

MECHANICAL = [
    # except X, e:  /  except (X, Y), e:   ->   except ... as e:
    (re.compile(r"^(\s*except\s+)(\([^)]*\)|[\w.]+)\s*,\s*(\w+)\s*:", re.M), r"\1\2 as \3:"),
    (re.compile(r"\.iteritems\(\)"), ".items()"),
    (re.compile(r"\.iterkeys\(\)"), ".keys()"),
    (re.compile(r"\.itervalues\(\)"), ".values()"),
    # x = d.keys()  (later sorted in place)   ->   x = list(d.keys())
    (re.compile(r"^(\s*\w+\s*=\s*)([\w.]+\.keys\(\))\s*$", re.M), r"\1list(\2)"),
    # x = range(...)  (later indexed/mutated) ->   x = list(range(...))
    (re.compile(r"^(\s*[\w.]+\s*=\s*)range\((.*)\)\s*$", re.M), r"\1list(range(\2))"),
    (re.compile(r"^import ConfigParser$", re.M), "import configparser as ConfigParser"),
    # OpenFilters data and project files are Latin-1; Python 2 read bytes, so
    # make the encoding explicit instead of depending on the OS locale.
    (re.compile(r'\bopen\((\w+)(?:, "r")?\)'), r'open(\1, "r", encoding="latin-1")'),
    (re.compile(r'\bopen\((\w+), "w"\)'), r'open(\1, "w", encoding="latin-1", errors="replace")'),
]

# Also matches one-line forms such as "if error: print 'message'".
PRINT_STMT = re.compile(r"^(\s*(?:if [^:]*:\s*)?)print\b(?!\s*\()(.*)$")


def convert_print(line):
    m = PRINT_STMT.match(line)
    if not m:
        return line
    prefix, rest = m.group(1), m.group(2).strip()
    if rest.startswith(">>"):
        raise ValueError("print >> not handled: " + line)
    if not rest:
        return f"{prefix}print()"
    if rest.endswith(","):
        return f"{prefix}print({rest[:-1].rstrip()}, end=' ')"
    return f"{prefix}print({rest})"


def expand_py2_tabs(text):
    """Python 2 treated a tab in indentation as advancing to the next multiple
    of 8 columns; Python 3 rejects ambiguous mixes. Expanding the leading
    whitespace with tab size 8 reproduces the Python 2 block structure."""
    out = []
    for line in text.split("\n"):
        stripped = line.lstrip(" \t")
        out.append(line[:len(line) - len(stripped)].expandtabs(8) + stripped)
    return "\n".join(out)


# --- targeted manual fixes: (file, old, new) --------------------------------

MANUAL = [
    ("__init__.py", '"moremath".', '"moremath",'),
    # Py2 "except OSError, WindowsError:" bound the exception to the name
    # WindowsError; the intent is clearly to catch both.
    ("project.py", "except OSError as WindowsError:", "except OSError:"),
    ("version.py", "import string\n", ""),
    ("version.py", "string.split(number, \".\")", "number.split(\".\")"),
    # imp was removed in Python 3.12.
    ("modules.py", "import imp\n", "import importlib.util\n"),
    ("modules.py",
     "\t\t\tfile, path_name, description = imp.find_module(self.filename, [self.path])",
     "\t\t\tpath_name = os.path.join(self.path, self.filename + \".py\")\n"
     "\t\t\tspec = importlib.util.spec_from_file_location(self.filename, path_name)\n"
     "\t\t\tfile = None"),
    ("modules.py",
     "\t\t\t\tself.module = imp.load_module(self.filename, file, path_name, description)",
     "\t\t\t\tself.module = importlib.util.module_from_spec(spec)\n"
     "\t\t\t\tspec.loader.exec_module(self.module)"),
    # exec() can no longer create local variables.
    ("modules.py", "exec((\"submodule_function = self.module.\" + submodule_description[1]))",
     "submodule_function = getattr(self.module, submodule_description[1])"),
    ("modules.py", "exec((\"submodule_dialog = self.module.\" + submodule_description[2]))",
     "submodule_dialog = getattr(self.module, submodule_description[2])"),
]

# The C++ source folders share the name of the compiled extension and would
# otherwise be imported as empty namespace packages, silently disabling the
# pure-Python fallback.
EXT_DETECT = """try:
\t# The C++ source folder has the same name as the compiled module and
\t# would otherwise be imported as an empty namespace package.
\timport os as _os
\tif _os.environ.get("OPENFILTERS_PURE_PYTHON"):
\t\traise ImportError("pure-Python mode requested")
\timport {mod} as _dll
\tif getattr(_dll, "__file__", None) is None:
\t\traise ImportError("{mod} is not compiled")
\tfrom {mod} import *
except ImportError as _import_error:
\t# Say why a compiled core that exists could not be used (stderr only:
\t# stdout may be a protocol channel).
\tif "requested" not in str(_import_error) and "not compiled" not in str(_import_error):
\t\timport sys as _sys
\t\t_sys.stderr.write("OpenFilters: compiled {mod} could not be loaded (%s); "
\t\t                  "using the slower pure-Python implementation\\n" % _import_error)"""
MANUAL += [(f"{pkg}/__init__.py", f"try:\n\tfrom {mod} import *\nexcept ImportError:", EXT_DETECT.format(mod=mod))
           for pkg, mod in [("abeles", "_abeles"), ("moremath", "_moremath")]]


# Directories that OpenFilters' launcher (Filters.py) appended to sys.path, in
# order, after the main directory.
SEARCH_DIRS = ["", "abeles", "color", "config", "GUI", "materials", "modules", "moremath"]

IMPORT_LINE = re.compile(r"^(\s*)import\s+([\w.]+(?:\s+as\s+\w+)?(?:\s*,\s*[\w.]+(?:\s+as\s+\w+)?)*)\s*(#.*)?$")
FROM_LINE = re.compile(r"^(\s*)from\s+([\w.]+)\s+import\s+(.*)$")


def make_relative_imports(output):
    """Turn OpenFilters' bare imports (which relied on sys.path entries and on
    Python 2 implicit relative imports) into explicit package-relative
    imports, so the core can live inside an installable package without
    shadowing or being shadowed by other top-level modules.

    Resolution follows Python 2: a module inside a package first sees its
    siblings, then the sys.path entries set up by Filters.py."""

    def provides(directory, name):
        d = output / directory
        return (d / f"{name}.py").is_file() or (d / name / "__init__.py").is_file() or \
            any(d.glob(f"{name}.*.pyd")) or (directory == "abeles" and name == "_abeles") or \
            (directory == "moremath" and name == "_moremath")

    def resolve(file_dir, name):
        """Return the directory (relative to the core root) holding `name`,
        or None for standard-library/third-party modules."""
        if file_dir and provides(file_dir, name):
            return file_dir
        for directory in SEARCH_DIRS:
            if provides(directory, name):
                return directory
        return None

    def relative(file_dir, target_dir):
        if target_dir == file_dir:
            return "."
        return ("." if not file_dir else "..") + target_dir

    for path in output.rglob("*.py"):
        file_dir = path.parent.relative_to(output).as_posix()
        file_dir = "" if file_dir == "." else file_dir
        lines = path.read_text(encoding="utf-8").split("\n")
        out = []
        for line in lines:
            m = FROM_LINE.match(line)
            if m and not m.group(2).startswith((".", "__future__")):
                indent, module, names = m.groups()
                head, _, tail = module.partition(".")
                where = resolve(file_dir, head)
                if where is not None:
                    base = relative(file_dir, where)
                    sep = "" if base.endswith(".") else "."
                    out.append(f"{indent}from {base}{sep}{module} import {names}")
                    continue
            m = IMPORT_LINE.match(line)
            if m:
                indent, modules, comment = m.groups()
                parts = [p.strip() for p in modules.split(",")]
                rewritten, kept = [], []
                for part in parts:
                    name, _, alias = part.partition(" as ")
                    name, alias = name.strip(), alias.strip()
                    where = resolve(file_dir, name) if "." not in name else None
                    if where is None:
                        kept.append(part)
                    else:
                        imported = f"{name} as {alias}" if alias else name
                        rewritten.append(f"{indent}from {relative(file_dir, where)} import {imported}")
                if rewritten:
                    if kept:
                        out.append(f"{indent}import {', '.join(kept)}")
                    out.extend(rewritten)
                    continue
            out.append(line)
        path.write_text("\n".join(out), encoding="utf-8", newline="\n")


def port_moremath_cpp(directory):
    """Port the _moremath C++ wrappers to the Python 3 C API (the fork only
    ported _abeles)."""
    init_module = re.compile(r'(\w+) = Py_InitModule\("([\w.]+)", (\w+)\);')
    for path in directory.glob("*.cpp"):
        text = path.read_text(encoding="latin-1")
        text = init_module.sub(
            r'static struct PyModuleDef \1_def = {PyModuleDef_HEAD_INIT, "\2", NULL, -1, \3};'
            r'\n\t\1 = PyModule_Create(&\1_def);', text)
        text = re.sub(r"PyObject_HEAD_INIT\(NULL\)\s*\n\s*0,\s*/\* ob_size \*/",
                      "PyVarObject_HEAD_INIT(NULL, 0)", text)
        text = text.replace("self->ob_type->tp_free", "Py_TYPE(self)->tp_free")
        text = text.replace("PyInt_AsLong", "PyLong_AsLong")
        text = text.replace("PyCObject_Check(answer)", "PyCapsule_CheckExact(answer)")
        text = re.sub(r"PyCObject_AsVoidPtr\((\w+)\)", r"PyCapsule_GetPointer(\1, NULL)", text)
        if path.name == "_moremath.cpp":
            text = text.replace("PyMODINIT_FUNC init_moremath()", "PyMODINIT_FUNC PyInit__moremath()")
            text = text.replace("if (!package) return;", "if (!package) return NULL;")
            text = text.replace("if (PyErr_Occurred()) return;", "if (PyErr_Occurred()) return NULL;")
            text = text.replace('PyModule_AddObject(package, "roots", roots_module);\n',
                                'PyModule_AddObject(package, "roots", roots_module);\n\n\treturn package;\n')
        for leftover in ("Py_InitModule", "PyInt_", "PyString_", "PyCObject", "ob_type"):
            if leftover in text:
                raise SystemExit(f"unported {leftover} in {path}")
        path.write_text(text, encoding="latin-1", newline="\n")
    (directory / "_moremath.def").write_text(
        (directory / "_moremath.def").read_text().replace("init_moremath", "PyInit__moremath"))


INIT = '''"""Calculation core of OpenFilters 1.1.1 (Larouche and Martinu, Polytechnique
Montreal), ported to Python 3 without the GUI. See PROVENANCE.md."""
'''


def main(original, fork, output):
    original, fork, output = map(pathlib.Path, (original, fork, output))
    # Empty the output folder rather than deleting it (Windows refuses to
    # delete a folder that is some process's working directory).
    output.mkdir(parents=True, exist_ok=True)
    # Compiled extensions (and DLLs they need) are kept: they are built
    # separately and are not produced by this script.
    compiled = {p: p.read_bytes() for p in output.rglob("*") if p.suffix in (".pyd", ".so", ".dll")}
    for child in output.iterdir():
        shutil.rmtree(child) if child.is_dir() else child.unlink()
    shutil.copytree(original, output, dirs_exist_ok=True,
                    ignore=lambda d, names: [n for n in names if n in EXCLUDE])

    for path in output.rglob("*.py"):
        raw = path.read_bytes()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("latin-1")
        text = text.replace("\r\n", "\n")
        for pattern, repl in MECHANICAL:
            text = pattern.sub(repl, text)
        text = "\n".join(convert_print(line) for line in text.split("\n"))
        try:
            compile(text, str(path), "exec")
        except TabError:
            text = expand_py2_tabs(text)
        except SyntaxError:
            pass  # handled by the manual fixes below; checked after
        path.write_text(text, encoding="utf-8", newline="\n")

    for rel, old, new in MANUAL:
        path = output / rel
        text = path.read_text(encoding="utf-8")
        if old not in text:
            raise SystemExit(f"manual fix not applicable in {rel}: {old!r}")
        path.write_text(text.replace(old, new), encoding="utf-8", newline="\n")

    make_relative_imports(output)
    # The original __init__ listed every module (including the GUI) in
    # __all__; the core is used module by module instead.
    (output / "__init__.py").write_text(INIT, encoding="utf-8", newline="\n")

    # C++ extension: Python 3 C API port from the fork.
    shutil.rmtree(output / "abeles" / "_abeles")
    shutil.copytree(fork / "abeles" / "_abeles", output / "abeles" / "_abeles")
    port_moremath_cpp(output / "moremath" / "_moremath")

    for path, data in compiled.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


if __name__ == "__main__":
    main(*sys.argv[1:4])
