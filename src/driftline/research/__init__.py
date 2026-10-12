"""Research package: fair comparison, temporal validation, experiments."""

from driftline.research.comparison import compare_strategies
from driftline.research.experiments import ExperimentRecord, build_experiment
from driftline.research.splits import run_split_test
from driftline.research.walk_forward import run_walk_forward

__all__ = [
    "ExperimentRecord",
    "build_experiment",
    "compare_strategies",
    "run_split_test",
    "run_walk_forward",
]
