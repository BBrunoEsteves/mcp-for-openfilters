"""Build the OpenFilters C++ extensions (_abeles and _moremath).

Package metadata lives in pyproject.toml. If the extensions cannot be
compiled, OpenFilters falls back to its (slower) pure-Python implementation,
so the build failure is reported but not fatal.
"""

import glob
import os
import sys

from setuptools import Extension, setup
from setuptools.command.build_ext import build_ext

CORE = os.path.join("src", "openfilters_core")


def sources(directory):
    return sorted(glob.glob(os.path.join(CORE, directory, "*.cpp")))


if sys.platform == "win32":
    # The module definitions use designated initializers (C++20 in MSVC).
    compile_args = ["/O2", "/EHsc", "/std:c++20"]
else:
    compile_args = ["-O3"]
# M_PI is not defined by MSVC's <cmath> without this.
macros = [("_USE_MATH_DEFINES", None)]

extensions = [
    Extension("openfilters_core.abeles._abeles", sources(os.path.join("abeles", "_abeles")),
              extra_compile_args=compile_args, define_macros=macros, language="c++"),
    Extension("openfilters_core.moremath._moremath", sources(os.path.join("moremath", "_moremath")),
              extra_compile_args=compile_args, define_macros=macros, language="c++"),
]


class optional_build_ext(build_ext):
    def run(self):
        try:
            super().run()
        except Exception as error:
            self.warn(f"C++ extensions not built ({error}); using the pure-Python implementation.")

    def build_extension(self, ext):
        try:
            super().build_extension(ext)
        except Exception as error:
            self.warn(f"{ext.name} not built ({error}); using the pure-Python implementation.")


setup(ext_modules=extensions, cmdclass={"build_ext": optional_build_ext})
