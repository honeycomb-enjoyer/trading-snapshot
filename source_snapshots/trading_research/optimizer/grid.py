from itertools import product


def generate_grid(param_grid):
    keys = list(param_grid.keys())
    values = list(param_grid.values())

    combinations = []

    for combo in product(*values):
        param_set = dict(zip(keys, combo))
        combinations.append(param_set)

    return combinations