"""Build the isotope list the Mass Scan panel names peaks with (needs: pip install mendeleev).
Run from anywhere: python src/utils/build_element_database.py"""
import json
import os

OUTPUT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "gui", "control", "widgets", "mass_scan",
                                      "isotope_db.json"))


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

    output_path = OUTPUT
    with open(output_path, "w") as f:
        json.dump(db, f, indent=4)

    print(f"Success! Exported {len(db)} isotopes to {output_path}")


if __name__ == "__main__":
    build_isotope_db()