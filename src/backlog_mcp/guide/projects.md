# Projects

Use owner/slug names. A connection can have project scope through `?project=` in HTTP
or `--project` on stdio. Pass project explicitly when no connection scope exists.
Local connections can resolve a registered working directory.
Board reads and registration never change connection scope.

Begin with `project_board(project=<absolute local path>)`. Verify principal and roots.
If the binding is missing or wrong, use project_list, including all_computers=true,
before registering an existing logical project with project_save(project, root).
Do not access paths belonging to another computer.

Projects are private until shared. Owners manage sharing and deletion;
editors change content; readers read. project_members shows roles and roots.
Shared memory does not bypass membership. Archive to retain history.
Project deletion is irreversible and requires user confirmation.
