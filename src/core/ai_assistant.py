import os
import json
from google import genai
from google.genai import types
from dotenv import load_dotenv

load_dotenv()


class RecipeAIAssistant:
    def __init__(self, config_dir: str):
        self.api_key = os.getenv("GEMINI_API_KEY")
        self.available = bool(self.api_key)
        self.config_dir = config_dir

        if self.available:
            self.client = genai.Client(api_key=self.api_key)

        # 1. Load Machine Tags
        tags_path = os.path.abspath(os.path.join(config_dir, '../system_tags.json'))
        try:
            with open(tags_path, 'r', encoding='utf-8') as f:
                self.machine_tags = f.read()
        except FileNotFoundError:
            self.machine_tags = "{}"

    def _get_existing_recipes(self) -> str:
        """Dynamically fetch current recipe names to prevent duplicates."""
        if not os.path.exists(self.config_dir):
            return "None"
        files = [f.replace(".json", "") for f in os.listdir(self.config_dir) if f.endswith(".json")]
        return ", ".join(files) if files else "None"

    def generate(self, user_prompt: str) -> dict:
        if not self.available:
            return {"status": "error", "message": "API Key missing."}

        existing_recipes = self._get_existing_recipes()

        system_instruction = f"""
        You are a Master Control Engineer for an Ion Beam facility.
        Translate user requests into exact JSON recipes for a state-machine execution engine.

        AVAILABLE MACHINE ISA-95 TAGS: 
        {self.machine_tags}

        EXISTING RECIPES (Do not duplicate these names):
        {existing_recipes}

        RULE 1 - SCHEMA COMPLIANCE:
        You MUST use the following step types and EXACT keys. NEVER use 'WRITE_TAG' or 'tag'.
        - ACTION: {{"step_id": 1, "type": "ACTION", "target": "isa_95_tag_string", "value": 1.0, "comment": "..."}}
        - WAIT_TIME: {{"step_id": 2, "type": "WAIT_TIME", "duration_sec": 5.0, "comment": "..."}}
        - WAIT_TELEMETRY: {{"step_id": 3, "type": "WAIT_TELEMETRY", "target": "isa_95_tag_string", "condition": "==", "value": 1.0, "timeout_sec": 0.0, "comment": "..."}}
        - USER_PROMPT: {{"step_id": 4, "type": "USER_PROMPT", "prompt_text": "...", "verify_conditions": [], "comment": "..."}}
        - ROUTINE_CALL: {{"step_id": 5, "type": "ROUTINE_CALL", "routine_name": "mass_scan", "parameters": {{"sp_target": "...", "start_val": 0.0}}, "comment": "..."}}

        RULE 2 - RECIPE NAMING:
        Generate a highly professional, descriptive `recipe_name`. Distinguish it from Existing Recipes. 
        Bad: "AI Generated Sequence"
        Good: "Source Extraction Warmup v2", "Faraday Cup Diagnostic Routine"

        RULE 3 - BEHAVIOR:
        If the user provides clear intent, return EXACTLY this JSON format:
        {{
            "status": "success", 
            "recipe": {{
                "recipe_name": "Professional Name Here", 
                "version": "1.0", 
                "fault_policy": [], 
                "steps": [ ... ]
            }}
        }}

        If crucial physical parameters (e.g., target RPM, target Voltage, specific wait times) are missing, DO NOT GUESS. Return EXACTLY this JSON format to ask the user:
        {{
            "status": "chat", 
            "message": "I need to know the target extraction voltage. What value should I use?"
        }}
        """

        try:
            response = self.client.models.generate_content(
                model='gemini-flash-lite-latest',
                contents=system_instruction + "\nUser Request: " + user_prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                )
            )
            return json.loads(response.text)
        except Exception as e:
            return {"status": "error", "message": str(e)}