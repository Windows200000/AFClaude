The owner typed this message for their project's task-manager, a Claude Code session that works on the project while the owner is away. Waking that session costs a lot, so decide:
- "now": it needs a direct response or action soon: a question, a request to do, stop or change something now, anything urgent.
- "note": it can wait until the session's next start: information, an idea or goal, feedback, a preference, something to keep in mind.
When unsure, answer "now".

Message:
<<<
{text}
>>>

Reply with ONLY a JSON object, no prose:
{{"verdict": "now|note", "reason": "<one short line>"}}
