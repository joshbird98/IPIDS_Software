# Releasing to the lab

How a new version of the software reaches the lab PC: the `lab` branch, what the launcher checks before it installs a version and while it does, what has to be installed by hand, and the first update to a version with the updater. For the operator's side, see [Updating the software](../getting-started/starting-the-software.md#updating-the-software).

## The lab branch

The lab PC installs only what is on the branch `lab` on GitHub (`joshbird98/IPIDS_Software`). To release a version:

1. Wait for its GitHub tests to pass (the **Actions** tab, or the tick beside the commit). The launcher shows a version as soon as it is on `lab`, but won't install it until its tests have passed on it.
2. Move `lab` to it, from the branch you work on:

    ```
    git push origin HEAD:lab
    ```

    or, for another commit, `git push origin <commit>:lab`. GitHub refuses the push if it isn't a fast-forward: `lab` only ever moves forward.

The launcher on the lab PC finds the new version at its next daily check, or when someone presses **Check for updates**, and offers it. Nothing is installed until an operator chooses to.

To take a release back, don't move `lab` backwards: release a commit that reverts it. On the lab PC, **Go back...** returns to the version before the last update at once.

Never rewrite `lab` (a forced push, or a rebase of commits already on it). The lab PC would then find that its version has changes the branch doesn't have, and refuse to update until someone sorts it out by hand.

## What the launcher checks first

The update window lists whatever of these is the case (`check()` in `src/launcher/updater.py`), and the launcher won't install the version:

