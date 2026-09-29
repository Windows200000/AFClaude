You help forecast Claude Code usage for a single session (auto-generated names like "claude-3f" carry no information; judge from the messages/usage instead).

Heavy usage typically comes from building a bigger project (for work, school or privately): coding, infrastructure, multi-step research, long documents.
Light usage typically comes from quick questions: facts, plant care, short lookups, small one-off fixes.

Session: {name}

Last {n} user messages (oldest first):
{prompts}

Tokens generated (output+thinking, by model) between each message and the next:
{gaps}

Total token usage since the last check, by model:
{totals}

Reply with ONLY a JSON object, no prose:
{{"category": "work|school|private-project|quick-question|unknown", "load": "heavy|medium|light|unknown", "confidence": 0.0}}
