import collections

# Replace this string with your new measured Niobium-target SNICS data
raw_data = """26 450nA
38 4nA
40 3nA
50 3.5nA
52 600pA
54 110pA
62 9nA
64 4.5nA
66 300pA
68 90pA
74 1nA
76 500pA
78 120pA
80 400pA
82 60pA
88 600pA
90 80pA
92 18pA
94 30pA
102 18pA"""

# Integer masses for relevant isotopes (Niobium replaces Titanium)
isotopes = {
    '1H': 1, '2H': 2,
    '12C': 12, '13C': 13,
    '14N': 14, '15N': 15,
    '16O': 16, '17O': 17, '18O': 18,
    '93Nb': 93
}

# Empirical limits for sputtered clusters
limits = {'H': 5, 'C': 9, 'N': 7, 'O': 6, 'Nb': 2}


def get_base_element(iso):
    return ''.join([c for c in iso if c.isalpha()])


def format_combo(combo):
    parts = []
    for iso, count in sorted(combo.items()):
        if count == 1:
            parts.append(iso)
        else:
            parts.append(f"{iso}_{count}")
    return " + ".join(parts)


def find_combinations(target):
    results = []
    iso_keys = list(isotopes.keys())

    def search(current_combo, current_sum, index):
        if current_sum == target:
            results.append(dict(current_combo))
            return
        if current_sum > target or index >= len(iso_keys):
            return

        current_iso = iso_keys[index]
        iso_mass = isotopes[current_iso]
        base_elem = get_base_element(current_iso)

        # Calculate current count of the base element in the combination
        current_elem_count = sum(v for k, v in current_combo.items() if get_base_element(k) == base_elem)

        max_possible = min((target - current_sum) // iso_mass, limits[base_elem] - current_elem_count)

        for count in range(max_possible, -1, -1):
            if count > 0:
                current_combo[current_iso] = count

            search(current_combo, current_sum + (count * iso_mass), index + 1)

            if count > 0:
                del current_combo[current_iso]

    search({}, 0, 0)
    return results


def main():
    print(f"{'Mass':<5} | {'Current':<8} | {'Possible Compositions'}")
    print("-" * 120)

    for line in raw_data.strip().split('\n'):
        if not line.strip():
            continue
        parts = line.split()
        mass = int(parts[0])
        current = parts[1] if len(parts) > 1 else "-"

        combos = find_combinations(mass)
        formatted_combos = [format_combo(c) for c in combos]

        if not formatted_combos:
            combo_str = "No combinations found within physical limits"
        elif len(formatted_combos) > 5:
            combo_str = ", ".join(formatted_combos[:5]) + f" ... (+{len(formatted_combos) - 5} more)"
        else:
            combo_str = ", ".join(formatted_combos)

        print(f"{mass:<5} | {current:<8} | {combo_str}")


if __name__ == "__main__":
    main()