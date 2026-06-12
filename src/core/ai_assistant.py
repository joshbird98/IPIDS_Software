import os
import json
from google import genai
from google.genai import types
from dotenv import load_dotenv
import time
import inspect
load_dotenv()

from src.core import routines


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

        # 2. Load Fault Config
        faults_path = os.path.abspath(os.path.join(config_dir, '../fault_config.json'))
        try:
            with open(faults_path, 'r', encoding='utf-8') as f:
                self.fault_tags = f.read()
        except FileNotFoundError:
            self.fault_tags = "{}"

    def _get_existing_recipes(self) -> str:
        """Dynamically fetch current recipe names to prevent duplicates."""
        if not os.path.exists(self.config_dir):
            return "None"
        files = [f.replace(".json", "") for f in os.listdir(self.config_dir) if f.endswith(".json")]
        return ", ".join(files) if files else "None"

    def _get_routine_documentation(self) -> str:
        doc_str = "AVAILABLE ROUTINES (Use these for ROUTINE_CALL steps):\n"
        # Iterate over the registry
        for name, cls in routines.ROUTINE_REGISTRY.items():
            # Get the docstring, stripping whitespace
            doc = inspect.getdoc(cls) or "No description provided."
            doc_str += f"- {name}: {doc}\n"
        return doc_str

    def generate(self, user_prompt: str) -> dict:
        max_retries = 3
        backoff_time = 2  # Start with 2 seconds

        if not self.available:
            return {"status": "error", "message": "API Key missing."}

        existing_recipes = self._get_existing_recipes()
        routine_docs = self._get_routine_documentation()

        system_instruction = f"""
            You are a Master Control Engineer for an Ion Beam facility.
            Translate user requests into exact JSON recipes for a state-machine execution engine.

            AVAILABLE MACHINE ISA-95 TAGS: 
            {self.machine_tags}

            AVAILABLE FAULT CONFIGURATIONS:
            {self.fault_tags}

            EXISTING RECIPES (Do not duplicate these names):
            {existing_recipes}
            
            AVAILABLE ROUTINES:
            {routine_docs}
            
            MACHINE CONTEXT:
            - The Ion Source is a Cesium Sputter Source.
            - Filament heats the Ionizer via thermionic bombardment. The filament ramps slowly at ~0.8A per second.
            - Thermionic current is coupled to the filament temperature. When enabling the Thermionic PSU using `psu_control`, you MUST set `wait_for_readback` to 0.0, because the current readback will not match the setpoint until the filament is fully hot.
            - Thermionic current can be controlled via PID with an auto-emission mode.
            - To use the auto-emission mode, first ensure the filament and thermionic power supplies are enabled, then set 'stat_thermionic_auto_emission' to true, and choose a requested setpoint current.
            - The entire source is biased at Extraction Voltage; the Target stick is biased at Target Voltage.
            - Cesium Oven temperature is managed by the PLC; do not micro-manage it.
            
            CRITICAL OPERATING RANGES (Warn user if requested values are outside these):
            - Extraction: 15-20 kV
            - Source Einzel: 8-15 kV
            - Target Bias: 5-9 kV
            - Filament Current: 25-30 A (typically driven by auto-emission control loop)
            - Thermionic Current: 50-130 mA
            - Cesium Temp: 40-90 C
            
            RULE 0 - BANNED OUTPUTS:
            - NEVER use the tag 'ion_beam.system.cmd_fault_reset'. Automated recipes are strictly prohibited from resetting faults. If a user asks to reset faults, return a chat message explaining this is a safety violation.
            - NEVER use primitive ACTION steps to adjust or turn on Power Supplies (Filament, Extraction, Target, Magnet, etc.). You MUST use the `psu_control` ROUTINE_CALL instead, which safely handles enabling the supply and verifying the hardware readback.
            Example: {{"step_id": 2, "type": "ROUTINE_CALL", "routine_name": "psu_control", "parameters": {{"cmd_enable": "...", "stat_enabled": "...", "sp_tag": "...", "rb_tag": "...", "target_val": 10.0, "tolerance": 0.5, "timeout_sec": 15.0, "requires_relay": 1.0, "rb_relay": "ion_beam.facilities.safety_relay_active"}}, "comment": "Turn on Filament"}}
          
            RULE 1 - SCHEMA COMPLIANCE:
            You MUST use the following step types and EXACT keys.
            - ACTION: {{"step_id": 1, "type": "ACTION", "target": "isa_95_tag_string", "value": 1.0, "comment": "..."}}
            - WAIT_TIME: {{"step_id": 2, "type": "WAIT_TIME", "duration_sec": 5.0, "comment": "..."}}
            - WAIT_TELEMETRY: {{"step_id": 3, "type": "WAIT_TELEMETRY", "target": "isa_95_tag_string", "condition": "==", "value": 1.0, "timeout_sec": 0.0, "comment": "..."}}
            - USER_PROMPT: {{"step_id": 4, "type": "USER_PROMPT", "prompt_text": "...", "verify_conditions": [], "comment": "..."}}
            - ROUTINE_CALL: {{"step_id": 5, "type": "ROUTINE_CALL", "routine_name": "mass_scan", "parameters": {{...}}, "comment": "..."}}
            - VERIFIED_ACTION: {{"step_id": 6, "type": "VERIFIED_ACTION", "target": "cmd_tag", "value": 1.0, "pre_target": "perm_tag", "pre_condition": "==", "pre_value": 1.0, "verify_target": "rb_tag", "verify_condition": "==", "verify_value": 1.0, "timeout_sec": 5.0, "comment": "..."}}
            - PARALLEL: {{"step_id": 7, "type": "PARALLEL", "steps": [ {{...child step...}}, {{...child step...}} ], "comment": "..."}}

            RULE 2 - PROACTIVE FAULT POLICY & SAFETY AUTHORITY:
            You are responsible for hardware safety. You must be PRESUMPTUOUS about adding fault policies.
            - If the user's sequence interacts with ANY high-voltage, heating, or magnetic components (e.g., Magnet, Extraction, Target, Filament, Thermionic, Cesium, or Source Temperature), you MUST automatically inject the 'Safety_Relay_Tripped' BIT_FAULT into the `fault_policy` array, EVEN IF the user did not ask for it.
            - Example: {{"type": "BIT_FAULT", "registry_tag": "ion_beam.faults.word_0_system", "bit_str": "0.1", "name": "Safety_Relay_Tripped", "action": "ABORT"}}
            - If the sequence involves Vacuum/Pumps, automatically include relevant Turbo Trip or Gauge faults from the FAULT CONFIGURATIONS.
            - If you are unsure what safety limits apply to a vague request, return exactly: {{"status": "chat", "message": "I can build this, but should I include the Safety Relay and Gate Valve timeout faults in the policy?"}}
            - ANY recipe involving the source, magnet, or high-voltage bias MUST start with the 'safety_relay_init' routine.
            - Before adjusting thermionic current, ensure 'stat_thermionic_auto_emission' is active.
            - Equally, if driving the filament directly, ensure that 'stat_thermionic_auto_emission' is 0.
            - You MUST verify the Safety Relay is active (using PSUControlRoutine 'requires_relay') before any voltage or current setpoint is applied.
            
            RULE 3 - BEHAVIOR:
            Generate a highly professional, descriptive `recipe_name`.
            If crucial physical parameters are missing, return exactly: {{"status": "chat", "message": "..."}}
            If clear, return exactly: {{"status": "success", "recipe": {{"recipe_name": "...", "version": "1.0", "fault_policy": [...], "steps": [...]}}}}
            
            RULE 4 - HEURISTIC COMPLETION:
            When the user requests a source parameter change (e.g., "Set target voltage to 7kV"), you are not just a translator, but a safety engineer. You must:
            - Check if the 'safety_relay_init' routine is in the steps. If not, inject it as Step 1.
            - Check if the value is within the CRITICAL OPERATING RANGES. If not, return a chat status with a warning.
            - Ensure the PSU is enabled before sending a setpoint.
            """



        for attempt in range(max_retries):
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
                if "503" in str(e) or "UNAVAILABLE" in str(e):
                    if attempt < max_retries - 1:
                        time.sleep(backoff_time)
                        backoff_time *= 2  # Double the wait time for next try
                        continue
                    else:
                        return {"status": "error", "message": "Service overloaded after retries."}
                else:
                    return {"status": "error", "message": str(e)}
        return None