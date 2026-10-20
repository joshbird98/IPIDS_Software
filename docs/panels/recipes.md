# Recipe Execution and the recipe builder

A recipe is a list of steps that the sequencer carries out in order: commands, waits, checks, operator prompts and routines such as a mass scan. The **Recipe Execution** panel starts, pauses and stops recipes and shows their progress. The recipe builder creates and edits them.

![The Recipe Execution panel with a recipe loaded](../images/panel-recipe-execution.png)

## How recipes run

Recipes run in the sequencer service (`service_sequencer`), not in the HMI. The panel only sends the sequencer commands and shows what it reports. Closing the HMI does not stop a recipe, and an HMI that is opened again picks it up: its steps, its status and any prompt waiting for an answer.

- **One recipe at a time.** A recipe can run alongside a mass scan and an optimiser sweep, but two of them can never drive the same thing at once: a recipe that needs a control another sequence holds fails.
- **Control leases.** Before its first command to a subsystem (the analyser magnet, a supply, the Faraday cup drop-in ...), the recipe claims a control lease on it. While the recipe holds the lease, that subsystem takes commands only from the recipe: its controls in the other panels are greyed out, with the reason in their tooltip, and the [Automation bar](automation-bar.md) shows what the recipe holds, for example **recipe 'Automated Mass Resolution' holds: diagnostics, magnet, smu**. The leases are released when the recipe ends. The safety relay, shutdown, fault reset and gate valve controls are never leased: they stay with the operator.
- **Steps in order.** The steps run one at a time. A step is done when its purpose is met: an ACTION, for example, when the service confirms it has carried out the command.
- **Any failure ends the recipe.** A step that fails or times out ends the recipe at once. There are no retries and no alternative paths. The status line gives the step and the reason, and the step turns red. A failed recipe also raises a medium-priority alarm on the [Alarms](alarms.md) panel, which clears when the next recipe starts.
- **No fault resets.** A recipe can't reset faults: a step that sends the fault reset fails (**automated fault resets are not allowed in recipes**), and the builder's tag selector does not offer it. Reset faults yourself with **RESET FAULTS** on the [System Health](system-health.md) panel.
- **The machine still has the last word.** The services apply the same limits and hard caps to a recipe's commands as to yours, and whatever a recipe asks for, the PLC applies its own interlocks, limits and shutdown sequences.

## The Recipe Execution panel

