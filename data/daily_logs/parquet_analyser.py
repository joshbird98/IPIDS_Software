import pandas as pd
import matplotlib.pyplot as plt
from pathlib import Path


def plot_mass_scan(parquet_path="day_20260818.parquet"):
    if not Path(parquet_path).exists():
        print(f"Error: {parquet_path} not found.")
        return

    time_col = "timestamp"
    faraday_col = "ion_beam.beamline.faraday.smu.rb_current"
    magnet_col = "ion_beam.beamline.magnet.rb_current"

    start_ts = 1787069712
    end_ts = 1787072200

    try:
        df = pd.read_parquet(
            parquet_path,
            columns=[time_col, faraday_col, magnet_col],
            filters=[(time_col, '>=', start_ts), (time_col, '<=', end_ts)]
        )

        if df.empty:
            print(f"No data found between timestamps {start_ts} and {end_ts}.")
            return

        current_offset = 3e-11
        df[faraday_col] += current_offset

        # Bin magnet current to 0.001A
        df['magnet_binned'] = df[magnet_col].round(3)

        # Group by bins to calculate average beam current and point counts
        scan_data = df.groupby('magnet_binned')[faraday_col].agg(['mean', 'count']).reset_index()

        # Print binning stats
        print("--- Binning Statistics ---")
        print(f"Total raw data points: {len(df)}")
        print(f"Number of 0.001A bins: {len(scan_data)}")
        print(f"Average points per bin: {scan_data['count'].mean():.2f}")
        print(f"Max points in a bin: {scan_data['count'].max()}")
        print(f"Min points in a bin: {scan_data['count'].min()}")
        print("--------------------------")

        # Plotting
        fig, ax = plt.subplots(figsize=(12, 6))

        # Plot data in Amps natively to support the 1e-13 minimum threshold
        ax.plot(scan_data['magnet_binned'], scan_data['mean'],
                marker='.', linestyle='-', linewidth=1, color='tab:blue')

        # Symmetrical log scale handles both positive and negative logarithmic values
        # linthresh defines the region around zero where the scale becomes linear to avoid log(0) errors
        ax.set_yscale('symlog', linthresh=1e-12)

        # Note: If negative values should be entirely discarded from the plot view instead of displayed,
        # uncomment the following line to enforce a hard y-axis floor:
        # ax.set_ylim(bottom=1e-13)

        ax.set_xlabel("Magnet Current (A)")
        ax.set_ylabel("Average Faraday Current (A)")
        ax.set_title(f"Mass Scan: Beam Current vs Magnet Current\n(Binned to 0.001A)")

        ax.grid(True, which="major", linestyle='-', alpha=0.7)
        ax.grid(True, which="minor", linestyle=':', alpha=0.4)

        fig.tight_layout()
        plt.show()

        x_data = scan_data['magnet_binned'].values
        y_data = scan_data['mean'].values
        #evaluate_peak_models_weighted(x_data, y_data)
        #evaluate_constrained_models_weighted(x_data, y_data)
        #best_params, n_sub = auto_optimize_peak_shape_weighted(x_data, y_data)
        #result = fit_with_lmfit(x_data, y_data)
        export_arrays_to_csv(x_data, y_data)

    except Exception as e:
        print(f"Execution failed: {e}")


import numpy as np
import matplotlib.pyplot as plt
from scipy.optimize import curve_fit
from scipy.signal import find_peaks
from scipy.special import erf


# --- Mathematical Models ---

def gaussian(x, a, mu, sigma):
    return a * np.exp(-((x - mu) ** 2) / (2 * np.maximum(sigma, 1e-15) ** 2))


def skewed_gaussian(x, a, mu, sigma, alpha):
    """Gaussian multiplied by a Cumulative Distribution Function for skew."""
    norm_pdf = np.exp(-((x - mu) ** 2) / (2 * np.maximum(sigma, 1e-15) ** 2))
    norm_cdf = 0.5 * (1 + erf(alpha * (x - mu) / (np.maximum(sigma, 1e-15) * np.sqrt(2))))
    return a * norm_pdf * norm_cdf


def model_3_gaussians(x, *p):
    return gaussian(x, *p[0:3]) + gaussian(x, *p[3:6]) + gaussian(x, *p[6:9])


def model_3_skewed(x, *p):
    return skewed_gaussian(x, *p[0:4]) + skewed_gaussian(x, *p[4:8]) + skewed_gaussian(x, *p[8:12])


