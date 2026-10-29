# Releasing to the lab

How a new version of the software reaches the lab PC: the `lab` branch, what the launcher checks before it installs a version and while it does, what has to be installed by hand, and the first update to a version with the updater. For the operator's side, see [Updating the software](../getting-started/starting-the-software.md#updating-the-software).

## The lab branch

The lab PC installs only what is on the branch `lab` on GitHub (`joshbird98/IPIDS_Software`). To release a version:

1. Wait for its GitHub tests to pass (the **Actions** tab, or the tick beside the commit). The launcher won't offer a version until every test job has passed on it.
2. Move `lab` to it, from the branch you work on:

    ```
    git push origin HEAD:lab
    ```

    or, for another commit, `git push origin <commit>:lab`. GitHub refuses the push if it isn't a fast-forward: `lab` only ever moves forward.

The launcher on the lab PC finds the new version at its next daily check, or when someone presses **Check for updates**, and offers it. Nothing is installed until an operator chooses to.

To take a release back, don't move `lab` backwards: release a commit that reverts it. On the lab PC, **Go back...** returns to the version before the last update at once.

Never rewrite `lab` (a forced push, or a rebase of commits already on it). The lab PC would then find that its version has changes the branch doesn't have, and refuse to update until someone sorts it out by hand.

## What the launcher checks first

The update is offered only if none of these is the case (`check()` in `src/launcher/updater.py`). Each one is shown in the update window, and the launcher won't install the version:

| Blocks the update | Why |
|---|---|
| A change under `plc/generated/` | The PC-PLC data layout. The PLC program has to match it before the PC reads the PLC differently, or the PLC refuses the PC's reads. Install by hand, after the PLC (below). |
| A change to `requirements.txt` | pip can't replace a package that the running programs hold open. Install by hand (below). |
| Anything in `config/lab/` | The lab PC's own settings. That folder must never be in git. |
| The GitHub tests failed, haven't finished, or can't be found out | Asked of the GitHub API (`/commits/<sha>/check-runs`): every test job must have passed in some run on the commit. A final answer is remembered, so the API is asked once per version (it answers 60 requests an hour per address). |
| A change on the lab PC to the software itself | Not on the `lab` branch: committed there, or not committed. Someone has to decide what becomes of it. |
| A recipe or data file changed on the lab PC that the update changes too | It would be overwritten. |
| A file the update adds that is on the lab PC already | Untracked or git-ignored: git would refuse, or overwrite it. |
| A config file deleted on the lab PC, or broken, or a lab file that can't be read | |
| git in the middle of a merge or a rebase | |

These are only noted:

- a change to `plc/` outside `plc/generated/` (the tools), or to `config/fault_config.json` (the fault texts; the PLC program sets the bits);
- the update changes a config value that the lab PC has its own value for: the lab PC's value is kept;
- the lab PC has config files changed (by hand, or committed by the config editor before October 2026): the update turns them into lab settings;
- the lab PC has commits of its own: they stay on the branch they are on (a branch named `sindie-pc-<time>` if they were on `lab`);
- the update stops tracking a recipe or data file (`git rm --cached` in the repository): it stays on the lab PC as it is, no longer tracked.

## What an update does

`apply()` in `src/launcher/updater.py`, step by step. If a step fails, everything before it is undone.

1. Checks that nothing has moved since the check (the lab PC's version, the `lab` branch, the lab PC's own files).
2. Stops the services (if they run), recorded as stopped on purpose so that no launcher starts them again meanwhile.
3. Keeps what it may change: the lab PC's own changes to tracked files (the data compactor's, say) as a git stash commit, `refs/sindie/<time>`; and in `data/updates/<time>/`, copies of its changed config files, its lab files and the tag registry.
4. Builds the tag registry with this version's own code (`src/utils/build_registry.py --out`, on a copy of the registry in use, so that its hand-edited fields are kept), and runs this version's own check.
5. Turns the lab PC's own config changes into lab settings: what the programs used, as differences from the repository's file.
6. Checks out the new version, as the branch `lab` (`git checkout -B lab <version>`, which refuses to overwrite a local change). The lab PC's recipes and data files that the new version stops tracking are moved out of git's way first and put back after, so that git doesn't delete them; its own recipes are written back if the checkout changed them.
7. Builds the new version's tag registry with its own code, and compares the PLC's tags: if any tag's data block, byte, bit, data type or write access differs, or a tag has been added or removed, the PC would talk to the PLC differently, and the update is undone.
8. Runs the new version's own check (`python -m src.launcher.updater --self-check`, in a process of its own): every Python file compiles, the services and programs import, every config file, with the lab settings, passes its checks. Problems that this version had too don't count; any new one undoes the update.
9. Starts the services again (if they ran), and waits up to two minutes for every service that was ONLINE before, and for the PLC to answer.

