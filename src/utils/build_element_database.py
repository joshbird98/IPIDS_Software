import json


def build_isotope_db():
    try:
        from mendeleev import element
    except ImportError:
        print("Error: mendeleev is not installed. Run 'pip install mendeleev'")
        return

    db = []
    print("Querying Mendeleev SQL Database...")
    for z in range(1, 84):
        try:
            el = element(z)
            sym = el.symbol
            for iso in el.isotopes:
                # Exclude trace noise isotopes to limit combinatorial explosion
                if iso.abundance is not None and iso.abundance > 0.01:
                    frac = iso.abundance / 100.0 if iso.abundance > 1.0 else iso.abundance
                    mass = int(round(iso.mass))
                    db.append({
                        'mass': mass,
                        'html': f"<sup>{mass}</sup>{sym}",
                        'plain': f"{mass}{sym}",
                        'sym': sym,
                        'prob': frac
                    })
        except Exception as e:
            print(f"Skipping Z={z}: {e}")

    output_path = "../gui/control/widgets/isotope_db.json"
    with open(output_path, "w") as f:
        json.dump(db, f, indent=4)

    print(f"Success! Exported {len(db)} isotopes to {output_path}")


if __name__ == "__main__":
    build_isotope_db()