def model_6_gaussians(x, *p):
    """Three peak regions, each composed of a main and a secondary sub-peak (doublet)."""
    return (gaussian(x, *p[0:3]) + gaussian(x, *p[3:6]) +  # Peak 1 doublet
            gaussian(x, *p[6:9]) + gaussian(x, *p[9:12]) +  # Peak 2 doublet
            gaussian(x, *p[12:15]) + gaussian(x, *p[15:18]))  # Peak 3 doublet


# --- Evaluation Metrics ---

def calculate_fit_stats(y_true, y_pred):
    ss_res = np.sum((y_true - y_pred) ** 2)
    ss_tot = np.sum((y_true - np.mean(y_true)) ** 2)
    r2 = 1 - (ss_res / np.maximum(ss_tot, 1e-15))
    rmse = np.sqrt(np.mean((y_true - y_pred) ** 2))
    return r2, rmse


# --- Main Fitting Routine ---

def evaluate_peak_models(x, y):
    """
    Fits 3 different models to the mass-scan data and visualizes the results.
    x: Magnet current array
    y: Binned beam current array (linear, not log)
    """
    # 1. Auto-detect 3 main peaks to generate initial guesses (p0)
    # distance and prominence parameters may need manual tuning based on your specific scan envelope
    peak_indices, _ = find_peaks(y, distance=10, prominence=np.max(y) * 0.01)

    # Sort peaks by height and take the top 3, then sort by X position
    if len(peak_indices) < 3:
        print(f"Warning: find_peaks only located {len(peak_indices)} peaks. Adjust prominence/distance.")
        return

    top_3_indices = sorted(peak_indices[np.argsort(y[peak_indices])][-3:])
    peak_x = x[top_3_indices]
    peak_y = y[top_3_indices]

    # Base width guess based on step size
    w_guess = (np.max(x) - np.min(x)) * 0.01

    # p0 Arrays
    p0_3g = [peak_y[0], peak_x[0], w_guess,
             peak_y[1], peak_x[1], w_guess,
             peak_y[2], peak_x[2], w_guess]

    p0_3s = [peak_y[0], peak_x[0], w_guess, 2.0,  # alpha guess = +2 (positive skew)
             peak_y[1], peak_x[1], w_guess, 2.0,
             peak_y[2], peak_x[2], w_guess, 2.0]

    # Doublet guess: Main peak + secondary peak slightly offset to the right with 30% height
    p0_6g = [peak_y[0], peak_x[0], w_guess, peak_y[0] * 0.3, peak_x[0] + w_guess, w_guess,
             peak_y[1], peak_x[1], w_guess, peak_y[1] * 0.3, peak_x[1] + w_guess, w_guess,
             peak_y[2], peak_x[2], w_guess, peak_y[2] * 0.3, peak_x[2] + w_guess, w_guess]

    models = {
        "3 Standard Gaussians": (model_3_gaussians, p0_3g),
        "3 Skewed Gaussians": (model_3_skewed, p0_3s),
        "6 Gaussians (3 Doublets)": (model_6_gaussians, p0_6g)
    }

    fig, axes = plt.subplots(3, 1, figsize=(12, 12), sharex=True)

    for ax, (name, (model_func, p0)) in zip(axes, models.items()):
        try:
            # maxfev increased for high-parameter models
            popt, _ = curve_fit(model_func, x, y, p0=p0, maxfev=50000)
            y_pred = model_func(x, *popt)

            r2, rmse = calculate_fit_stats(y, y_pred)

            ax.plot(x, y, 'ko', markersize=3, alpha=0.5, label='Raw Data')
            ax.plot(x, y_pred, 'r-', linewidth=2, label=f'Composite Fit')

            ax.set_yscale('symlog', linthresh=1e-13)
            ax.set_title(f"{name} | R² = {r2:.5f} | RMSE = {rmse:.2e}")
            ax.set_ylabel("Current (A)")
            ax.grid(True, alpha=0.3)
            ax.legend(loc='upper right')

        except RuntimeError:
            ax.text(0.5, 0.5, "Fit failed to converge", ha='center', va='center', transform=ax.transAxes)
            print(f"[{name}] Fit failed. The solver could not estimate the covariance matrix.")

    axes[-1].set_xlabel("Magnet Current (A)")
    fig.tight_layout()
    plt.show()


# Example integration with previous script:
# x_data = scan_data['magnet_binned'].values
# y_data = scan_data['mean'].values
# evaluate_peak_models(x_data, y_data)


