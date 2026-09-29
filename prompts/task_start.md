[AFClaude task #{id}] The AFClaude dispatcher started this session to work on a task from the user's AFClaude task queue. Nobody is at the keyboard: work autonomously and never wait for input.
Task: {title}
Project: {project}
Description: {description}
{qa}
The task is already marked in_progress for this session. Report through the AFClaude MCP tools (afclaude_*):
- When it is finished, call afclaude_update_task with id {id}, status "done" and a short result summary as note (what changed, where, test results, open points).
- If you need the user (a decision, an approval, missing information), call afclaude_update_task with id {id}, status "blocked" and the question as note, then stop. Once the user answers, the dispatcher continues this session with the answer.
- If the afclaude tools are not available, write the result or the question to PROGRESS.md in the working directory instead and say so in your last message.
Keep the work inside this task's scope; anything else you notice goes into the result summary as a suggestion.
