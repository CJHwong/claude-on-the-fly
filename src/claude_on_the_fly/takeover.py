"""Build a manual resume command with the conversation's saved account."""

import shlex
from pathlib import Path

from claude_on_the_fly import accounts, agent, codex_state


def _selected(
    session_uuid: str, backend: str, platform: str | None
) -> accounts.Account | None:
    platforms = (
        (platform,) if platform in ("slack", "telegram") else ("slack", "telegram")
    )
    saved = {
        name
        for source in platforms
        if (
            name := accounts._saved(
                accounts._selection_path(source, session_uuid, backend)
            )
        )
        is not None
    }
    if len(saved) > 1:
        raise accounts.AccountError(
            "Conflicting saved accounts; select the conversation's platform"
        )
    if saved:
        name = saved.pop()
        return accounts.named(name, backend) if name else None
    return accounts.default_for(backend)


def command(
    workspace: Path, session_uuid: str, platform: str | None = None
) -> str | None:
    if not accounts.all_accounts():
        return agent.get_backend().takeover_command(workspace, session_uuid)
    profile = agent.resolve_profile()
    account = _selected(session_uuid, profile.backend, platform)
    if account is None:
        return agent.get_backend().takeover_command(workspace, session_uuid)
    effective = accounts.apply_profile(profile, account)
    with accounts.use(account, mode=effective.mode):
        resume = agent.get_backend(effective).takeover_command(workspace, session_uuid)
        if resume is None:
            return None
        prefix = ["env"]
        for key in (*accounts.AUTH_OVERRIDE_ENV, "COTF_CHATGPT_BASE_URL"):
            prefix.extend(("-u", key))
        if account.backend == "codex":
            prefix.append(f"CODEX_HOME={codex_state.home_dir(workspace)}")
        elif account.home is None:
            prefix.extend(("-u", "CLAUDE_CONFIG_DIR"))
        else:
            prefix.append(f"CLAUDE_CONFIG_DIR={account.home}")
        arguments = shlex.split(resume)
        if effective.mode == "ollama":
            launcher = agent.OllamaLauncher(effective.model)
            arguments = [*launcher.prefix(effective.backend), *arguments[1:]]
        return shlex.join([*prefix, *arguments])
