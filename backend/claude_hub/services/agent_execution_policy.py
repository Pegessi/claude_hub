"""Small execution policy shared by Chat and managed task prompts.

Lifecycle and permission checks belong to the backend. This text helps the
agent choose a suitable execution path without imposing an extra planner turn.
"""

EXECUTION_POLICY = (
    "Execution choice: complete small or urgent bounded work directly. Use the current provider's "
    "native subagents for useful short parallel work only when those tools are actually available; "
    "otherwise work serially. Delegate through Hub when a different provider/model, independently "
    "available quota, execution environment, or durable ownership has a concrete benefit. Respect "
    "the user's provider/model choices; a smaller model is not proof of lower total cost. Context "
    "pressure, exhausted provider quota, and a task budget are different constraints; do not assume "
    "another session has independent quota or silently switch providers.\n"
    "Decide persistence separately from execution: use a linked Task for work that must continue "
    "independently, recover across sessions, wait for external events, or track staged delivery. "
    "A Task owner can work directly or use native subagents; neither delegation nor a long answer "
    "alone requires another Task. An assigned Task already provides that persistence. Keep tightly "
    "coupled work together and preserve explicit review "
    "requirements. The owning agent integrates evidence and remains responsible for delivery.\n"
)
