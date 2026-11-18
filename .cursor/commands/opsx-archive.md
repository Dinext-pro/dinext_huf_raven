---
name: "/opsx-archive"
id: "opsx-archive"
category: "Workflow"
description: "Verify completed Dinext work, sync accepted requirements and archive with the OpenSpec CLI"
---

# Archive a Dinext change

Use this workflow from the terminal in Cursor or Codex. The Cursor shortcut invokes the same steps. Maintain the canonical file in `dinext-docs/.cursor/commands/`.

1. Select the named change. If the name is ambiguous, run `openspec list --json` and resolve which change the user intends. Run the following commands from the connected consumer or Store. Set the shell variable `change` to the selected ID (for example, `change=fix-kanban-filter`). Carry the same explicit `--store` selector across calls if one was used.

   ```bash
   openspec status --change "$change" --json
   openspec instructions archive --change "$change" --json
   openspec instructions apply --change "$change" --json
   ```

   Use the paths returned by status. Read the archive context and apply relevant operation guidance within the central Dinext rules.

2. Verify that proposal, design, delta specs and tasks all report `done`. Read the task list and its evidence. Require `all_done` from the apply instructions and completed tasks for every affected repository. Require successful Cypress results for changed screens, or the entry `Cypress: not applicable — <reason>` for a change without a screen. Keep the change active while any required artifact, task or acceptance check is outstanding.

3. Read every delta listed in `artifactPaths.specs.existingOutputPaths` and compare it to the main spec under `<planningHome.root>/openspec/specs/<capability>/spec.md`. Obtain valid JSON from `openspec instructions specs --change "$change" --json` before writing a main spec. Follow the central `opsx-sync.md` workflow inline using that rules snapshot. Preserve content outside the delta. Compare every delta to the resulting main spec, including added, modified, removed and renamed requirements. Run `openspec validate <capability> --type spec --strict` for each affected main spec. Any remaining difference or validation failure keeps the change active.

4. Once the sync and all checks have succeeded, archive with the official CLI.

   ```bash
   openspec validate "$change" --type change --strict
   openspec archive "$change" --skip-specs --yes
   ```

   Here `--skip-specs` avoids applying deltas that step 3 already merged and verified. A failed validation or archive must be resolved before reporting success.

5. Check the archive path returned by the CLI and confirm the active list no longer includes this change. Report the completed tasks, applicable test results, synchronized capabilities and archive location.
