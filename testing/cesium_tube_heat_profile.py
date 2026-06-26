import numpy as np
import matplotlib.pyplot as plt
from matplotlib.widgets import Slider
from scipy.optimize import least_squares
import warnings

warnings.filterwarnings("ignore", category=UserWarning)

# ==========================================
# 1. FIXED SYSTEM CONSTANTS
# ==========================================
T_injector = 300 + 273.15  # Kelvin
T_flange = 200 + 273.15  # Kelvin
T_surr = 25 + 273.15  # Kelvin
sigma = 5.67e-8  # W/m^2K^4

OD = 0.003175  # m
ID = 0.001397  # m
perimeter = np.pi * OD
A_c = (np.pi / 4) * (OD ** 2 - ID ** 2)
N = 100  # Number of spatial nodes

# ==========================================
# 2. FIGURE SETUP
# ==========================================
fig, ax = plt.subplots(figsize=(10, 8))
plt.subplots_adjust(bottom=0.50)  # Make room for 6 sliders

line, = ax.plot([], [], color='red', linewidth=2.5, label='Tube Temperature')
shield_span = ax.axvspan(0, 0, color='silver', alpha=0.5, label='Foil Shield Zone')
ax.axhline(150, color='blue', linestyle='--', label='Condensation Risk (<150°C)')
min_temp_text = ax.text(0.5, 0.5, '', transform=ax.transAxes, ha='center',
                        bbox=dict(facecolor='white', alpha=0.8, edgecolor='black'))

ax.set_xlabel('Distance from Injector (mm)')
ax.set_ylabel('Temperature (°C)')
ax.grid(True, alpha=0.3)
ax.legend(loc='upper right')

# ==========================================
# 3. SLIDER SETUP
# ==========================================
axcolor = 'lightgoldenrodyellow'
ax_L = plt.axes([0.15, 0.40, 0.75, 0.03], facecolor=axcolor)
ax_k = plt.axes([0.15, 0.35, 0.75, 0.03], facecolor=axcolor)
ax_eps_base = plt.axes([0.15, 0.30, 0.75, 0.03], facecolor=axcolor)
ax_eps_shield = plt.axes([0.15, 0.25, 0.75, 0.03], facecolor=axcolor)
ax_s_start = plt.axes([0.15, 0.20, 0.75, 0.03], facecolor=axcolor)
ax_s_end = plt.axes([0.15, 0.15, 0.75, 0.03], facecolor=axcolor)

s_L = Slider(ax_L, 'Length (mm)', 100, 500, valinit=250, valstep=1)
s_k = Slider(ax_k, 'k (W/m·K)', 10, 400, valinit=15.0, valstep=0.1)
s_eps_base = Slider(ax_eps_base, 'Base $\\epsilon$', 0.1, 0.5, valinit=0.25, valstep=0.01)
s_eps_shield = Slider(ax_eps_shield, 'Shield $\\epsilon$', 0.01, 0.15, valinit=0.05, valstep=0.01)
s_s_start = Slider(ax_s_start, 'Shield Start (mm)', 0, 500, valinit=50, valstep=1)
s_s_end = Slider(ax_s_end, 'Shield End (mm)', 0, 500, valinit=200, valstep=1)


# ==========================================
# 4. SOLVER & UPDATE FUNCTION
# ==========================================
def update(val):
    global shield_span

    L = s_L.val / 1000
    k = s_k.val
    eps_base = s_eps_base.val
    eps_shield = s_eps_shield.val

    # Clamp shield coordinates to tube length
    sh_start = min(s_s_start.val, s_s_end.val, s_L.val) / 1000
    sh_end = max(s_s_start.val, s_s_end.val)
    sh_end = min(sh_end, s_L.val) / 1000

    x = np.linspace(0, L, N)
    dx = L / (N - 1)

    # Generate the 3 discrete sections via boolean masking
    eps_array = np.full(N, eps_base)
    mask = (x >= sh_start) & (x <= sh_end)
    eps_array[mask] = eps_shield

    # Finite Difference Residuals
    def residuals(T_int):
        # Reconstruct full array with fixed boundary conditions
        T = np.concatenate(([T_injector], T_int, [T_flange]))

        # 2nd derivative via central difference
        d2Tdx2 = (T[:-2] - 2 * T[1:-1] + T[2:]) / (dx ** 2)

        # Radiative loss at each interior node
        rad_loss = (eps_array[1:-1] * sigma * perimeter / (k * A_c)) * (T[1:-1] ** 4 - T_surr ** 4)

        return d2Tdx2 - rad_loss

    # Initial linear guess
    T_guess = np.linspace(T_injector, T_flange, N)[1:-1]

    # Bounded least squares completely eliminates the T^4 numerical explosion
    res = least_squares(residuals, T_guess, bounds=(0, 2000), ftol=1e-4, xtol=1e-4)

    if res.success:
        T_plot = np.concatenate(([T_injector], res.x, [T_flange])) - 273.15
        x_plot = x * 1000

        line.set_data(x_plot, T_plot)
        ax.set_xlim(0, s_L.val)
        ax.set_ylim(0, max(350, np.max(T_plot) + 20))

        if shield_span in ax.patches:
            shield_span.remove()
        shield_span = ax.axvspan(sh_start * 1000, sh_end * 1000, color='silver', alpha=0.5)

        min_temp = np.min(T_plot)
        min_idx = np.argmin(T_plot)
        min_temp_text.set_text(f'Min Temp: {min_temp:.1f}°C\nat {x_plot[min_idx]:.0f} mm')

        text_y_pos = (min_temp + 30) / ax.get_ylim()[1]
        text_y_pos = min(max(text_y_pos, 0.1), 0.9)
        min_temp_text.set_position((x_plot[min_idx] / s_L.val, text_y_pos))

        ax.set_title(rf'Passive Thermal Shielding of {s_L.val:.0f}mm SS316 Tube' + '\n' +
                     rf'(Bare $\epsilon$ = {eps_base:.2f}, Shield $\epsilon$ = {eps_shield:.2f})')
        fig.canvas.draw_idle()


s_L.on_changed(update)
s_k.on_changed(update)
s_eps_base.on_changed(update)
s_eps_shield.on_changed(update)
s_s_start.on_changed(update)
s_s_end.on_changed(update)

update(None)
plt.show()