import numpy as np
import matplotlib.pyplot as plt
from scipy.optimize import curve_fit
from scipy.stats import exponnorm


# --- Mathematical Models ---

def split_gaussian(x, a, mu, sig_left, sig_right):
    """Gaussian with independent left and right standard deviations."""
    return np.where(x < mu,
                    a * np.exp(-((x - mu) ** 2) / (2 * sig_left ** 2)),
                    a * np.exp(-((x - mu) ** 2) / (2 * sig_right ** 2)))


def shared_split_3(x, a1, mu1, a2, mu2, a3, mu3, sig_left, sig_right):
    """3 peaks constrained to share the exact same asymmetric widths."""
    return (split_gaussian(x, a1, mu1, sig_left, sig_right) +
            split_gaussian(x, a2, mu2, sig_left, sig_right) +
            split_gaussian(x, a3, mu3, sig_left, sig_right))


def emg(x, a, mu, sigma, K):
    """
    Exponentially Modified Gaussian using scipy.stats.exponnorm.
    K is the shape parameter (tau/sigma). K > 0 dictates a right-side tail.
    """
    return a * exponnorm.pdf(x, K, loc=mu, scale=sigma)


def shared_emg_3(x, a1, mu1, a2, mu2, a3, mu3, sigma, K):
    """3 peaks constrained to share the exact same Gaussian width and exponential tail."""
    return (emg(x, a1, mu1, sigma, K) +
            emg(x, a2, mu2, sigma, K) +
            emg(x, a3, mu3, sigma, K))


# --- Evaluation ---

def calculate_fit_stats(y_true, y_pred):
    ss_res = np.sum((y_true - y_pred) ** 2)
    ss_tot = np.sum((y_true - np.mean(y_true)) ** 2)
    r2 = 1 - (ss_res / np.maximum(ss_tot, 1e-15))
    return r2


# --- Main Fitting Routine ---

def evaluate_constrained_models(x, y):
    """
    Fits asymmetric constrained models to Si-28, 29, and 30 peaks.
    """
    # 1. Physics-based initial guesses for Silicon
    # Find the maximum value and assume it is Si-28
    max_idx = np.argmax(y)
    mu28 = x[max_idx]
    amp28 = y[max_idx]

    # Magnet current scales with sqrt(Mass). Calculate expected positions for 29 and 30.
    mu29 = mu28 * np.sqrt(29 / 28)
    mu30 = mu28 * np.sqrt(30 / 28)

    # Approximate natural abundances (Si-28: 92%, Si-29: 4.7%, Si-30: 3.1%)
    amp29 = amp28 * 0.05
    amp30 = amp28 * 0.03

    # Base width guess
    w_guess = (np.max(x) - np.min(x)) * 0.005

    # Parameters: [a1, mu1, a2, mu2, a3, mu3, width_param_1, width_param_2]
    p0_split = [amp28, mu28, amp29, mu29, amp30, mu30, w_guess * 0.5, w_guess * 2.0]
    p0_emg = [amp28 * (w_guess * 2), mu28, amp29 * (w_guess * 2), mu29, amp30 * (w_guess * 2), mu30, w_guess, 2.0]

    # Bounds to force positive amplitudes and positive widths
    bounds_split = ([0, 0, 0, 0, 0, 0, 1e-6, 1e-6],
                    [np.inf, np.inf, np.inf, np.inf, np.inf, np.inf, np.inf, np.inf])

    bounds_emg = ([0, 0, 0, 0, 0, 0, 1e-6, 1e-3],
                  [np.inf, np.inf, np.inf, np.inf, np.inf, np.inf, np.inf, np.inf])

    models = {
        "Shared Split-Gaussian": (shared_split_3, p0_split, bounds_split),
        "Shared EMG (Exp. Tail)": (shared_emg_3, p0_emg, bounds_emg)
    }

    fig, axes = plt.subplots(2, 1, figsize=(12, 10), sharex=True)

    for ax, (name, (model_func, p0, bounds)) in zip(axes, models.items()):
        try:
            popt, _ = curve_fit(model_func, x, y, p0=p0, bounds=bounds, maxfev=50000)
            y_pred = model_func(x, *popt)

            r2 = calculate_fit_stats(y, y_pred)

            ax.plot(x, y, 'ko', markersize=3, alpha=0.5, label='Raw Binned Data')
            ax.plot(x, y_pred, 'r-', linewidth=2, label=f'Constrained Fit')

            # Plot individual extracted peaks for visual verification
            if "Split" in name:
                ax.plot(x, split_gaussian(x, popt[0], popt[1], popt[6], popt[7]), '--', alpha=0.5, label="Si-28")
                ax.plot(x, split_gaussian(x, popt[2], popt[3], popt[6], popt[7]), '--', alpha=0.5, label="Si-29")
                ax.plot(x, split_gaussian(x, popt[4], popt[5], popt[6], popt[7]), '--', alpha=0.5, label="Si-30")
            else:
                ax.plot(x, emg(x, popt[0], popt[1], popt[6], popt[7]), '--', alpha=0.5, label="Si-28")
                ax.plot(x, emg(x, popt[2], popt[3], popt[6], popt[7]), '--', alpha=0.5, label="Si-29")
                ax.plot(x, emg(x, popt[4], popt[5], popt[6], popt[7]), '--', alpha=0.5, label="Si-30")

            #ax.set_yscale('symlog', linthresh=1e-13)
            ax.set_title(f"{name} | R² = {r2:.6f}")
            ax.set_ylabel("Current (A)")
            ax.grid(True, alpha=0.3)
            ax.legend(loc='upper right')

        except RuntimeError as e:
            ax.text(0.5, 0.5, f"Fit failed: {e}", ha='center', va='center', transform=ax.transAxes)

    axes[-1].set_xlabel("Magnet Current (A)")
    fig.tight_layout()
    plt.show()


