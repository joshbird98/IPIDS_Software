import numpy as np
import matplotlib.pyplot as plt
from scipy.integrate import solve_ivp

# --- Global Constants ---
SIGMA = 5.6703e-8  # W/(m^2 K^4)
T_ROOM = 293.15    # K

# --- Material Parameters ---
# Molybdenum (3.0g)
m_mo, A_mo, cp_mo, eps_mo = 0.003, 0.0025, 330.0, 0.25
# Alumina (15.6g)
m_al, A_al, cp_al, eps_al = 0.0156, 0.0019, 1100.0, 0.5

# --- Power Profile Definition ---
np.random.seed(42) # Set seed for reproducible power steps

# Phase durations (seconds):
# 0 to 60 mins: 300W (Reach steady state)
# 60 to 180 mins: Random 30-minute intervals between 250W and 350W
time_intervals = [3600, 5400, 7200, 9000, 10800]
power_levels = [300.0] + [np.random.uniform(250, 350) for _ in range(4)]

def get_power(t):
    for i, t_end in enumerate(time_intervals):
        if t < t_end:
            return power_levels[i]
    return power_levels[-1]

# --- Differential Equations ---
def mo_deriv(t, T):
    P_rad = eps_mo * SIGMA * A_mo * (T[0]**4 - T_ROOM**4)
    dTdt = (get_power(t) - P_rad) / (m_mo * cp_mo)
    return [dTdt]

def al_deriv(t, T):
    P_rad = eps_al * SIGMA * A_al * (T[0]**4 - T_ROOM**4)
    dTdt = (get_power(t) - P_rad) / (m_al * cp_al)
    return [dTdt]

# --- Numerical Integration ---
t_total = time_intervals[-1]
t_eval = np.linspace(0, t_total, 5000)

# max_step=5.0 ensures the solver doesn't step over the discrete power changes
sol_mo = solve_ivp(mo_deriv, [0, t_total], [T_ROOM], t_eval=t_eval, max_step=5.0)
sol_al = solve_ivp(al_deriv, [0, t_total], [T_ROOM], t_eval=t_eval, max_step=5.0)

# Convert outputs for plotting
T_mo_C = sol_mo.y[0] - 273.15
T_al_C = sol_al.y[0] - 273.15
time_mins = t_eval / 60.0
power_vals = [get_power(t) for t in t_eval]

# --- Plotting ---
fig, ax1 = plt.subplots(figsize=(10, 6))

# Plot Temperatures on primary Y-axis
ax1.plot(time_mins, T_mo_C, label='Molybdenum (3.0g)', color='blue', linewidth=2)
ax1.plot(time_mins, T_al_C, label='Alumina (15.6g)', color='red', linewidth=2)
ax1.set_xlabel('Time (Minutes)')
ax1.set_ylabel('Temperature (°C)', color='black')
ax1.tick_params(axis='y', labelcolor='black')
ax1.grid(True, which='both', linestyle='--', alpha=0.7)

# Plot Input Power on secondary Y-axis
ax2 = ax1.twinx()
ax2.plot(time_mins, power_vals, label='Input Power (W)', color='green', linestyle=':', alpha=0.8, linewidth=2)
ax2.set_ylabel('Input Power (W)', color='green')
ax2.tick_params(axis='y', labelcolor='green')

# Aggregate legends from both axes
lines_1, labels_1 = ax1.get_legend_handles_labels()
lines_2, labels_2 = ax2.get_legend_handles_labels()
ax1.legend(lines_1 + lines_2, labels_1 + labels_2, loc='lower right')

plt.title('Thermal Response to Fluctuating Power: Molybdenum vs. Alumina')
fig.tight_layout()
plt.show()