| Blocks the update | Why |
|---|---|
| A change under `plc/generated/` | The PC-PLC data layout. The PLC program has to match it before the PC reads the PLC differently, or the PLC refuses the PC's reads. Install by hand, after the PLC (below). |
| A change to the `plc` section of `config/network_config.json` | Where the PC finds the PLC: its address, rack, slot and data blocks. Install by hand. |
| A change to `requirements.txt` | pip can't replace a package that the running programs hold open. Install by hand (below). |
| Anything in `config/lab/` | The lab PC's own settings. That folder must never be in git. |
| The GitHub tests failed, haven't finished, or can't be found out | Asked of the GitHub API (`/actions/runs?head_sha=<commit>`): each workflow must have passed in some run on the commit, by its own conclusion (a job skipped because the one before it failed makes the workflow fail; a cancelled run doesn't pass). A pass is remembered, so the API is asked once for each version that passed (it answers 60 requests an hour per address); a failure is asked again at each check, as a run made again can pass. |
| A change on the lab PC to the software itself | Not on the `lab` branch: committed there, or not committed. Someone has to decide what becomes of it. |
| A recipe or data file changed, or deleted, on the lab PC that the update changes too | It would be overwritten. (The same change on both sides is no matter.) |
| A file the update adds that is on the lab PC already, and isn't the same | Untracked or git-ignored: it would be overwritten. |
| A config file deleted on the lab PC, or broken, or a lab file that can't be read | |
| git in the middle of a merge or a rebase, or with a conflict not resolved | |

These are only noted:

- a change to `plc/` outside `plc/generated/` (the tools), or to `config/fault_config.json` (the fault texts; the PLC program sets the bits);
- the update changes a config value that the lab PC has its own value for: the lab PC's value is kept;
- the lab PC has config files changed (by hand, or committed by the config editor before October 2026): the update turns them into lab settings;
- the lab PC has commits of its own: they stay on the branch they are on (a branch named `sindie-pc-<time>` if they were on `lab`);
- the update stops tracking a recipe or data file (`git rm --cached` in the repository): it stays on the lab PC as it is, no longer tracked - also when the data compactor has rewritten it; one the compactor deleted stays deleted;
- the update brings a recipe or data file that the lab PC has a version of its own of: the lab PC's is kept;
- a file deleted on the lab PC that the move to the new version writes (one deleted in its own commits, say) comes back.

A file on the lab PC that is the same as one the update adds is moved into `data/updates/<time>/set_aside/in_the_way/`, and the update's takes its place.

## What an update does

`apply()` in `src/launcher/updater.py`, step by step. If a step fails, everything before it is undone.

1. Checks that no other update runs on this PC (an OS lock on `data/updates/update.lock`: the real system's launcher, the simulator's and the command line share the software), that nothing has moved since the check (the lab PC's version and branch, the `lab` branch, the lab PC's own files), that no other git holds the folder (`.git/index.lock`: it waits 10 s), and the machine once more: the launcher checks its conditions again as **Update now** is pressed. If any of these fails, nothing has been done.
2. Stops the services (if they run). Which of them were ONLINE has to be known (the service manager's latest report). They are recorded as stopped on purpose, and while an update runs no launcher starts them by itself.
3. Keeps what it may change, in `data/updates/<time>/`: git's index (as a tree, `index_tree` in `update.json`), copies of the lab PC's changed config files and of its own files the move writes, its lab files, and the tag registry. Nothing of the lab PC's goes into git.
4. Builds the tag registry with this version's own code (`src/utils/build_registry.py --out`, on a copy of the registry in use, so that its hand-edited fields are kept). The registry in use must match it where the PLC's tags are: if it doesn't, it has gone stale (a PLC interface pulled without a rebuild, or an edit by hand) and the update would change how the PC talks to the PLC, so it stops - rebuild it by hand first. A version from before 17 October 2026 ignores `--out` and builds the registry in use: that one is put back. Then runs this version's own check.
5. Turns the lab PC's own config changes into lab settings: what the programs used, as differences from the repository's file.
6. Goes to the new version. The lab PC's recipes and data files that it stops tracking (or brings a version of its own of) are moved out of git's way first and put back after; files the same as ones it adds are set aside. The branch `lab` is set at the new version and checked out (its own commits on `lab`, if any, are kept on `sindie-pc-<time>` first), and every file the new version changes is written: `git checkout <version> -- <those files>`, with the files it deletes moved aside. Each of those files is then compared with the new version: a file a program holds open (Excel, an antivirus) isn't replaced, and git can still say it worked - the update stops and is undone. The lab PC's own recipes are written back if the move changed them.
7. Builds the new version's tag registry with its own code, and compares the PLC's tags: if any tag's data block, byte, bit, data type or write access differs, or a tag has been added or removed, the PC would talk to the PLC differently, and the update is undone. (The builder takes a PLC tag's data type from the PLC interface, never from the registry it builds on.)
8. Runs the new version's own check (`python -m src.launcher.updater --self-check`, in a process of its own): every Python file compiles, the services and programs import, every config file, with the lab settings, passes its checks. Problems that this version had too don't count; any new one undoes the update.
9. Starts the services again (if they ran), and waits up to two minutes for every service that was ONLINE before (one the new version's service manager doesn't run counts as not back), and for the PLC to answer. From the simulator's launcher, the simulator's registry is built again by the new version first.

Going back runs the same steps, to the version before the last update. Stopping it half way (Ctrl+C at the command line) undoes it first.

Undoing stops the new version's services (a service manager that doesn't answer is ended, with its services), then puts back each part on its own, so that one that can't be done doesn't stop the others: git (HEAD and the branch the lab PC was on; the branch `lab` and its upstream as they were - a `lab` the update made is taken away, and so is the backup branch; the files the update changed, written from the version before; and git's index, staged changes and all), the lab PC's own files (byte for byte), the lab files and the registry. Files the update didn't change - the data compactor's, say - aren't touched. Then it checks that each part is as it was, judging git by what it left rather than what it said. Nothing is deleted: a file that has to make way is moved into `data/updates/<time>/set_aside/`. A git ended for taking too long (5 minutes to write the files) leaves its `index.lock`: that is set aside as `index.lock.stale-<time>`.

If anything isn't as it was, the services are left stopped, the operator is told to call you, and no launcher starts them by itself: the launcher's Services box says **HELD** until someone presses **Start services** and confirms (`release_hold`, written in `update.json`). The same holds after an update that never finished (the launcher stopped half way through it: a crash, a power cut). While an update runs, the launcher can't be closed, and if Windows logs off or shuts down, the launcher holds it up (Windows says a program is preventing it) and writes it in `update.json`.

The launcher that ran the update then starts again (`main_launcher.py --after-update`), so that it runs the new version too. (From the simulator's launcher, without the simulator's registry: it builds its own.)

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

    The builder takes each PLC tag's data type from the PLC interface (`plc/generated/PLC_PC_Interface.scl`), and prints any that differs from the registry it rebuilds.

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

From then on, updates come through the launcher's **Software** box. (After **Go back...** to a version from before the updater, the next update is made this way again: that version's launcher has no updater.)

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
| `data/updates/state.json` | The last check, the history of updates, the GitHub tests it has seen pass, the version it last told the tray about, the version the lab PC went back from. Changed by one program at a time (`state.lock`). |
| `data/updates/update.lock` | Held while an update runs. |
| `data/updates/<time>/update.json` | What one update did: from and to, by whom, each step, git's index before (`index_tree`), and how it ended; `released` if someone started the services after one that failed or never finished. |
| `data/updates/<time>/` | The copies it kept: `worktree/` (the lab PC's changed files), `lab/` (its lab files before), `system_tags.json` (the registry before), `registry_before.json` and `registry_after.json` (the two it compared), `held/` (the lab PC's files held out of git's way while it runs), `set_aside/` (files that had to make way: `in_the_way/`, the same as the update's; `removed_by_the_update/`; and when undoing, `from_the_new_version/` and the like). |
| Branches `sindie-pc-<time>` | The lab PC's own commits on `lab`, before an update moved it (only if no other branch has them). |
| `SystemLogs/launcher.log` | Every step, and why one failed. |

`data/updates/` and `config/lab/` are git-ignored.

## When changing the software

- Keep `python -m src.launcher.updater --self-check` working, with its problems as a JSON list on the last line of its output: every update runs the new version's own check. A version whose check doesn't run isn't installed.
- Keep `src/utils/build_registry.py --out <file>` working: both versions' registries are built with it.
- A version that changes where the PC finds the PLC (`config/network_config.json`'s `plc`) is installed by hand, like a PLC data layout change.
- The launcher that installs a version runs the version before. A change to the update itself takes effect from the update after the one that installs it.
- Never commit `config/lab/` or `data/updates/`.

## See also

- [Configuration files and the config editor](configuration.md#lab-settings): lab settings
- [Tests](testing.md)
- [The PLC program](../how-it-works/plc.md)