| Control | What it does |
|---|---|
| **OPEN RECIPE BUILDER** | Opens the recipe builder with a new, empty recipe. |
| Recipe list | The recipe files (`.json`) in `config/recipes`. |
| **↻** | Reads the recipe folder again, to list files added since. |
| **LOAD RECIPE** | Reads the selected recipe and lists its steps, ready to start. The status line reads **READY:** and its name. A file the sequencer would refuse (not valid JSON, an unknown step type, a step number used twice) gives a **Load Error** with the reason. |
| **EDIT** | Opens the selected recipe in the builder. |
| **START** | Asks the sequencer to run the loaded recipe from step 1: the recipe last loaded, not whatever is selected in the list. After a run has finished the button reads **RESTART** and runs the same recipe again. Greyed out until a recipe is loaded, while a recipe is running, and while the sequencer is not running; the tooltip says which. |
| **PAUSE** | Pauses the recipe. While it is paused the button reads **RESUME**. Available only while a recipe is running. |
| **STOP** | Stops the recipe. Available only while a recipe is running. |
| **ACKNOWLEDGE** | In the prompt box: answers the prompt (see [Operator prompts](#operator-prompts)). |
| **STOP RECIPE** | In the prompt box: stops the recipe, as **STOP** does. |
| **Steps** | The steps of the recipe loaded or running. Hover over a step to see what it does. |

> [!NOTE]
> The sequencer reads the recipe file when the recipe starts. The steps listed after **LOAD RECIPE** are the file as it was when you loaded it; if someone has changed the file since, the run follows the changed file.

### The status line

The status line shows the state, the recipe's name in brackets (inside a sub-recipe, the path to it, such as **Silicon Implantation Run > Standard Beamline Startup**) and what is happening.

| Status line | Colour | Meaning |
|---|---|---|
| **STATUS: IDLE** | grey | Nothing loaded or run since the HMI started. |
| **READY:** and a name | grey | A recipe is loaded and can be started. |
| **RUNNING** | violet | The recipe is running, and holds the controls it has claimed. |
| **PROMPT** | amber | The recipe is waiting for you. |
| **PAUSED** | amber | Paused by you, or because you took control of something it drives. |
| **FINISHED** and **Recipe completed successfully.** | green | Every step is done. |
| **FINISHED** and **Stopped by operator at step** ... **- outputs left as they were.** | grey | You stopped it. |
| **FINISHED** and the reason it failed | red | A step failed (**Step 3 failed:** and why), or the fault policy stopped it. |
| **START refused:** (or another command) and the reason | red | The sequencer refused your command, for example while it is still starting up. |
| **SEQUENCER OFFLINE** | dashed outline | The sequencer service is not running. Start it from the [Service Manager](service-manager.md) panel. The line can stay after the sequencer is back, until a recipe is loaded or started. |

In the step list:

| Step | Meaning |
|---|---|
| Plain | Not reached yet. |
| Violet | The step the recipe is on (also while it is paused). |
| Amber | The step is waiting for you. |
| Green, with a tick (✓) | Done. |
| Red, with a cross (✗) | Failed. |

Inside a sub-recipe the list shows the sub-recipe's steps. A PARALLEL step is one row.

### Operator prompts

When a recipe reaches a prompt (a USER_PROMPT step, or a routine or PARALLEL step that needs you), the box **Recipe action required** appears above the steps, with the step number and what to do. The recipe waits as long as it takes: prompts have no time limit.

1. Do what the prompt asks.
2. Press **ACKNOWLEDGE**. The sequencer checks the step's conditions, if it has any, against live values.
3. If a condition is not met, your answer is refused and the box shows **Not accepted - not yet:** and each condition that fails, with its present value (**no live value** if there is none). The prompt stays: press **ACKNOWLEDGE** again once the conditions hold.

Each prompt carries a number, so an answer meant for an earlier prompt can't answer a later one. The box is hidden while the recipe is paused and comes back when you resume it. Prompts and answers are recorded in the event log.

## Pausing and stopping a recipe

| You press | What happens |
|---|---|
| **PAUSE** | The recipe halts at the step it is on and sends nothing more. The step's timers stop: a wait, a timeout, a verification, a routine's own timers. The recipe keeps the controls it holds. The fault policy is not checked while it is paused. |
| **RESUME** | The step carries on where it was, with its timers moved on by the length of the pause. |
| **RESUME** after you took control | The step starts again from its beginning: its command is sent again, a wait starts from zero, a routine from its first stage. The recipe claims its controls again. |
| **STOP** or **STOP RECIPE** | The recipe ends at once (**Stopped by operator at step** ... **- outputs left as they were.**). Nothing is switched off and no setpoint is put back: every output stays as the completed steps left it, and a command already sent is still carried out. The recipe's controls are released. |

> [!WARNING]
> **STOP** does not make the machine safe: it leaves every supply as it is. To shut the source down, use **INITIATE SHUTDOWN** on the [System Health](system-health.md) panel; the automated shutdown takes about 20 minutes (see [Shutting down](../procedures/beam-shut-down.md)).

If you take control of something a running recipe drives, with **TAKE CONTROL** on the [Automation bar](automation-bar.md), the recipe pauses: **Paused: the operator took control of the** ... **Resume to carry on (the current step starts again), or stop.**

## Fault policy

A recipe's fault policy lists PLC faults that stop it. While the recipe runs, the sequencer checks each listed fault bit in the live PLC data every time it moves the recipe on (about 50 times a second):

- if a listed fault is active, the recipe ends as failed: **Stopped: fault** name **is active (fault policy).**;
- if the PLC fault data has not been live for 3 s, the recipe ends as failed too (**fault status unknown for 3 s**).

Ending the recipe is the only action a fault policy can take. Inside a sub-recipe, the top recipe's policy applies together with the running sub-recipe's own. Set the policy with **Fault policy...** in the builder.

## Steps

| Step type | What it does | Fails when |
|---|---|---|
| ACTION | Sends one command: **Target Tag** set to **Command Value**. Done when the service confirms it has carried it out. | The service refuses or fails the command (the reason is given), or the target is the fault reset. |
| VERIFIED_ACTION | Checks a condition (**Pre-Req**), sends the command, then waits for **Verify Tag** to meet its condition, for up to **Timeout** (5 s by default). With no Pre-Req tag the first check is skipped. | The precondition is not met, or has no live value, when the step starts (it does not wait for it); the command is refused; the verification is not met in time. |
| WAIT_TIME | Waits **Duration**. | Never. |
| WAIT_TELEMETRY | Waits until **Target Tag** meets **Condition** and **Target Value**, judged on live values only. A **Timeout** of 0 means none. | The timeout runs out, or the tag has had no live value for 10 s. |
| USER_PROMPT | Shows **Popup Text** and waits for **ACKNOWLEDGE**. With verify conditions, the answer is refused until they all hold. | Never: it waits for as long as it takes. |
| SUB_RECIPE | Runs another recipe (**Target File**), then carries on with the next step. | The file is missing, unreadable, outside the recipes folder, or nested more than 8 deep. |
| ROUTINE_CALL | Runs a routine with its parameters (see [Routines](#routines)). | The routine fails (the reason is given), or its name is unknown. |
| PARALLEL | Runs its sub-steps together. Done when they are all done. | Any sub-step fails (the reasons are given). |

Fill in **Verify Tag** in a VERIFIED_ACTION: a step whose verify tag is left empty can never be verified, and fails at its timeout.

**Conditions** are `==`, `>`, `<`, `>=` and `<=`. Values are compared as numbers (true is 1, false is 0), and `==` counts values within 1×10⁻⁶ of each other as equal. In the builder, a value typed as `true` or `false` is stored as true or false, a number as a number, and anything else as text.

**Sub-recipes** come from the same recipes folder and are read when the step is reached. A sub-recipe may call others, up to 8 deep: a recipe that calls itself fails there. A SUB_RECIPE can't be inside a PARALLEL step.

**PARALLEL steps** can hold ACTION, VERIFIED_ACTION, WAIT_TIME, WAIT_TELEMETRY and ROUTINE_CALL steps. Their numbers follow from the step's own: step 3's sub-steps are 301, 302 and so on. All the sub-steps start together, and the step fails as soon as one of them fails.

### Routines

A ROUTINE_CALL step runs one of four routines. The builder fills in each routine's parameters with the defaults given here; units are those of the tags.

| Routine | What it does | Parameters and defaults |
|---|---|---|
| `mass_scan` | The [Mass Scan](mass-scan.md) panel's scan, as a step. With `goto_peak`, it then sets the magnet to the most prominent peak of the scan (one standing out by at least 10 % of the scan's current range); with no such peak the magnet stays where the scan ended. The SMU goes back to 1 NPLC at the end, also after a failure, but not if you stop the recipe part way through the scan. A bad setting fails the step at once. | `start_a` 20, `stop_a` 30, `step_a` 0.05 (magnet amps), `samples` 5, `nplc` 1, `goto_peak` true. Also accepted: `tolerance`, `settle_time`, `sample_interval`, `export`, `export_path`, as on the panel. |
| `safety_relay_init` | Brings the safety relay on with the operator, by prompts (below). Done at once if the relay is already on. | `timeout_sec` 6 (for the relay to come on), `gv_timeout_sec` 15 (for the gate valve to open), `stabilization_sec` 15 (wait after the relay comes on), and the tags it reads and sends. |
| `hv_conditioning` | Ramps a supply that is already on, from its present readback up to `max_val` in steps of `step_size`, holding each step for `hold_time_sec`. While the pressure is above `pressure_max` it holds, until the pressure falls below `pressure_recover`. When the arc flag (`arc_mode_tag`) reads 1, it steps back by `backoff_step` and waits `arc_cooldown_sec`. If the pressure or arc tag has no live value, it holds, and fails after 30 s. It fails when the arcs exceed `max_arcs`. | `cmd_tag` and `rb_tag`: the extraction voltage; `max_val` 30, `step_size` 1, `hold_time_sec` 60, `pressure_tag`: gauge 1, `pressure_max` 5×10⁻⁵, `pressure_recover` 1×10⁻⁵, `arc_mode_tag`: none, `backoff_step` 2, `max_arcs` 5, `arc_cooldown_sec` 5. |
| `psu_control` | Switches a supply on (unless it is on already), sends its setpoint and, with `wait_for_readback`, waits for the readback to come within `tolerance` of it. It fails if the safety relay is off (with `requires_relay`), if the supply does not report on within 5 s, or if the readback is not within tolerance after `timeout_sec`. | `cmd_enable`, `stat_enabled`, `sp_tag`, `rb_tag`: the filament; `target_val` 5, `tolerance` 0.2, `timeout_sec` 45, `wait_for_readback` 1, `requires_relay` 1. |

Set `max_val` within the supply's range: the default, 30, is above the extraction voltage's 20 kV limit in the tag registry, so the ramp would be refused there and the step would fail.

`safety_relay_init` works through prompts. In them, "Click OK" means **ACKNOWLEDGE**.

1. If the gate valve is not open, it asks you to let it open the valve or to open it by hand. When you acknowledge, it opens the valve itself if the valve's open permissive is there, and waits up to `gv_timeout_sec`. If the permissive is missing, it asks you to clear the vacuum or gauge fault first; if the valve does not open in time, it asks you to open it by hand.
2. With the valve open, it asks you to acknowledge and then press the physical **PANEL START** button straight away.
3. When you acknowledge, it switches the safety relay enable on and waits up to `timeout_sec` for the relay. If the relay does not come on, it switches the enable off again and the step fails (**safety relay did not energise: physical PANEL START not pressed in time**).
4. Once the relay is on, it waits `stabilization_sec` before the step is done.

> [!CAUTION]
> Acknowledge the last `safety_relay_init` prompt and press **PANEL START** only when it is safe to energise the safety relay. See the [safety rules](../safety.md).

## The recipe builder

**OPEN RECIPE BUILDER** opens the builder (**Interactive Recipe Builder**) with a new recipe; **EDIT** opens the selected one. Closing the builder without saving discards your changes.

![The recipe builder with a recipe open: the AI assistant at the top, then the toolbox, the steps and the step properties](../images/dialog-recipe-builder.png)

| Control | What it does |
|---|---|
| **Recipe name** | The recipe's name. It also gives the file name (see [Saving a recipe](#saving-a-recipe)). |
| **Version** | The recipe's version. |
| **Fault policy...** | Opens the fault policy (below). |
| **SAVE RECIPE** | Checks the recipe and saves it in `config/recipes`, then closes the builder. |
| **Toolbox (double-click to add)** | Double-click a step type to add a step of that type at the end. |
| **Steps (drag to reorder)** | The steps, each as its number and comment. Drag a step to move it; select one to edit it. The steps are renumbered 1, 2, 3 ... after every change. |
| **Remove selected step** | Deletes the selected step. |
| **Step properties** | The selected step's fields. |

Every step has a **Comment/Label:**, the text shown in the step lists. The other fields depend on the type:

| Step type | Fields |
|---|---|
| ACTION | **Target Tag:**, **Command Value:** |
| VERIFIED_ACTION | **Pre-Req Tag:**, **Pre-Req Condition:**, **Pre-Req Value:**, **Command Tag:**, **Command Value:**, **Verify Tag:**, **Verify Condition:**, **Verify Value:**, **Timeout (sec):** (up to 3600) |
| WAIT_TIME | **Duration (sec):** (up to 36 000) |
| WAIT_TELEMETRY | **Target Tag:**, **Condition:**, **Target Value:**, **Timeout (sec):** (up to 36 000; 0 for none) |
| USER_PROMPT | **Popup Text:**. **Add Verify Condition** adds a condition (tag, condition and value); the cross button beside it removes it. |
| SUB_RECIPE | **Target File:**, one of the recipe files. |
| ROUTINE_CALL | **Routine:**, then one field per parameter, named after it. Choosing another routine puts in that routine's defaults. |
| PARALLEL | The type of sub-step to add, **Add Sub-Step**, the list of sub-steps, and the selected sub-step's own fields with **Delete sub-step**. |

Double-click a tag field (and any routine parameter whose name ends in `_tag`) to pick a tag from the **ISA-95 Tag Selector**: the tag registry as a tree, by area, with descriptions. Double-click a tag, or select it and press **OK**. The selector leaves out the raw fault words and the fault reset. You can also type a tag.

**Fault policy...** opens **Global Fault Policy**. **Add Specific Fault Abort** adds a line (**Trip 1:**, **Trip 2:** ...) with a list of every PLC fault in `config/fault_config.json`, by name and description. A new line starts at the first fault in the list: choose the one you want. The cross button beside a line removes it, and **Done** closes the window. Changes take effect in the builder at once. A fault that is no longer in `fault_config.json` stays in the policy and is marked as such.

### Saving a recipe

**SAVE RECIPE**:

1. refuses an empty name (**Recipe name cannot be empty.**);
2. runs the sequencer's own checks on every step. If the sequencer would refuse a step (an unknown step type, a SUB_RECIPE inside a PARALLEL step, a value it can't read as a number), the recipe is not saved, and **Recipe Not Saved** gives the reasons. These checks do not look at tags, values or the order of the steps;
3. makes the file name from the recipe name: lower case, with anything other than letters, digits, `-` and `_` turned into `_`. **Ramp 1/2** is saved as `ramp_1_2.json`;
4. if that file exists and has the same version, warns (**Traceability Warning**): press **Overwrite**, or **Cancel** and give the recipe a new version;
5. writes the file whole and then puts it in place, so a half-written recipe never appears, says so (**Success**) and closes the builder. The panel's list is read again.

A recipe opened with **EDIT** is saved under the file name made from its recipe name. If that differs from the file it came from (`master_implant.json` holds the recipe **Silicon Implantation Run**), saving writes a new file and leaves the old one as it was.

### The AI assistant

The **AI recipe assistant** at the top of the builder drafts a recipe from a description in plain words. Type what the recipe should do and press **Generate**; the button reads **...** while it works.

It sends, to Google's Gemini service over the internet: your request; the recipe now in the builder (its name, steps and fault policy); the tag registry; the PLC fault list; the names of the recipes in `config/recipes`; and the descriptions of the routines. It answers either with a message, shown in the chat (a question, or a warning such as a value outside the usual ranges it has been given), or with a recipe. A recipe replaces the name, version, fault policy and steps in the builder, and the chat says that the sequence was generated and loaded. The builder then runs the sequencer's checks on it and lists any problems under **Check these steps before saving:**. Nothing is saved until you press **SAVE RECIPE**.

The assistant's instructions ask it, among other things, never to use the fault reset, to use `psu_control` rather than plain ACTION steps for supplies, to start source, magnet and high-voltage recipes with `safety_relay_init`, and to put the safety relay trip in the fault policy. These are instructions to the model, not checks: nothing makes sure its answer follows them.

> [!WARNING]
> The assistant can get tags, values, conditions and the order of steps wrong. Read every step and the fault policy it writes before you save or run the recipe: the builder checks only that each step is one the sequencer can run.

The assistant needs the `google-genai` Python package, an API key in `GEMINI_API_KEY` (in the environment, or in a `.env` file in the SINDIE folder) and internet access from the PC. The label at the top of the assistant reads **Online** when the package is installed and a key is set (it does not test the connection), and **Offline (missing API key)** otherwise; **Generate** then shows **AI Offline**. Errors appear in the chat in red; when the service is overloaded, the assistant tries three times before it gives up.

> [!NOTE]
> Whether the lab PC has an API key and internet access, and whether the lab allows the tag list and requests to be sent to Google, is to be confirmed.

## Recipe files

Recipes are JSON files in `config/recipes/`, which is in git. The panel's list, the builder and SUB_RECIPE steps all use this folder. A recipe file holds `recipe_name`, `version`, `fault_policy` (a list) and `steps` (a list). Each step has a `step_id`, a `type`, a `comment` and the fields of its type. For example, `auto_tune.json`:

```
{
  "recipe_name": "Automated Mass Resolution",
  "version": "1.1",
  "fault_policy": [],
  "steps": [
    {"step_id": 1, "type": "ACTION", "target": "ion_beam.beamline.diagnostics.cmd_insert_fc",
     "value": true, "comment": "Ensure Faraday Cup is inserted"},
    {"step_id": 2, "type": "ROUTINE_CALL", "routine_name": "mass_scan",
     "parameters": {"start_a": 20.0, "stop_a": 30.0, "step_a": 0.05, "samples": 5, "nplc": 1.0,
                    "goto_peak": true},
     "comment": "Mass scan 20-30 A, then set the magnet on the strongest peak"},
    {"step_id": 3, "type": "WAIT_TIME", "duration_sec": 2.0, "comment": "Wait for Magnet to settle on peak"}
  ]
}
```

A fault policy entry, as the builder writes it, names the fault word and the bit (byte.bit) of the fault:

```
{"type": "BIT_FAULT", "registry_tag": "ion_beam.faults.word_0_system", "bit_str": "0.1",
 "name": "Safety_Relay_Tripped", "action": "ABORT"}
```

Only `BIT_FAULT` entries are checked; anything else in the list is ignored.

Recipes saved before October 2026 went to `src/config/recipes/`. When the panel starts, it copies any recipe from there into `config/recipes/`, never overwriting: a recipe whose file name is taken by a different file is copied as `name (from src-config).json`. Check the copies, then delete the old folder.

## See also

- [The Automation bar](automation-bar.md): control leases and taking control
- [Mass Scan](mass-scan.md)
- [System Health](system-health.md): **RESET FAULTS** and **INITIATE SHUTDOWN**
- [Alarms](alarms.md)
- [Bringing up a beam](../procedures/beam-start-up.md)
- [How the controls behave](../getting-started/controls-and-colours.md)
- [Services and messaging](../how-it-works/services.md)
