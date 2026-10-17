import numpy as np
import matplotlib.pyplot as plt

# Constants
k_B = 8.617333262e-5  # Boltzmann constant [eV/K]
I_Cs = 3.893          # Ionization energy of Cesium [eV]
g_ratio = 1.0 / 2.0   # Statistical weight ratio (g_ion / g_atom) for Cs

def saha_langmuir_efficiency(T, phi):
    """Calculate ionization efficiency based on Saha-Langmuir equation."""
    alpha = g_ratio * np.exp((phi - I_Cs) / (k_B * T))
    return alpha / (1.0 + alpha)

# Independent variable
T = np.linspace(270, 2500, 500)  # Temperature array [K]

# Constant parameter
phi_Mo = 4.36  # Work function of clean Molybdenum [eV]

# Calculate efficiency
efficiency_T = saha_langmuir_efficiency(T, phi_Mo)

# Plot
plt.figure(figsize=(8, 5))
plt.plot(T, efficiency_T * 100, label=f'Mo Surface ({phi_Mo} eV)', color='blue')
plt.title('Cesium Ionization Efficiency vs. Surface Temperature')
plt.xlabel('Surface Temperature $T$ (K)')
plt.ylabel('Ionization Efficiency (%)')
plt.grid(True, which='both', linestyle='--', alpha=0.7)
plt.legend()
plt.tight_layout()
plt.show()