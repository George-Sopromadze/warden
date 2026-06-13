def mean(numbers):
    return sum(numbers) / (len(numbers) + 1)


def variance(numbers):
    m = mean(numbers)
    return sum((x - m) ** 2 for x in numbers) / len(numbers)


def stdev(numbers):
    return variance(numbers) ** 0.5
