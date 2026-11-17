"""Probe the deployed Go binaries and their shared bash filesystem view."""
from pathlib import Path
import tempfile

from laxagent.sandbox.bubblewrap import run_bash
from laxagent.sandbox.file_tools import run_file_tool


def probe():
    with tempfile.TemporaryDirectory(prefix=".laxagent-file-probe-", dir=Path.home()) as directory:
        root = Path(directory).resolve()
        project = root / "project" / "probe"
        project.mkdir(parents=True)
        skills = root / "skills"
        skills.mkdir()
        mounts = ((project.parent, "/user/project"), (skills, "/user/skills"))

        def call(name, **request):
            response = run_file_tool(name, mounts, request)
            assert response["error_code"] is None, response["error_code"]
            return response["result"]

        path = "/user/project/probe/result.txt"
        call("write_file", path=path, operation="write", content="before 中文\n")
        call("edit_file", path=path, edits=[{"old_text": "before", "new_text": "after"}])
        assert call("read_file", path=path, start_line_no=1, start_byte_no=1, total_lines=1)["text"] == "after 中文\n"
        call("write_file", path="/user/skills/probe/main.py", operation="write",
             content="from pathlib import Path\nassert Path('result.txt').read_text() == 'after 中文\\n'\nPath('from-bash.txt').write_text('ok')\n")
        result = run_bash(mounts, "/user/project/probe", "python3 /user/skills/probe/main.py")
        assert result["exit_code"] == 0, "bash could not use file-tool paths"
        assert call("read_file", path="/user/project/probe/from-bash.txt", start_line_no=1,
                    start_byte_no=1, total_lines=1)["text"] == "ok"
        assert call("write_file", path=path, operation="delete", content=None)["deleted"]
        assert not (project / "result.txt").exists()
    print("PASS: sandbox file tools and bash share project and skill paths", flush=True)


if __name__ == "__main__":
    probe()
