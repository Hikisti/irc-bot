"""The deploy's restart decision (.github/needs-restart.sh), run against a throwaway git repository: a push
that changes only files the bot never runs leaves the bot running; anything else restarts it."""
import itertools
import os
import subprocess

import pytest

SCRIPT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".github", "needs-restart.sh")
GIT_ENV = {
    "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "test@example.com",
    "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
}


@pytest.fixture
def repo(tmp_path):
    def git(*args):
        result = subprocess.run(["git", *args], cwd=tmp_path, env={**os.environ, **GIT_ENV},
                                capture_output=True, text=True, check=True, timeout=30)
        return result.stdout.strip()

    def write(path, text="x\n"):
        full = tmp_path / path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(text)

    def commit(*paths, remove=(), rename=None):
        for path in paths:
            write(path, f"{path} {next(edits)}\n")  # new content every time, so the file really changes
        for path in remove:
            git("rm", "-q", path)
        if rename:
            git("mv", *rename)
        git("add", "-A")
        git("commit", "-q", "-m", "change")
        return git("rev-parse", "HEAD")

    edits = itertools.count()
    git("init", "-q")
    for path in ("README.md", "src/irc_bot.py", "tests/test_x.py", "channels.toml", "requirements.txt"):
        write(path)
    git("add", "-A")
    git("commit", "-q", "-m", "start")
    return tmp_path, commit, git("rev-parse", "HEAD")


def decide(cwd, started_from):
    result = subprocess.run(["sh", SCRIPT, started_from], cwd=cwd, capture_output=True, text=True, timeout=30)
    assert result.returncode in (0, 1), result.stderr
    return result.returncode == 0, result.stdout.strip()


@pytest.mark.parametrize("paths", [
    ("README.md",), ("CLAUDE.md",), ("tests/test_x.py",), ("tools/goal_probe.py",),
    (".github/workflows/ci-cd.yml",), ("ruff.toml", "pytest.ini", "requirements-dev.txt", ".gitignore"),
])
def test_a_push_of_files_the_bot_never_runs_does_not_restart(repo, paths):
    cwd, commit, started = repo
    commit(*paths)
    restart, reason = decide(cwd, started)
    assert not restart and reason.startswith("no restart needed")


@pytest.mark.parametrize("paths", [
    ("src/irc_bot.py",), ("src/aijamatto.txt",), ("channels.toml",), ("requirements.txt",),
    ("some_new_file.cfg",),  # a file the list does not know: restart to be safe
    ("README.md", "src/irc_bot.py"),
])
def test_a_push_that_touches_what_the_bot_runs_restarts(repo, paths):
    cwd, commit, started = repo
    commit(*paths)
    restart, reason = decide(cwd, started)
    assert restart and paths[-1] in reason


def test_a_file_removed_from_src_restarts(repo):
    cwd, commit, started = repo
    commit(remove=("src/irc_bot.py",))
    assert decide(cwd, started)[0]


def test_a_file_moved_out_of_src_restarts(repo):
    cwd, commit, started = repo
    commit(rename=("src/irc_bot.py", "tests/irc_bot.py"))
    assert decide(cwd, started)[0]


def test_a_code_change_the_bot_was_never_restarted_for_is_still_owed(repo):
    # A deploy that updated the code but failed before its restart: the bot still runs `started`, so a later
    # docs-only push compares with that, not with the code push, and restarts.
    cwd, commit, started = repo
    commit("src/irc_bot.py")
    commit("README.md")
    assert decide(cwd, started)[0]


def test_nothing_new_since_the_bot_started_does_not_restart(repo):
    cwd, _, started = repo
    assert not decide(cwd, started)[0]


@pytest.mark.parametrize("started_from", ["", "0123456789abcdef0123456789abcdef01234567", "not-a-commit"])
def test_an_unknown_starting_commit_restarts(repo, started_from):
    cwd, commit, _ = repo
    commit("README.md")
    restart, reason = decide(cwd, started_from)
    assert restart and "not known" in reason