# Example execution assuming 'scan_data' exists from the previous script:
# x_data = scan_data['magnet_binned'].values
# y_data = scan_data['mean'].values
# evaluate_constrained_models(x_data, y_data)

import numpy as np
import matplotlib.pyplot as plt
from scipy.optimize import least_squares


# --- Mathematical Model ---

def generate_predictions(p, x, n_sub):
    """
    Constructs the composite prediction for 3 peaks using a shared N-Gaussian shape.
    p layout: [A28, A29, A30, mu28, mu29, mu30, w1] + [h_i, d_i, w_i] for each extra sub-Gaussian.
    """
    A28, A29, A30 = p[0:3]
    mu28, mu29, mu30 = p[3:6]
    w1 = p[6]

    # Shared shape function S(dx) anchored at dx=0
    def shared_shape(dx):
        shape = np.exp(-(dx ** 2) / (2 * w1 ** 2))
        for i in range(2, n_sub + 1):
            idx = 7 + (i - 2) * 3
            h_i, d_i, w_i = p[idx:idx + 3]
            shape += h_i * np.exp(-((dx - d_i) ** 2) / (2 * w_i ** 2))
        return shape

    return A28 * shared_shape(x - mu28) + \
        A29 * shared_shape(x - mu29) + \
        A30 * shared_shape(x - mu30)


def gmm_residuals(p, x, y, n_sub):
    return generate_predictions(p, x, n_sub) - y


# --- Optimization Routine ---

