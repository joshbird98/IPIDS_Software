import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
from matplotlib.widgets import Slider, Button
import sys


def interactive_multiple_shape_subtraction(base_file, shape_file, output_file,
                                           mag_col='Measured_Magnet_A',
                                           beam_col='Measured_Beam_A'):
    """
    Loads a full base scan and an extracted tail shape.
    Provides a GUI to manually instantiate, shift, scale, and stretch the shape
    over multiple peaks to model and subtract the global background.
    """
    try:
        df_base = pd.read_csv(base_file)
        df_shape = pd.read_csv(shape_file)

        # Determine shape column name based on previous script output
        shape_beam_col = f"{beam_col}_TailShape"
        if shape_beam_col not in df_shape.columns:
            shape_beam_col = beam_col  # Fallback if renamed

    except Exception as e:
        print(f"Error loading files: {e}", file=sys.stderr)
        sys.exit(1)

    df_base = df_base.sort_values(by=mag_col).copy()
    base_mag = df_base[mag_col].values
    base_beam = df_base[beam_col].values

    df_shape = df_shape.sort_values(by=mag_col).copy()
    shape_mag = df_shape[mag_col].values
    shape_beam = df_shape[shape_beam_col].values

    # Normalize the extracted shape's X-axis to center around 0
    # Assuming the center of the extracted array corresponds to the peak location
    shape_center_mag = (shape_mag[0] + shape_mag[-1]) / 2.0
    norm_shape_mag = shape_mag - shape_center_mag

    # GUI State
    state = {
        'peaks': [],  # List of dicts: {'x': center, 'amp': 1.0, 'stretch': 1.0}
        'active_idx': -1,
        'updating_ui': False,
        'global_bg': np.zeros_like(base_beam),
        'subtracted': np.copy(base_beam)
    }

    # Setup Figure
    fig, ax = plt.subplots(figsize=(14, 8))
    plt.subplots_adjust(bottom=0.45)

    ax.set_yscale('symlog', linthresh=1e-12)
    ax.set_title("Global Background Modeling via Shape Placement")
    ax.set_xlabel(f"{mag_col} (A)")
    ax.set_ylabel("Beam Current (A)")
    ax.grid(True, which='both', linestyle='--', alpha=0.5)

    line_base, = ax.plot(base_mag, base_beam, label='Original Scan', color='blue', alpha=0.4)
    line_bg, = ax.plot(base_mag, state['global_bg'], label='Total Modeled Background', color='orange', alpha=0.8)
    line_sub, = ax.plot(base_mag, state['subtracted'], label='Subtracted Scan', color='red', linewidth=1.5)

    # Active peak indicator
    vline_active = ax.axvline(x=0, color='black', linestyle=':', alpha=0)
    ax.legend(loc='upper right')

    # Define UI Axes
    ax_x = plt.axes([0.15, 0.30, 0.7, 0.03])
    ax_amp = plt.axes([0.15, 0.25, 0.7, 0.03])
    ax_stretch = plt.axes([0.15, 0.20, 0.7, 0.03])

    ax_add = plt.axes([0.15, 0.10, 0.1, 0.05])
    ax_next = plt.axes([0.26, 0.10, 0.1, 0.05])
    ax_del = plt.axes([0.37, 0.10, 0.1, 0.05])
    ax_save = plt.axes([0.75, 0.10, 0.1, 0.05])

    # Sliders
    mag_min, mag_max = base_mag.min(), base_mag.max()
    slider_x = Slider(ax_x, 'Center (A)', mag_min, mag_max, valinit=mag_min)
    slider_amp = Slider(ax_amp, 'Amplitude (x)', -10.0, 10.0, valinit=1.0)
    slider_stretch = Slider(ax_stretch, 'Stretch (x)', 0.1, 5.0, valinit=1.0)

    def calculate_model():
        state['global_bg'].fill(0.0)
        for p in state['peaks']:
            # Transform base x-coordinates into the normalized shape space
            eval_x = (base_mag - p['x']) / p['stretch']
            # Interpolate shape; left/right bounds set to 0 to prevent infinite tail extensions
            shape_eval = np.interp(eval_x, norm_shape_mag, shape_beam, left=0.0, right=0.0)
            state['global_bg'] += shape_eval * p['amp']

        state['subtracted'] = base_beam - state['global_bg']

        line_bg.set_ydata(state['global_bg'])
        line_sub.set_ydata(state['subtracted'])

        ax.relim()
        ax.autoscale_view()
        fig.canvas.draw_idle()

    def update_sliders_from_state():
        if state['active_idx'] >= 0 and len(state['peaks']) > 0:
            state['updating_ui'] = True
            p = state['peaks'][state['active_idx']]
            slider_x.set_val(p['x'])
            slider_amp.set_val(p['amp'])
            slider_stretch.set_val(p['stretch'])
            vline_active.set_xdata([p['x']])
            vline_active.set_alpha(0.8)
            state['updating_ui'] = False
        else:
            vline_active.set_alpha(0)
            fig.canvas.draw_idle()

    def on_slider_change(val):
        if state['updating_ui'] or state['active_idx'] < 0:
            return

        p = state['peaks'][state['active_idx']]
        p['x'] = slider_x.val
        p['amp'] = slider_amp.val
        p['stretch'] = slider_stretch.val

        vline_active.set_xdata([p['x']])
        calculate_model()

    slider_x.on_changed(on_slider_change)
    slider_amp.on_changed(on_slider_change)
    slider_stretch.on_changed(on_slider_change)

    def add_peak(event):
        # Default new peak to center of visible axis
        x_lims = ax.get_xlim()
        new_x = (x_lims[0] + x_lims[1]) / 2.0
        state['peaks'].append({'x': new_x, 'amp': 1.0, 'stretch': 1.0})
        state['active_idx'] = len(state['peaks']) - 1
        update_sliders_from_state()
        calculate_model()

    def next_peak(event):
        if len(state['peaks']) > 0:
            state['active_idx'] = (state['active_idx'] + 1) % len(state['peaks'])
            update_sliders_from_state()

    def delete_peak(event):
        if state['active_idx'] >= 0:
            state['peaks'].pop(state['active_idx'])
            state['active_idx'] -= 1
            if state['active_idx'] < 0 and len(state['peaks']) > 0:
                state['active_idx'] = 0
            update_sliders_from_state()
            calculate_model()

    def save_data(event):
        df_out = df_base.copy()
        df_out[beam_col] = state['subtracted']
        # Optional: Save the background model as a reference column
        df_out['Global_Background_Model'] = state['global_bg']

        if 'Timestamp' in df_out.columns:
            df_out = df_out.sort_values(by='Timestamp')

        df_out.to_csv(output_file, index=False)
        print(f"Data saved to {output_file} with {len(state['peaks'])} background shapes applied.")

    btn_add = Button(ax_add, 'Add Shape')
    btn_add.on_clicked(add_peak)

    btn_next = Button(ax_next, 'Next Shape')
    btn_next.on_clicked(next_peak)

    btn_del = Button(ax_del, 'Delete Active')
    btn_del.on_clicked(delete_peak)

    btn_save = Button(ax_save, 'Save CSV')
    btn_save.on_clicked(save_data)

    plt.show()


if __name__ == "__main__":
    BASE_FILE = 'scan_base.csv'
    SHAPE_FILE = 'extracted_c_tail.csv'
    OUTPUT_FILE = 'subtracted_multiple_tails.csv'

    interactive_multiple_shape_subtraction(BASE_FILE, SHAPE_FILE, OUTPUT_FILE)