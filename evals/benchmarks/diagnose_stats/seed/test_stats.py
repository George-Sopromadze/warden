from stats import mean, variance, stdev


def test_mean():
    assert mean([2, 4, 6]) == 4


def test_variance():
    assert variance([1, 2, 3]) == 2 / 3


def test_stdev():
    assert round(stdev([2, 4, 4, 4, 5, 5, 7, 9]), 4) == 2.0
