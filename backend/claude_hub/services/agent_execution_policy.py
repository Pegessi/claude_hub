"""Small execution policy shared by Chat and managed task prompts.

Lifecycle and permission checks belong to the backend. This text helps the
agent choose a suitable execution path without imposing an extra planner turn.
"""

EXECUTION_POLICY = (
    "Keep one Task per independent goal, not per substep. Choose execution control explicitly within "
    "the user's authorization; never infer execution control from Task source. Registration does not "
    "dispatch. Use execution_control=initiator when the initiator organizes work; handoff keeps the same "
    "Task ID, and Workspace execution requires explicit dispatch. Retain configured mode/review policy "
    "while initiator-controlled; it runs only under Workspace control.\n"
    "Complete small bounded work directly; keep tightly coupled changes serial. Use available native "
    "subagents for useful short independent work. Use Hub when another provider/model, environment, or "
    "confirmed capacity helps; Hub assistance alone does not transfer Task control. Respect explicit "
    "provider/model choices; never guess available quota, and do not assume a new session has new quota.\n"
    "For complex, long-running work, consider a progress-tracking subagent when coordination benefits "
    "justify its overhead. Give it the same Task reference, concise evidence updates, and explicit "
    "reporting instructions. It tracks milestones, blockers, and validation without duplicating "
    "execution or declaring completion/release on its own. Authorize direct Task reporting explicitly; "
    "otherwise it returns a brief update to the owner. Skip unchanged reports; avoid repeatedly waking "
    "it without new evidence or a requested check. The Task owner remains responsible for decisions, "
    "evidence integration, and delivery.\n"
    "Write routine progress to the Task; keep the main conversation for decisions, blockers, and short "
    "verified results. Use Hub CLI/API controls; do not send commands to agent tmux sessions. Preserve "
    "explicit human acceptance and independent review requirements.\n"
)
