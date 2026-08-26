"""
The unit file and the sudoers rule must not contradict each other.

They did, silently, for every release that had both. `sudo` is setuid;
`NoNewPrivileges=true` forbids setuid escalation. With both in place the
player asked for a restart and sudo answered

    sudo: The "no new privileges" flag is set, which prevents sudo from
    running as root.

so restart, reboot, shutdown, update and os_update — every command that
touches the system — failed at the first syscall on every device. Dashboard
showed the commands delivered, the device recorded them as run, and nothing
in any test suite had an opinion, because neither file is code.

These tests give them one.
"""

from __future__ import annotations

import re
from pathlib import Path

OPS = Path(__file__).resolve().parent.parent / "ops"
UNIT = OPS / "adnova-player.service"
SUDOERS = OPS / "sudoers-adnova-player.template"


def _directives(unit: Path) -> dict[str, str]:
    """The unit's live directives, ignoring comments and blank lines."""
    found: dict[str, str] = {}
    for line in unit.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("["):
            continue
        if "=" in line:
            key, _, value = line.partition("=")
            found[key.strip()] = value.strip()
    return found


def test_the_player_may_actually_use_sudo():
    """
    The one that would have caught it.

    Granting a sudo rule and then forbidding setuid escalation is not
    defence in depth — it is a feature that cannot run, reported as if it did.
    """
    directives = _directives(UNIT)

    assert directives.get("NoNewPrivileges", "").lower() != "true", (
        "adnova-player.service sets NoNewPrivileges=true while the sudoers "
        "template grants it systemctl. sudo is setuid, so with that flag every "
        "granted command fails with 'the \"no new privileges\" flag is set'. "
        "Drop one or the other — see the comment in the unit file."
    )


def test_the_sudo_rule_still_exists_and_is_a_closed_list():
    """
    The flag is gone, so this list is now the whole escalation boundary.

    It must stay exact: no wildcards, no ALL, nothing that widens past the
    named invocations.
    """
    rule = [
        line for line in SUDOERS.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert len(rule) == 1, "the sudoers template should hold exactly one rule"
    line = rule[0]

    assert "NOPASSWD:" in line
    assert "*" not in line, "a wildcard here would widen the boundary to anything"

    commands = line.split("NOPASSWD:", 1)[1]
    assert "ALL" not in commands, "NOPASSWD: ALL would grant the whole system"

    # Every granted invocation is an absolute path to systemctl and nothing else.
    for command in commands.split(","):
        command = command.strip()
        assert command.startswith("/usr/bin/systemctl "), (
            f"unexpected command in the sudo rule: {command!r}"
        )


def test_every_sudo_command_the_player_runs_is_granted():
    """
    The player's closed map and the sudoers rule are two lists that have to
    agree. `systemctl poweroff` was in one and not the other, which is how the
    shutdown button came to fail for reasons unrelated to the flag above.
    """
    agent = (Path(__file__).resolve().parent.parent
             / "src" / "adnova_player" / "agent.py").read_text(encoding="utf-8")

    granted = SUDOERS.read_text(encoding="utf-8")

    # Every ["sudo", "-n", "systemctl", ...] argv the agent can run.
    wanted = set()
    for match in re.finditer(r'\["sudo",\s*"-n",\s*([^\]]+)\]', agent):
        parts = re.findall(r'"([^"]+)"', match.group(1))
        wanted.add("/usr/bin/" + " ".join(parts))

    assert wanted, "no sudo invocations found in agent.py — has the shape changed?"

    for command in sorted(wanted):
        assert command in granted, (
            f"the player runs {command!r} but the sudoers template does not "
            "grant it; that command will fail with a bare sudo error"
        )
