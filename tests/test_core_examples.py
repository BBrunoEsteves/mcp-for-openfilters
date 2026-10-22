"""Load, optimize and save the example projects shipped with OpenFilters."""

import os
import time

import pytest

from conftest import CORE
from openfilters_core import project
from openfilters_core.optimization_needles import optimization_needles
from openfilters_core.optimization_refinement import optimization_refinement
from openfilters_core.optimization_steps import optimization_steps

EXAMPLES = os.path.join(CORE, "examples")


class TimeLimit:
    def __init__(self, optimizer, seconds):
        self.optimizer, self.deadline = optimizer, time.time() + seconds

    def update(self, working, status):
        if time.time() > self.deadline:
            self.optimizer.stop()


@pytest.mark.parametrize("name", sorted(os.listdir(EXAMPLES)))
def test_project_round_trip(name, tmp_path):
    original = project.read_project(os.path.join(EXAMPLES, name))
    project.write_project(original, str(tmp_path / name))
    reread = project.read_project(str(tmp_path / name))
    assert reread.get_nb_filters() == original.get_nb_filters()
    assert reread.get_nb_targets() == original.get_nb_targets()
    for a, b in zip(original.get_filters(), reread.get_filters()):
        assert a.get_nb_layers() == b.get_nb_layers()
        assert a.get_total_thickness() == pytest.approx(b.get_total_thickness())


@pytest.mark.parametrize("name, filter_nb, method, min_layers", [
    ("Color.ofp", 0, optimization_needles, 3),
    ("AR.ofp", 0, optimization_refinement, 3),
    ("Beamsplitter.ofp", 0, optimization_needles, 5),
])
def test_example_optimization(name, filter_nb, method, min_layers):
    p = project.read_project(os.path.join(EXAMPLES, name))
    f = p.get_filter(filter_nb).clone()
    optimizer = method(f, p.get_targets())
    optimizer.set_parent(TimeLimit(optimizer, 60))
    if hasattr(optimizer, "set_automatic_mode"):
        optimizer.set_automatic_mode(True)
    optimizer.calculate_values()
    initial_chi_2 = optimizer.get_chi_2() if optimizer.get_chi_2() else float("inf")
    optimizer.go()
    optimizer.copy_to_filter()
    assert optimizer.get_chi_2() < initial_chi_2
    assert f.get_nb_layers() >= min_layers
