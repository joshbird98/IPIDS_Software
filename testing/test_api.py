import os
from google import genai
from dotenv import load_dotenv

# Load the key from your .env file
load_dotenv()
api_key = os.getenv("GEMINI_API_KEY")

if not api_key:
    print("Error: Could not find GEMINI_API_KEY in .env")
else:
    print("Authenticating...")
    client = genai.Client(api_key=api_key)

    print("\n--- Available Flash Models ---")
    try:
        # Ask Google for the exact list of models your key has access to
        for model in client.models.list():
            if "flash" in model.name.lower():
                # Print the exact string you need to copy/paste
                print(model.name)
    except Exception as e:
        print(f"API Error: {e}")