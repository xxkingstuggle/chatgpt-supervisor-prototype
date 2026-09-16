import re
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skill" / "chatgpt-supervisor"


class SkillLayoutTests(unittest.TestCase):
    def test_required_resources_and_reference_links_exist(self):
        required = [
            SKILL / "SKILL.md",
            SKILL / "agents" / "openai.yaml",
            SKILL / "references" / "protocol.md",
            SKILL / "references" / "browser.md",
            SKILL / "references" / "recovery.md",
            SKILL / "scripts" / "supervisor.py",
            SKILL / "assets" / "initial-control.md",
            SKILL / "assets" / "continuation-report.md",
            SKILL / "assets" / "protocol-repair.md",
        ]
        for path in required:
            self.assertTrue(path.is_file(), path)
        skill_text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
        for ref in re.findall(r"\]\((references/[^)]+)\)", skill_text):
            self.assertTrue((SKILL / ref).is_file(), ref)

    def test_cli_is_executable_by_current_python_without_browser_import(self):
        result = subprocess.run([sys.executable, str(SKILL / "scripts" / "supervisor.py"), "--help"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0)
        self.assertIn("浏览器由 Codex", result.stdout)
        script_text = (SKILL / "scripts" / "supervisor.py").read_text(encoding="utf-8")
        self.assertNotIn("browser.send(", script_text)
        self.assertNotIn("import cua", script_text)


if __name__ == "__main__":
    unittest.main()
