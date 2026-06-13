from calculator import average, median


def test_average_basic():
    assert average([2, 4, 6]) == 4


def test_average_single():
    assert average([5]) == 5


def test_median_odd():
    assert median([3, 1, 2]) == 2
