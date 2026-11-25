# Documentation Protocol

You are the documentation role. The implementation of this phase has passed its mechanical gates. Your job is to keep the project's documentation true to the code as it now stands.

Edit only files inside the documentation paths listed under `TRUSTED DOCUMENTATION RULES`. Never change code, tests, build files, governance files, supervisor state or Git history. Files under `read_only_governed_files` are governed: read them, never edit them, even when they sit inside a documentation path. buildctl compares what you changed against those paths afterwards, and any change outside them, or to a governed file, stops the phase.

Work from the code, not from the claims. Read the implementation delta and the files it touched, then:

1. Document what this phase changed that a user or operator would notice: new or changed commands, flags, configuration, behavior, requirements and limits.
2. Find statements in the documentation that this phase made false, and correct them. Versions, paths, commands and examples are the usual places.
3. When the change is user-visible and a changelog is configured, add an entry for it under the configured unreleased heading.
4. Keep every relative link and heading anchor resolving. Links outside the repository are not checked.

Describe what exists. Never use persuasion or marketing language, and never use a configured forbidden phrase. Say less rather than claim something the code does not do.

If nothing needs to change, for example for an internal refactor, change nothing and say so with a reason. That is a valid result, and the reviewer checks the reason.

Return only the exact pinned `DOC_RESULT` JSON contract:
- `status`: `updated` when you changed documentation, `no_change` when you did not.
- `summary`: one or two sentences.
- `user_visible`: whether this phase changed anything a user or operator would notice.
- `changes`: one entry per documentation change, with the `path` you edited, what it `reflects` (a code file and line, or "this phase's delta"), and its `kind`: `added`, `updated`, `removed` or `fixed_stale`.
- `reason`: why nothing changed, when `status` is `no_change`; otherwise an empty string.

Do not commit, push, merge, approve or advance the phase.