def auto_optimize_peak_shape(x, y, max_sub_gaussians=4):
    """
    Iteratively fits shared GMMs of increasing complexity and selects the best model via AIC.
    """
    # Base initial guesses
    max_idx = np.argmax(y)
    mu28_guess = x[max_idx]
    amp28_guess = y[max_idx]
    mu29_guess = mu28_guess * np.sqrt(29 / 28)
    mu30_guess = mu28_guess * np.sqrt(30 / 28)
    w_base = (np.max(x) - np.min(x)) * 0.005

    best_aic = np.inf
    best_popt = None
    best_n = 1
    best_y_pred = None

    results = {}

    for n in range(1, max_sub_gaussians + 1):
        print(f"Fitting model with {n} sub-Gaussians ({(n - 1) * 3 + 7} parameters)...")

        # Initialize parameters and bounds
        # Base params: A28, A29, A30, mu28, mu29, mu30, w1
        p0 = [amp28_guess, amp28_guess * 0.05, amp28_guess * 0.03,
              mu28_guess, mu29_guess, mu30_guess, w_base]

        lower_bounds = [0, 0, 0, mu28_guess - 0.05, mu29_guess - 0.05, mu30_guess - 0.05, 1e-6]
        upper_bounds = [np.inf, np.inf, np.inf, mu28_guess + 0.05, mu29_guess + 0.05, mu30_guess + 0.05, 0.1]

        # Append parameters for additional sub-Gaussians
        for i in range(2, n + 1):
            p0.extend([0.1, 0.0, w_base * (i)])  # h_i, d_i, w_i guess
            lower_bounds.extend([0.0, -0.05, 1e-6])
            upper_bounds.extend([2.0, 0.05, 0.1])

        # Execute bounded optimization
        res = least_squares(
            gmm_residuals, x0=p0, bounds=(lower_bounds, upper_bounds),
            args=(x, y, n), loss='linear', max_nfev=10000, ftol=1e-12, xtol=1e-12
        )

        # Calculate Akaike Information Criterion (AIC)
        sse = np.sum(res.fun ** 2)
        k = len(p0)
        m = len(y)
        aic = 2 * k + m * np.log(sse / m)

        results[n] = {'aic': aic, 'popt': res.x, 'y_pred': generate_predictions(res.x, x, n)}
        print(f"  -> AIC: {aic:.2f} | SSE: {sse:.3e}")

        if aic < best_aic:
            best_aic = aic
            best_popt = res.x
            best_n = n
            best_y_pred = results[n]['y_pred']

    print(f"\nOptimal geometry selected: {best_n} sub-Gaussians (AIC: {best_aic:.2f})")

    # --- Visualization ---
    fig, ax = plt.subplots(figsize=(12, 6))

    ax.plot(x, y, 'ko', markersize=3, alpha=0.5, label='Raw Binned Data')

    # Plot all attempts lightly
    for n_test, data in results.items():
        if n_test != best_n:
            ax.plot(x, data['y_pred'], '--', alpha=0.3, linewidth=1, label=f'N={n_test} (AIC: {data["aic"]:.0f})')

    # Highlight best fit
    ax.plot(x, best_y_pred, 'r-', linewidth=2, label=f'Best Fit (N={best_n})')

    ax.set_yscale('symlog', linthresh=1e-13)
    ax.set_xlabel("Magnet Current (A)")
    ax.set_ylabel("Faraday Current (A)")
    ax.set_title(f"Dynamic GMM Shape Optimization\nLowest AIC dictates {best_n} shared sub-Gaussians")
    ax.grid(True, alpha=0.3)
    ax.legend(loc='upper right')

    fig.tight_layout()
    plt.show()

    return best_popt, best_n


# Example integration:
# best_params, n_sub = auto_optimize_peak_shape(x_data, y_data)

import numpy as np
import matplotlib.pyplot as plt
from scipy.optimize import least_squares


# --- Mathematical Model ---

def generate_predictions(p, x, n_sub):
    A28, A29, A30 = p[0:3]
    mu28, mu29, mu30 = p[3:6]
    w1 = p[6]

    def shared_shape(dx):
        shape = np.exp(-(dx ** 2) / (2 * w1 ** 2))
        for i in range(2, n_sub + 1):
            idx = 7 + (i - 2) * 3
            h_i, d_i, w_i = p[idx:idx + 3]
            shape += h_i * np.exp(-((dx - d_i) ** 2) / (2 * w_i ** 2))
        return shape

    return A28 * shared_shape(x - mu28) + \
        A29 * shared_shape(x - mu29) + \
        A30 * shared_shape(x - mu30)


def gmm_residuals_weighted(p, x, y, n_sub, noise_floor=1e-13):
    """
    Calculates the relative (weighted) residuals.
    Dividing by the signal amplitude forces the solver to prioritize relative accuracy.
    The noise_floor prevents dividing by near-zero in the background regions.
    """
    y_pred = generate_predictions(p, x, n_sub)

    # Use absolute y to handle any remaining negatives, clamped to the noise floor
    weights = 1.0 / np.maximum(np.abs(y), noise_floor)

    # Return weighted residuals: (Prediction - Actual) / Actual
    return (y_pred - y) * weights


# --- Optimization Routine ---

