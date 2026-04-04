import json
import os

current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir) # Go up one level from 'utils'
CONFIG_FILE = os.path.join(project_root, "plot_settings.json")

def load_config():
    """Loads the channel configuration from disk."""
    if not os.path.exists(CONFIG_FILE):
        return {}

    try:
        with open(CONFIG_FILE, 'r') as f:
            return json.load(f)
    except Exception as e:
        print(f"Error loading config: {e}")
        return {}


def save_config(config_dict):
    """Saves the current channel configuration to disk."""
    try:
        with open(CONFIG_FILE, 'w') as f:
            json.dump(config_dict, f, indent=4)
        print(f"✅ Plot settings saved to: {CONFIG_FILE}")
    except Exception as e:
        print(f"Error saving config: {e}")

def delete_config():
    """Nuclear option to wipe settings."""
    if os.path.exists(CONFIG_FILE):
        try:
            os.remove(CONFIG_FILE)
            print("💥 Config file deleted.")
            return True
        except:
            return False
    return False