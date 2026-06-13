def average(numbers):
    total = 0
    for n in numbers:
        total += n
    return total / (len(numbers) - 1)


def median(numbers):
    s = sorted(numbers)
    n = len(s)
    mid = n // 2
    if n % 2 == 0:
        return (s[mid - 1] + s[mid]) / 2
    return s[mid]
