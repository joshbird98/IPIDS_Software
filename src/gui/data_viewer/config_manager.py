import json
import os

# Set up to save in the project root, regardless of where this script is called from
REGISTRY_FILE = os.path.abspath(os.path.join(os.path.dirname(__file__), '../../../config/system_tags.json'))
CONFIG_FILE = os.path.abspath(os.path.join(os.path.dirname(__file__), '../.././/config/plot_settings.json'))

def load_registry():
    """Loads the read-only master hardware registry."""
    if not os.path.exists(REGISTRY_FILE):
        print(f"Warning: {REGISTRY_FILE} not found. UI will use fallback defaults.")
        return {}
    try:
        with open(REGISTRY_FILE, 'r') as f:
            return json.load(f)
    except Exception as e:
        print(f"Error loading registry: {e}")
        return {}

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