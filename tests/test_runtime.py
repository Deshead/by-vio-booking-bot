import queue
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CHILD_SCRIPT = """
import sys
from pathlib import Path
from app.runtime import AlreadyRunningError, single_instance

try:
    with single_instance(Path(sys.argv[1])):
        print("acquired", flush=True)
        if sys.argv[2] == "hold":
            sys.stdin.read()
except AlreadyRunningError:
    print("already running", flush=True)
    sys.exit(23)
"""


class SingleInstanceTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.lock_path = Path(directory.name) / ".bot.lock"

    def command(self, mode):
        # Bypass Windows venv's launcher so Popen.pid identifies the lock owner.
        interpreter = getattr(sys, "_base_executable", sys.executable)
        return [interpreter, "-c", CHILD_SCRIPT, str(self.lock_path), mode]

    def start_owner(self):
        process = subprocess.Popen(
            self.command("hold"),
            cwd=PROJECT_ROOT,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        def cleanup():
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=10)

        self.addCleanup(cleanup)
        output = queue.Queue()
        reader = threading.Thread(
            target=lambda: output.put(process.stdout.readline()), daemon=True
        )
        reader.start()
        try:
            line = output.get(timeout=10)
        except queue.Empty:
            self.fail("Lock owner did not report readiness within 10 seconds")
        if line != "acquired\n":
            _, errors = process.communicate(timeout=10)
            self.fail(f"Lock owner failed to start: {line!r} {errors}")
        reader.join(timeout=1)
        return process

    def run_contender(self):
        return subprocess.run(
            self.command("once"),
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=10,
        )

    def assert_can_acquire(self):
        result = self.run_contender()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "acquired\n")

    def test_second_process_is_rejected_while_owner_is_running(self):
        owner = self.start_owner()
        result = self.run_contender()
        self.assertEqual(result.returncode, 23, result.stderr)
        self.assertEqual(result.stdout, "already running\n")
        self.assertIsNone(owner.poll())

    def test_lock_can_be_acquired_after_normal_exit(self):
        owner = self.start_owner()
        _, errors = owner.communicate(timeout=10)
        self.assertEqual(owner.returncode, 0, errors)
        self.assertTrue(self.lock_path.exists())
        self.assertEqual(self.lock_path.read_text().strip(), str(owner.pid))
        self.assert_can_acquire()

    def test_lock_can_be_acquired_after_process_is_killed(self):
        owner = self.start_owner()
        owner.kill()
        owner.communicate(timeout=10)
        self.assertNotEqual(owner.returncode, 0)
        self.assertTrue(self.lock_path.exists())
        self.assert_can_acquire()


if __name__ == "__main__":
    unittest.main()
