# Recipe sketches (not runnable)

`condition_hv_supply.json` - an early design for HV conditioning, moved here from
`data/recipes/`. It uses `LOOP` steps and `{parameter}` templating, which the recipe engine
does not support. HV conditioning is now the `hv_conditioning` routine (`HVConditioningRoutine` in `src/core/routines.py`),
called from a recipe with a `ROUTINE_CALL` step.

Recipes the HMI can run live in `config/recipes/`.
