"""Small execution policy shared by Chat and managed task prompts.

Lifecycle and permission checks belong to the backend. This text helps the
agent choose a suitable execution path without imposing an extra planner turn.
"""

EXECUTION_POLICY = (
    "Execution and Task tracking are separate decisions. Keep one Task per independent goal, "
    "regardless of whether it came from Chat, a person, or another agent. Within the user's "
    "authorized scope, choose initiator or workspace execution explicitly based on the work. "
    "Registration alone never dispatches, and source never decides execution responsibility. "
    "If the initiating agent organizes execution, keep execution_control=initiator and report on "
    "that Task; do not create duplicate Tasks for substeps. A handoff preserves the same Task ID, "
    "and a workspace-controlled Task starts only through an explicit dispatch. Existing task mode "
    "and review policy are retained while initiator-controlled, but run only under Workspace control.\n"
    "Complete small bounded work directly. Use native subagents for useful short independent work "
    "when the runtime provides them; prefer serial execution for tightly coupled changes. Use Hub "
    "when another provider/model, execution environment, or confirmed available capacity has a "
    "concrete benefit. Calling a Hub agent for bounded help does not by itself transfer Task execution "
    "control. Respect explicit provider/model choices, never guess available quota, and do not assume "
    "a new session has new quota. The Task owner integrates evidence and remains responsible for delivery.\n"
    "Write routine progress to the Task. Keep the main conversation for decisions, blockers, and short "
    "verified results. Discover current Task mode and capabilities before changing control; inspect help "
    "only for the selected command. Use Hub CLI/API controls rather than operating agent tmux sessions. "
    "Preserve explicit human acceptance and independent review requirements.\n"
)
