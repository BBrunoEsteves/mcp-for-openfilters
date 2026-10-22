# __init__.py
# 
# Initialize the moremath module. Try to load the moremath dll. If it
# fails, load Python versions instead.
# 
# Copyright (c) 2005-2007 Stephane Larouche.
# 
# This file is part of OpenFilters.
# 
# OpenFilters is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or (at
# your option) any later version.
#
# OpenFilters is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 59 Temple Place, Suite 330, Boston, MA 02111-1307
# USA


# Try to import the dll.
moremath_dll_import_failed = False
try:
	# The C++ source folder has the same name as the compiled module and
	# would otherwise be imported as an empty namespace package.
	import os as _os
	if _os.environ.get("OPENFILTERS_PURE_PYTHON"):
		raise ImportError("pure-Python mode requested")
	import _moremath as _dll
	if getattr(_dll, "__file__", None) is None:
		raise ImportError("_moremath is not compiled")
	from _moremath import *
except ImportError:
	moremath_dll_import_failed = True

# If the importation of the dll failed, load the Python versions.
if moremath_dll_import_failed:
	import Levenberg_Marquardt
	import QR
	import roots

# Those modules are not implemented in C.
import Gauss_Jordan
import integration
import interpolation
import least_squares
import limits
import linear_algebra



########################################################################
#                                                                      #
# get_moremath_dll_import_success                                      #
#                                                                      #
########################################################################
def get_moremath_dll_import_success():
	"""Get if the moremath dll was successfully imported"""
	
	return not moremath_dll_import_failed
