# The buildctl manual

buildctl is a supervisor for agent-built software. You write a roadmap of phases. One agent implements each phase, a second reviews it adversarially, your acceptance commands run in a sandbox with no network, and only what passed is committed. A human approves the phases that matter. buildctl owns the state, the git history and the budget; agents never touch any of them.
