# Codex capacity simulation

Run the simulation from the repository root:

```sh
uv run python evals/codex-capacity/simulate.py
```

The simulation starts a local provider and launches the real Codex CLI through `CodexBackend.run`.
It uses temporary configuration, transcript, workspace, and daemon directories.
The provider requests one tool action, then returns two terminal `server_overloaded` errors.
The backend waits 10 seconds, then 30 seconds, and resumes the same thread.
The provider then returns `CAPACITY_RETRY_OK`.
Assertions require a successful reply and exactly one recorded tool action.
The script prints the directory that contains its transcripts and action record.

## Avery run on 2026-10-29

The isolated run used Codex CLI 0.163.0 on `avery`.
The complete backend returned `CAPACITY_RETRY_OK` after 40.923 seconds.
Both retries used thread `01a1859c-36da-7801-b3f6-cf7ca2649e6f`.
The action record contained one line, and the response counted one `CommandExecution`.
The response counted 40 input tokens and 8 output tokens from the simulated provider.

The run used a temporary copy of the source code.
The production daemon still uses its installed version.