Going back runs the same steps, to the version before the last update.

Undoing puts back git (the branch the lab PC was on, at its version, checked out again in full), the lab PC's own changes to tracked files (the stash commit, applied again), the lab files and the registry, then starts the services again. Nothing is deleted: a file that has to make way is moved into `data/updates/<time>/set_aside/`. If undoing fails, the services are left stopped and the operator is told to call you.

The launcher that ran the update then starts again (`main_launcher.py --after-update`), so that it runs the new version too.

## Installing by hand

A version that changes the PC-PLC data layout or the Python packages, or any time the launcher can't be used:

1. Stop the services (the launcher's **Stop services...**), and close the HMI, the data viewer and the launcher.
2. For a PLC data layout change: download the matching PLC program to the PLC (TIA Portal).
3. For new packages: `pip install -r requirements.txt`, with the project's Python.
4. In the SINDIE folder:

    ```
    git fetch origin
    git checkout lab
    git merge --ff-only origin/lab
    python src/utils/build_registry.py
    ```

5. Open the launcher, press **Start services**, check that every service comes ONLINE, and press **RESET FAULTS** in **System Health**.

## The first update to this version

A version from before 29 October 2026 has no updater, and may have config changes committed by its config editor. Install the first version with the updater from a second copy of it, so that the new updater does the work:

1. Move `lab` to the version (above).
2. On the lab PC: **Stop services...** in the launcher, and close the HMI, the data viewer and the launcher (tray icon, **Close the launcher**).
3. In a command window in the SINDIE folder (`C:\Users\100kV\PycharmProjects\IPIDS_Software`), with `<python>` the project's Python (shown at the bottom of the launcher, as `python.exe`, not `pythonw.exe`):

    ```
    git fetch origin
    git worktree add --detach ..\sindie-update origin/lab
    cd ..\sindie-update
    <python> -m src.launcher.updater --root ..\IPIDS_Software --apply
    ```

    It shows what the update brings and anything in its way, waits a minute with the services stopped, and asks for `UPDATE`. Then it installs it as the launcher would, without starting the services: the config changes made on the lab PC become its lab settings, and the lab PC is left on the `lab` branch.
4. Remove the second copy: `cd ..\IPIDS_Software`, then `git worktree remove ..\sindie-update`.
5. Open the launcher (the **SINDIE** shortcut), press **Start services**, check that every service comes ONLINE, and press **RESET FAULTS** in **System Health**.

From then on, updates come through the launcher's **Software** box.

## From a command window

```
python -m src.launcher.updater                  what an update would bring (asks GitHub)
python -m src.launcher.updater --no-fetch       ... from what was fetched last
python -m src.launcher.updater --apply          install it: the services stopped, the launcher closed
python -m src.launcher.updater --root <folder>  ... for another SINDIE folder (the first update)
python -m src.launcher.updater --self-check     this version's own check: its problems, as JSON
```

## What it keeps

| Where | What |
|---|---|
| `data/updates/state.json` | The last check, the history of updates, the GitHub test results it has had, the version it last told the tray about, the version the lab PC went back from. |
| `data/updates/<time>/update.json` | What one update did: from and to, by whom, each step, and how it ended. |
| `data/updates/<time>/` | The copies it kept: `worktree/` (the lab PC's changed files), `lab/` (its lab files before), `system_tags.json` (the registry before), `registry_before.json` and `registry_after.json` (the two it compared), `set_aside/` (files that had to make way). |
| `refs/sindie/<time>` | The lab PC's own changes to tracked files before an update, as a git stash commit (`git stash apply refs/sindie/<time>` puts them back by hand). Made only when there were any. |
| Branches `sindie-pc-<time>` | The lab PC's own commits on `lab`, before an update moved it. |
| `SystemLogs/launcher.log` | Every step, and why one failed. |

`data/updates/` and `config/lab/` are git-ignored.

## When changing the software

- Keep `python -m src.launcher.updater --self-check` working, with its problems as a JSON list on the last line of its output: every update runs the new version's own check. A version whose check doesn't run isn't installed.
- Keep `src/utils/build_registry.py --out <file>` working: both versions' registries are built with it.
- The launcher that installs a version runs the version before. A change to the update itself takes effect from the update after the one that installs it.
- Never commit `config/lab/` or `data/updates/`.

## See also

- [Configuration files and the config editor](configuration.md#lab-settings): lab settings
- [Tests](testing.md)
- [The PLC program](../how-it-works/plc.md)
