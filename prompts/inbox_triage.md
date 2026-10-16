The owner typed this message for their project's task-manager, a Claude Code session that works on the project while the owner is away. Waking that session costs a lot, so decide:
- "now": only when the owner expects an answer or action right away: a direct question to answer, an explicit "now"/"right away"/"asap", a request to stop or fix something that is going wrong, an operational command for the running system (resume, pause, start, stop, run, retry, send), anything urgent.
- "note": everything that can wait for the session's next start: instructions, specs, design or feature requests, decisions, changes to earlier decisions, ideas, goals, feedback, preferences. A request to build or change something (code, design, features) is "note" unless the owner says it is urgent; operational commands are always "now".
When unsure, answer "note".

Message:
<<<
{text}
>>>

Reply with ONLY a JSON object, no prose:
{{"verdict": "now|note", "reason": "<one short line>"}}
