import math
from statistics import NormalDist

import numpy as np
import pytest

from tbot.research.statistics import (
    daily_sharpe,
    deflated_sharpe,
    expected_max_sharpe,
    moments,
)


def test_moments() -> None:
    skew, kurt = moments(np.array([-1.0, 1.0, -1.0, 1.0]))
    assert skew == pytest.approx(0.0)
    assert kurt == pytest.approx(1.0)
    assert moments(np.array([2.0, 2.0, 2.0])) == (0.0, 3.0)
    skew, _ = moments(np.array([0.0, 0.0, 0.0, 10.0]))
    assert skew > 0


def test_expected_max_sharpe_grows_with_trials() -> None:
    assert expected_max_sharpe(0.01, 1) == 0.0
    assert expected_max_sharpe(0.0, 50) == 0.0
    few, many = expected_max_sharpe(0.01, 5), expected_max_sharpe(0.01, 500)
    assert 0 < few < many


def test_deflated_sharpe_single_trial_matches_normal_cdf() -> None:
    # One trial: no deflation. Normal returns: skew 0, kurtosis 3.
    z = 0.1 * math.sqrt(100) / math.sqrt(1 + 0.1**2 / 2)
    assert deflated_sharpe(0.1, 101, 0.0, 3.0, 0.0, 1) == pytest.approx(NormalDist().cdf(z))


def test_deflated_sharpe_falls_with_more_trials() -> None:
    honest = deflated_sharpe(0.1, 500, 0.0, 3.0, 0.002, 1)
    mined = deflated_sharpe(0.1, 500, 0.0, 3.0, 0.002, 1000)
    assert mined < honest
    assert math.isnan(deflated_sharpe(math.nan, 500, 0.0, 3.0, 0.0, 1))


def test_daily_sharpe() -> None:
    assert daily_sharpe(math.sqrt(365)) == pytest.approx(1.0)


def test_expected_max_sharpe_matches_the_known_maximum_of_normals() -> None:
    # The expected maximum of 100 standard normals is about 2.508; the paper's
    # approximation gives 2.531.
    assert expected_max_sharpe(1.0, 100) == pytest.approx(2.5306029, rel=1e-6)
    assert expected_max_sharpe(1.0, 100) == pytest.approx(2.508, abs=0.05)


def test_deflated_sharpe_counts_every_trial() -> None:
    from tbot.research.validate import _deflated

    returns = np.random.default_rng(7).normal(0.001, 0.02, 1000)
    trials = [1.0, 0.5, math.nan, 1.5, 0.2, -0.3, 0.8, 1.1, 0.0, 0.6]  # NaN: found nothing
    result = _deflated(returns, trials)
    assert result.trials == 10
    assert result.probability == pytest.approx(0.0128119, rel=1e-4)
    assert result.expected_max_sharpe == pytest.approx(0.8975057, rel=1e-6)
