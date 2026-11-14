#!/usr/bin/env python3
"""Offline tests for the `afclaude` host wrapper (phase 5b K5, docker/host-kit/afclaude_host.py, plan §3.3): the
docker command it builds (`docker exec -i -e NAME ... afclaude-mcp afclaude <args>`), which environment goes into
the container (only the identity inputs, the token from its file and this host's name), that the token is in no
argv, and what it refuses (any other AFCLAUDE_* variable, a token in the environment, a token file that is not
0600 and the caller's or holds no token). A stub `docker` on PATH proves the process level.

    python3 -m unittest test_afclaude_host
"""
import json
import os
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "docker", "host-kit"))
import afclaude_host as w  # noqa: E402

WRAPPER = os.path.join(HERE, "docker", "host-kit", "afclaude_host.py")
SID = "00000000-0000-4000-8000-000000000001"
TOKEN = "afc_m_7_" + "ab" * 20


class Build(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.file = os.path.join(self.tmp.name, "machine.json")

    def token_file(self, doc=None, mode=0o600, raw=None):
        with open(os.open(self.file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode), "w") as fh:
            fh.write(raw if raw is not None else json.dumps({"token": TOKEN} if doc is None else doc))
        os.chmod(self.file, mode)

    def build(self, env, argv=("tasks", "list"), cwd="/work/proj"):
        env = dict({"PATH": "/usr/bin", "HOME": "/home/x", "AFCLAUDE_MACHINE_FILE": self.file}, **env)
        return w.build(list(argv), env, cwd, "the-host", "/usr/bin/docker")

    def names(self, cmd):
        return [cmd[i + 1] for i, a in enumerate(cmd) if a == "-e"]

    def test_the_command_runs_the_arguments_unchanged_in_the_container(self):
        cmd, _ = self.build({}, argv=("mcp", "stdio"))
        self.assertEqual(cmd[:3], ["/usr/bin/docker", "exec", "-i"])
        self.assertEqual(cmd[-4:], ["afclaude-mcp", "afclaude", "mcp", "stdio"])

    def test_only_the_identity_env_goes_in(self):
        env = {"CLAUDE_CODE_SESSION_ID": SID, "CLAUDECODE": "1", "AFCLAUDE_AGENT_ID": "a1b2", "CLAUDE_GUARD_DISABLE": "1",
               "CLAUDE_CODE_ENTRYPOINT": "cli", "CLAUDE_CODE_MESSAGING_TOKEN": "secret", "FOO": "bar",
               "CLAUDE_PROJECT_DIR": "/work/other"}
        self.token_file()
        cmd, child = self.build(env)
        self.assertEqual(set(self.names(cmd)), {"CLAUDE_CODE_SESSION_ID", "CLAUDECODE", "AFCLAUDE_AGENT_ID",
                                                "CLAUDE_GUARD_DISABLE", "CLAUDE_PROJECT_DIR", w.TOKEN_VAR, w.HOST_VAR})
        self.assertEqual(child["CLAUDE_PROJECT_DIR"], "/work/other")
        self.assertEqual((child[w.TOKEN_VAR], child[w.HOST_VAR]), (TOKEN, "the-host"))

    def test_nothing_is_passed_that_is_not_set(self):
        cmd, _ = self.build({})                                        # no token file, no session
        self.assertEqual(self.names(cmd), ["CLAUDE_PROJECT_DIR"])      # the caller's directory, from its cwd

    def test_the_project_dir_is_the_callers_cwd_unless_given(self):
        _, child = self.build({}, cwd="/work/proj")
        self.assertEqual(child["CLAUDE_PROJECT_DIR"], "/work/proj")

    def test_the_token_is_in_no_argv(self):
        self.token_file()
        cmd, child = self.build({"CLAUDE_CODE_SESSION_ID": SID})
        self.assertFalse(any(TOKEN in a for a in cmd))
        self.assertEqual(child[w.TOKEN_VAR], TOKEN)

    def test_no_token_file_is_the_hosts_own_session(self):
        cmd, child = self.build({"CLAUDE_CODE_SESSION_ID": SID})
        self.assertNotIn(w.TOKEN_VAR, self.names(cmd))
        self.assertNotIn(w.TOKEN_VAR, child)
        self.assertNotIn(w.HOST_VAR, child)

    def test_any_other_afclaude_variable_is_refused(self):
        for name in ("AFCLAUDE_DB", "AFCLAUDE_DATA", "AFCLAUDE_PROJECTS_DIR", w.TOKEN_VAR, w.HOST_VAR):
            with self.assertRaises(w.Refused, msg=name) as cm:
                self.build({name: "x"})
            self.assertIn(name, str(cm.exception))

    def test_the_wrappers_own_inputs_are_not_refused(self):
        self.token_file()
        cmd, child = self.build({"AFCLAUDE_AGENT_ID": "a1", "AFCLAUDE_MACHINE_FILE": self.file})
        self.assertNotIn("AFCLAUDE_MACHINE_FILE", self.names(cmd))

    def test_a_token_in_the_environment_does_not_replace_the_file(self):
        self.token_file()
        with self.assertRaises(w.Refused):
            self.build({w.TOKEN_VAR: "afc_m_9_" + "cd" * 20})

    def test_a_bad_token_file_is_refused_not_ignored(self):
        self.token_file(mode=0o644)
        with self.assertRaises(w.Refused) as cm:
            self.build({})
        self.assertIn("0600", str(cm.exception))
        self.token_file(raw="not json")
        with self.assertRaises(w.Refused):
            self.build({})
        self.token_file(doc={"nope": 1})
        with self.assertRaises(w.Refused):
            self.build({})
        self.token_file(doc={"token": "  "})
        with self.assertRaises(w.Refused):
            self.build({})
        os.remove(self.file)
        os.symlink(os.path.join(self.tmp.name, "elsewhere"), self.file)    # a dangling link is no file, not a token
        self.assertEqual(self.names(self.build({})[0]), ["CLAUDE_PROJECT_DIR"])


class Process(unittest.TestCase):
    """The script itself, with a stub `docker` first on PATH that records what it was asked."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        t = self.tmp.name
        self.bin = os.path.join(t, "bin")
        os.makedirs(self.bin)
        self.log = os.path.join(t, "docker.json")
        stub = os.path.join(self.bin, "docker")
        with open(stub, "w") as fh:
            fh.write(textwrap.dedent(f"""\
                #!{sys.executable}
                import json, os, sys
                names = [sys.argv[i + 1] for i, a in enumerate(sys.argv) if a == "-e"]
                json.dump({{"argv": sys.argv[1:], "env": {{n: os.environ.get(n) for n in names}},
                           "stdin": sys.stdin.read()}}, open({self.log!r}, "w"))
                sys.exit(7)
                """))
        os.chmod(stub, 0o755)
        self.file = os.path.join(t, "machine.json")
        with open(os.open(self.file, os.O_WRONLY | os.O_CREAT, 0o600), "w") as fh:
            json.dump({"token": TOKEN}, fh)

    def run_wrapper(self, *args, stdin="", **env):
        e = {"PATH": self.bin + ":/usr/bin:/bin", "HOME": self.tmp.name, "AFCLAUDE_MACHINE_FILE": self.file, **env}
        return subprocess.run([sys.executable, WRAPPER, *args], input=stdin, capture_output=True, text=True, env=e,
                              cwd=self.tmp.name, timeout=30)

    def test_it_execs_docker_with_stdin_and_the_exit_code_of_the_container(self):
        r = self.run_wrapper("hook", "session-rules", "manager", stdin="payload", CLAUDE_CODE_SESSION_ID=SID)
        self.assertEqual(r.returncode, 7)                              # docker's exit code is the command's
        with open(self.log) as fh:
            seen = json.load(fh)
        self.assertEqual(seen["argv"][:2], ["exec", "-i"])
        self.assertEqual(seen["argv"][-5:], ["afclaude-mcp", "afclaude", "hook", "session-rules", "manager"])
        self.assertEqual(seen["stdin"], "payload")
        self.assertEqual(seen["env"]["CLAUDE_CODE_SESSION_ID"], SID)
        self.assertEqual(seen["env"][w.TOKEN_VAR], TOKEN)
        self.assertNotIn(TOKEN, json.dumps(seen["argv"]))

    def test_a_refusal_runs_nothing(self):
        r = self.run_wrapper("tasks", "list", AFCLAUDE_DB="/elsewhere/afclaude.db")
        self.assertEqual(r.returncode, w.EXIT_REFUSED)
        self.assertIn("AFCLAUDE_DB", r.stderr)
        self.assertFalse(os.path.exists(self.log))

    def test_no_docker_is_an_error(self):
        e = {"PATH": "/nonexistent", "HOME": self.tmp.name}
        r = subprocess.run([sys.executable, WRAPPER, "tasks"], capture_output=True, text=True, env=e, timeout=30)
        self.assertEqual(r.returncode, w.EXIT_REFUSED)
        self.assertIn("docker", r.stderr)

    def test_the_script_is_executable_and_python39_syntax(self):
        self.assertTrue(os.stat(WRAPPER).st_mode & stat.S_IXUSR)
        import ast
        with open(WRAPPER) as fh:
            ast.parse(fh.read(), feature_version=(3, 9))


if __name__ == "__main__":
    unittest.main()
