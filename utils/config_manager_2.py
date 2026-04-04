import json
import os

# Set up to save in the project root, regardless of where this script is called from
current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
CONFIG_FILE = os.path.join(project_root, "plot_settings.json")

def load_config():
    if not os.path.exists(CONFIG_FILE): return {}
    try:
        with open(CONFIG_FILE, 'r') as f:
            return json.load(f)
    except Exception as e:
        print(f"Error loading config: {e}")
        return {}

def save_config(config_dict):
    try:
        with open(CONFIG_FILE, 'w') as f:
            json.dump(config_dict, f, indent=4)
        print(f"Plot settings saved to: {CONFIG_FILE}")
    except Exception as e:
        print(f"Error saving config: {e}")

def delete_config():
    if os.path.exists(CONFIG_FILE):
        try:
            os.remove(CONFIG_FILE)
            print("Config file deleted.")
            return True
        except:
            return False
    return False