def auto_optimize_peak_shape_weighted(x, y, max_sub_gaussians=4):
    max_idx = np.argmax(y)
    mu28_guess = x[max_idx]
    amp28_guess = y[max_idx]
    mu29_guess = mu28_guess * np.sqrt(29 / 28)
    mu30_guess = mu28_guess * np.sqrt(30 / 28)
    w_base = (np.max(x) - np.min(x)) * 0.005

    best_aic = np.inf
    best_popt = None
    best_n = 1
    best_y_pred = None

    results = {}

    for n in range(1, max_sub_gaussians + 1):
        print(f"Fitting weighted model with {n} sub-Gaussians...")

        p0 = [amp28_guess, amp28_guess * 0.05, amp28_guess * 0.03,
              mu28_guess, mu29_guess, mu30_guess, w_base]

        lower_bounds = [0, 0, 0, mu28_guess - 0.05, mu29_guess - 0.05, mu30_guess - 0.05, 1e-6]
        upper_bounds = [np.inf, np.inf, np.inf, mu28_guess + 0.05, mu29_guess + 0.05, mu30_guess + 0.05, 0.1]

        for i in range(2, n + 1):
            p0.extend([0.1, 0.0, w_base * (i)])
            lower_bounds.extend([0.0, -0.05, 1e-6])
            upper_bounds.extend([2.0, 0.05, 0.1])

        res = least_squares(
            gmm_residuals_weighted, x0=p0, bounds=(lower_bounds, upper_bounds),
            args=(x, y, n), loss='linear', max_nfev=15000, ftol=1e-12, xtol=1e-12
        )

        # SSE is now the Sum of Squared Relative Errors
        sse = np.sum(res.fun ** 2)
        k = len(p0)
        m = len(y)
        aic = 2 * k + m * np.log(sse / m)

        results[n] = {'aic': aic, 'popt': res.x, 'y_pred': generate_predictions(res.x, x, n)}
        print(f"  -> AIC: {aic:.2f} | Weighted SSE: {sse:.3f}")

        if aic < best_aic:
            best_aic = aic
            best_popt = res.x
            best_n = n
            best_y_pred = results[n]['y_pred']

    print(f"\nOptimal weighted geometry: {best_n} sub-Gaussians (AIC: {best_aic:.2f})")

    # --- Visualization ---
    fig, ax = plt.subplots(figsize=(12, 6))
    ax.plot(x, y, 'ko', markersize=3, alpha=0.5, label='Raw Binned Data')

    for n_test, data in results.items():
        if n_test != best_n:
            ax.plot(x, data['y_pred'], '--', alpha=0.3, linewidth=1, label=f'N={n_test}')

    ax.plot(x, best_y_pred, 'r-', linewidth=2, label=f'Best Fit (N={best_n})')

    ax.set_yscale('symlog', linthresh=1e-13)
    ax.set_xlabel("Magnet Current (A)")
    ax.set_ylabel("Faraday Current (A)")
    ax.set_title(f"Relative-Error (Weighted) GMM Optimization\nPrioritizing pA tail matching")
    ax.grid(True, alpha=0.3)
    ax.legend(loc='upper right')

    fig.tight_layout()
    plt.show()

    return best_popt, best_n


import numpy as np
import matplotlib.pyplot as plt
from scipy.optimize import curve_fit
from scipy.signal import find_peaks


