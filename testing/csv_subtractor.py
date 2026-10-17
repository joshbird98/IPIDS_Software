import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
from matplotlib.widgets import Slider, Button
import sys


def interactive_subtraction(file_base, file_bg, output_file,
                            mag_col='Measured_Magnet_A', beam_col='Measured_Beam_A'):
    """
    Loads base and background mass scans, provides an interactive GUI to align the
    background magnet axis (gain/offset) and amplitude intensity, interpolates the
    background data, and outputs the subtracted result.
    """
    try:
        df_base = pd.read_csv(file_base)
        df_bg = pd.read_csv(file_bg)
    except FileNotFoundError as e:
        print(f"Error loading files: {e}", file=sys.stderr)
        sys.exit(1)

    df_base = df_base.sort_values(by=mag_col).copy()
    df_bg = df_bg.sort_values(by=mag_col).copy()

    base_mag = df_base[mag_col].values
    base_beam = df_base[beam_col].values
    bg_mag_raw = df_bg[mag_col].values
    bg_beam_raw = df_bg[beam_col].values

    # Setup Matplotlib Figure
    fig, ax = plt.subplots(figsize=(12, 8))
    # Increased bottom margin to accommodate the third slider
    plt.subplots_adjust(bottom=0.40)

    ax.set_yscale('symlog', linthresh=1e-12)
    ax.set_title("Mass Scan Subtraction Tuning")
    ax.set_xlabel(f"{mag_col} (Amps)")
    ax.set_ylabel(f"{beam_col} (Amps) - Symlog Scale")
    ax.grid(True, which='both', linestyle='--', alpha=0.5)

    line_base, = ax.plot(base_mag, base_beam, label='Base Scan', color='blue', alpha=0.4)
    line_bg, = ax.plot(bg_mag_raw, bg_beam_raw, label='Shifted/Scaled Background', color='orange', alpha=0.6)
    line_sub, = ax.plot(base_mag, base_beam, label='Subtracted Result', color='red', linewidth=1.5)
    ax.legend(loc='upper right')

    # Define Slider Axes Layout
    ax_offset = plt.axes([0.20, 0.25, 0.65, 0.03])
    ax_mag_gain = plt.axes([0.20, 0.20, 0.65, 0.03])
    ax_amp_gain = plt.axes([0.20, 0.15, 0.65, 0.03])
    ax_save = plt.axes([0.80, 0.05, 0.1, 0.05])

    # Instantiate Sliders
    slider_offset = Slider(ax_offset, 'Magnet Offset (A)', -0.5, 0.5, valinit=0.0, valstep=0.001)
    slider_mag_gain = Slider(ax_mag_gain, 'Magnet Gain (X-axis)', 0.95, 1.05, valinit=1.0, valstep=0.0001)
    slider_amp_gain = Slider(ax_amp_gain, 'BG Amplitude Gain (Y-axis)', 0.5, 5.0, valinit=1.0, valstep=0.01)

    state = {'subtracted_beam': np.zeros_like(base_beam)}

    def update(val):
        offset = slider_offset.val
        mag_gain = slider_mag_gain.val
        amp_gain = slider_amp_gain.val

        # Apply transformation to X-axis (Magnet)
        bg_mag_adjusted = (bg_mag_raw * mag_gain) + offset

        # Apply transformation to Y-axis (Beam Intensity)
        bg_beam_adjusted = bg_beam_raw * amp_gain

        # Interpolate background beam onto the base magnet axis
        bg_beam_interp = np.interp(base_mag, bg_mag_adjusted, bg_beam_adjusted, left=0.0, right=0.0)

        # Calculate subtraction
        state['subtracted_beam'] = base_beam - bg_beam_interp

        # Update plot arrays
        line_bg.set_xdata(bg_mag_adjusted)
        line_bg.set_ydata(bg_beam_adjusted)
        line_sub.set_ydata(state['subtracted_beam'])

        ax.relim()
        ax.autoscale_view()
        fig.canvas.draw_idle()

    slider_offset.on_changed(update)
    slider_mag_gain.on_changed(update)
    slider_amp_gain.on_changed(update)

    def save_data(event):
        df_out = df_base.copy()
        df_out[beam_col] = state['subtracted_beam']

        if 'Timestamp' in df_out.columns:
            df_out = df_out.sort_values(by='Timestamp')

        df_out.to_csv(output_file, index=False)
        print(f"Data saved to {output_file} (Mag Offset={slider_offset.val:.3f}A, "
              f"Mag Gain={slider_mag_gain.val:.4f}, BG Amp Gain={slider_amp_gain.val:.2f}x)")

    btn_save = Button(ax_save, 'Save CSV')
    btn_save.on_clicked(save_data)

    update(None)
    plt.show()


if __name__ == "__main__":
    FILE_1 = 'scan_base.csv'
    FILE_2 = 'scan_background.csv'
    OUTPUT_FILE = 'subtracted_scan.csv'

    MAGNET_COLUMN = 'Measured_Magnet_A'
    BEAM_COLUMN = 'Measured_Beam_A'

    interactive_subtraction(FILE_1, FILE_2, OUTPUT_FILE,
                            mag_col=MAGNET_COLUMN,
                            beam_col=BEAM_COLUMN)