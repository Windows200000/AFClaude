The owner typed this message for their project's task-manager, a Claude Code session that works on the project while the owner is away. Waking that session costs a lot, so decide:
- "now": only when the owner expects an answer or action right away: a direct question to answer, an explicit "now"/"right away"/"asap", a request to stop something that is doing damage right now, anything urgent.
- "note": everything that can wait for the session's next start: instructions, specs, design or feature requests, decisions, changes to earlier decisions, ideas, goals, feedback, preferences, and reports or observations of a bug or of something that already happened (e.g. "X ran twice", "Y showed the wrong number"), unless the owner asks for it now. A request to build or change something is "note" unless the owner says it is urgent.
When unsure, answer "note".

Message:
<<<
{text}
>>>

Reply with ONLY a JSON object, no prose:
{{"verdict": "now|note", "reason": "<one short line>"}}