def evaluate_constrained_models_weighted(x, y):
    """
    Fits asymmetric constrained models to Si-28, 29, and 30 peaks.
    Applies relative weighting to prioritize pA tails.
    """
    max_idx = np.argmax(y)
    mu28 = x[max_idx]
    amp28 = y[max_idx]

    mu29 = mu28 * np.sqrt(29 / 28)
    mu30 = mu28 * np.sqrt(30 / 28)

    amp29 = amp28 * 0.05
    amp30 = amp28 * 0.03

    w_guess = (np.max(x) - np.min(x)) * 0.005

    p0_split = [amp28, mu28, amp29, mu29, amp30, mu30, w_guess * 0.5, w_guess * 2.0]
    p0_emg = [amp28 * (w_guess * 2), mu28, amp29 * (w_guess * 2), mu29, amp30 * (w_guess * 2), mu30, w_guess, 2.0]

    bounds_split = ([0, 0, 0, 0, 0, 0, 1e-6, 1e-6],
                    [np.inf, np.inf, np.inf, np.inf, np.inf, np.inf, np.inf, np.inf])

    bounds_emg = ([0, 0, 0, 0, 0, 0, 1e-6, 1e-3],
                  [np.inf, np.inf, np.inf, np.inf, np.inf, np.inf, np.inf, np.inf])

    models = {
        "Shared Split-Gaussian": (shared_split_3, p0_split, bounds_split),
        "Shared EMG (Exp. Tail)": (shared_emg_3, p0_emg, bounds_emg)
    }

    fig, axes = plt.subplots(2, 1, figsize=(12, 10), sharex=True)

    # Define weighting array for curve_fit
    sigma_weights = np.maximum(np.abs(y), 1e-13)

    for ax, (name, (model_func, p0, bounds)) in zip(axes, models.items()):
        try:
            # Inject sigma to force relative error minimization
            popt, _ = curve_fit(model_func, x, y, p0=p0, bounds=bounds, sigma=sigma_weights, absolute_sigma=False,
                                maxfev=50000)
            y_pred = model_func(x, *popt)

            r2, _ = calculate_fit_stats_weighted(y, y_pred)

            ax.plot(x, y, 'ko', markersize=3, alpha=0.5, label='Raw Binned Data')
            ax.plot(x, y_pred, 'r-', linewidth=2, label=f'Constrained Fit')

            if "Split" in name:
                ax.plot(x, split_gaussian(x, popt[0], popt[1], popt[6], popt[7]), '--', alpha=0.5, label="Si-28")
                ax.plot(x, split_gaussian(x, popt[2], popt[3], popt[6], popt[7]), '--', alpha=0.5, label="Si-29")
                ax.plot(x, split_gaussian(x, popt[4], popt[5], popt[6], popt[7]), '--', alpha=0.5, label="Si-30")
            else:
                ax.plot(x, emg(x, popt[0], popt[1], popt[6], popt[7]), '--', alpha=0.5, label="Si-28")
                ax.plot(x, emg(x, popt[2], popt[3], popt[6], popt[7]), '--', alpha=0.5, label="Si-29")
                ax.plot(x, emg(x, popt[4], popt[5], popt[6], popt[7]), '--', alpha=0.5, label="Si-30")

            ax.set_yscale('symlog', linthresh=1e-13)
            ax.set_title(f"{name} | R² = {r2:.6f}")
            ax.set_ylabel("Current (A)")
            ax.grid(True, alpha=0.3)
            ax.legend(loc='upper right')

        except RuntimeError as e:
            ax.text(0.5, 0.5, f"Fit failed: {e}", ha='center', va='center', transform=ax.transAxes)

    axes[-1].set_xlabel("Magnet Current (A)")
    fig.tight_layout()
    plt.show()


def evaluate_peak_models_weighted(x, y):
    """
    Fits 3 different models to the mass-scan data and visualizes the results.
    Applies relative weighting to prioritize pA tails.
    """
    print("Evaluating peak models weighted")
    peak_indices, _ = find_peaks(y, distance=10, prominence=np.max(y) * 0.01)

    if len(peak_indices) < 3:
        print(f"Warning: find_peaks only located {len(peak_indices)} peaks. Adjust prominence/distance.")
        return

    top_3_indices = sorted(peak_indices[np.argsort(y[peak_indices])][-3:])
    peak_x = x[top_3_indices]
    peak_y = y[top_3_indices]

    w_guess = (np.max(x) - np.min(x)) * 0.01

    p0_3g = [peak_y[0], peak_x[0], w_guess,
             peak_y[1], peak_x[1], w_guess,
             peak_y[2], peak_x[2], w_guess]

    p0_3s = [peak_y[0], peak_x[0], w_guess, 2.0,
             peak_y[1], peak_x[1], w_guess, 2.0,
             peak_y[2], peak_x[2], w_guess, 2.0]

    p0_6g = [peak_y[0], peak_x[0], w_guess, peak_y[0] * 0.3, peak_x[0] + w_guess, w_guess,
             peak_y[1], peak_x[1], w_guess, peak_y[1] * 0.3, peak_x[1] + w_guess, w_guess,
             peak_y[2], peak_x[2], w_guess, peak_y[2] * 0.3, peak_x[2] + w_guess, w_guess]

    models = {
        "3 Standard Gaussians": (model_3_gaussians, p0_3g),
        "3 Skewed Gaussians": (model_3_skewed, p0_3s),
        "6 Gaussians (3 Doublets)": (model_6_gaussians, p0_6g)
    }

    fig, axes = plt.subplots(3, 1, figsize=(12, 12), sharex=True)

    # Define weighting array for curve_fit
    sigma_weights = np.maximum(np.abs(y), 1e-13)

    for ax, (name, (model_func, p0)) in zip(axes, models.items()):
        try:
            # Inject sigma to force relative error minimization
            popt, _ = curve_fit(model_func, x, y, p0=p0, sigma=sigma_weights, absolute_sigma=False, maxfev=50000)
            y_pred = model_func(x, *popt)

            r2, rmse = calculate_fit_stats_weighted(y, y_pred)

            ax.plot(x, y, 'ko', markersize=3, alpha=0.5, label='Raw Data')
            ax.plot(x, y_pred, 'r-', linewidth=2, label=f'Composite Fit')

            ax.set_yscale('symlog', linthresh=1e-13)
            ax.set_title(f"{name} | R² = {r2:.5f} | RMSE = {rmse:.2e}")
            ax.set_ylabel("Current (A)")
            ax.grid(True, alpha=0.3)
            ax.legend(loc='upper right')

        except RuntimeError:
            ax.text(0.5, 0.5, "Fit failed to converge", ha='center', va='center', transform=ax.transAxes)
            print(f"[{name}] Fit failed. The solver could not estimate the covariance matrix.")

    axes[-1].set_xlabel("Magnet Current (A)")
    fig.tight_layout()
    plt.show()

