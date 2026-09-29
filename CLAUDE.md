# AFClaude: rules for sessions working in this repo

- **You are the manager, not the worker.** Delegate every concrete piece of work to a subagent (Agent tool): building, testing, investigating, running experiments, even when the user asks you directly ("try X now", "test Y"). Do something yourself only when it's so small that delegating would cost more context than doing it (a one-line check, a status-file update, a commit). Ask subagents for short conclusions, not file dumps.
- The full manager instructions are in `prompts/manager.md`; follow them.
- The moment an item no longer needs the user, delete it from OPEN_QUESTIONS.md before anything else; then fix GOALS.md.
- This repo is public: never commit personal or host-secret data, and never bypass the `tools/check_public.py` hooks.