def calculate_fit_stats_weighted(y_true, y_pred):
    ss_res = np.sum((y_true - y_pred)**2)
    ss_tot = np.sum((y_true - np.mean(y_true))**2)
    r2 = 1 - (ss_res / np.maximum(ss_tot, 1e-15))
    rmse = np.sqrt(np.mean((y_true - y_pred)**2))
    return r2, rmse


import numpy as np
import matplotlib.pyplot as plt
from lmfit import Model, Parameters
from scipy.stats import exponnorm


# 1. Define a single asymmetric shape (EMG)
def emg_peak(x, amp, center, sigma, skew):
    return amp * exponnorm.pdf(x, skew, loc=center, scale=sigma)


# 2. Build composite model stringing 3 EMGs together
def composite_mass_scan(x, amp28, center28, amp29, center29, amp30, center30, sigma, skew):
    p28 = emg_peak(x, amp28, center28, sigma, skew)
    p29 = emg_peak(x, amp29, center29, sigma, skew)
    p30 = emg_peak(x, amp30, center30, sigma, skew)
    return p28 + p29 + p30


def fit_with_lmfit(x, y):
    # Initialize lmfit Model
    gmodel = Model(composite_mass_scan)
    params = Parameters()

    max_idx = np.argmax(y)
    mu28_guess = x[max_idx]
    amp28_guess = y[max_idx]
    w_guess = (np.max(x) - np.min(x)) * 0.005

    # 3. Add named parameters and algebraic constraints
    params.add('amp28', value=amp28_guess, min=0)
    params.add('center28', value=mu28_guess)

    # Force Si-29 and Si-30 positions based on B-field mass scaling
    params.add('center29', expr='center28 * sqrt(29/28)')
    params.add('center30', expr='center28 * sqrt(30/28)')

    # Initialize amplitudes
    params.add('amp29', value=amp28_guess * 0.05, min=0)
    params.add('amp30', value=amp28_guess * 0.03, min=0)

    # Shared shape parameters
    params.add('sigma', value=w_guess, min=1e-6)
    params.add('skew', value=2.0, min=0.1, max=10)

    # 4. Execute fit with relative weighting for pA tails
    weights = 1.0 / np.maximum(np.abs(y), 1e-13)
    result = gmodel.fit(y, params, x=x, weights=weights)

    # Print comprehensive statistical report
    print(result.fit_report())

    # Plotting
    fig, ax = plt.subplots(figsize=(10, 6))
    ax.plot(x, y, 'ko', markersize=3, alpha=0.5, label='Data')
    ax.plot(x, result.best_fit, 'r-', linewidth=2, label='lmfit Best Fit')

    ax.set_yscale('symlog', linthresh=1e-13)
    ax.set_ylabel("Current (A)")
    ax.set_xlabel("Magnet Current (A)")
    ax.grid(True, alpha=0.3)
    ax.legend()
    plt.show()

    return result


# Example execution:
# result = fit_with_lmfit(x_data, y_data)

import pandas as pd


def export_arrays_to_csv(x_data, y_data, output_filename="mass_scan_data.csv"):
    """
    Exports numpy arrays or lists to a CSV file.
    """
    df = pd.DataFrame({
        'Magnet_Current_A': x_data,
        'Faraday_Current_A': y_data
    })

    df.to_csv(output_filename, index=False)
    print(f"Exported {len(df)} rows to {output_filename}")


# --- Alternative: Exporting directly from the Pandas DataFrame ---
# If you are utilizing the 'scan_data' dataframe generated in the earlier binning script:
# scan_data.to_csv("binned_mass_scan.csv", index=False)

if __name__ == "__main__":
    plot_mass_scan()