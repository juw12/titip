"""Starting up: Titip keeps normal priority, so a busy PC still answers the phone."""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent

LOWER_THEN_RECOVER = """
import ctypes
from titip.__main__ import NORMAL_PRIORITY, stay_responsive

kernel = ctypes.windll.kernel32
kernel.GetCurrentProcess.restype = ctypes.c_void_p
kernel.GetPriorityClass.argtypes = [ctypes.c_void_p]
kernel.GetPriorityClass.restype = ctypes.c_ulong
kernel.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
kernel.GetCurrentThread.restype = ctypes.c_void_p
kernel.SetThreadPriority.argtypes = [ctypes.c_void_p, ctypes.c_int]
process = kernel.GetCurrentProcess()
# This one thread stays in front: on a full PC a lowered program waits minutes for the processor,
# and the test with it. What is under test, the priority class of the program, is not touched by this.
kernel.SetThreadPriority(kernel.GetCurrentThread(), 15)
kernel.SetPriorityClass(process, 0x4000)  # below normal, as a background-program tamer would set it
lowered = kernel.GetPriorityClass(process) == 0x4000
changed = stay_responsive()
normal = kernel.GetPriorityClass(process) == NORMAL_PRIORITY
print(lowered, changed, normal, stay_responsive())
"""

BELOW_NORMAL, NORMAL = 0x4000, 0x20


class Priority(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "win32", "process priority classes are a Windows matter")
    def test_a_lowered_priority_is_put_back_to_normal_and_no_higher(self):
        done = subprocess.run(
            [sys.executable, "-c", LOWER_THEN_RECOVER],
            cwd=str(ROOT), capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(done.stdout.split(), ["True", "True", "True", "False"], done.stderr)

    def test_it_is_harmless_where_there_is_nothing_to_do(self):
        from titip.__main__ import stay_responsive

        self.assertIn(stay_responsive(), (True, False))

    @unittest.skipUnless(sys.platform == "win32", "process priority classes are a Windows matter")
    def test_what_a_lowered_titip_starts_runs_at_normal_priority(self):
        # A new version started by a lowered Titip inherited the low priority and, on a full PC, did not
        # answer for minutes: the updater would have taken a good version for one that does not start.
        import ctypes

        from titip.update import spawn_detached

        kernel = ctypes.windll.kernel32
        kernel.GetCurrentProcess.restype = ctypes.c_void_p
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        kernel.GetPriorityClass.argtypes = [ctypes.c_void_p]
        kernel.GetPriorityClass.restype = ctypes.c_ulong
        kernel.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        kernel.GetCurrentThread.restype = ctypes.c_void_p
        kernel.SetThreadPriority.argtypes = [ctypes.c_void_p, ctypes.c_int]
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]

        def priority_of(child) -> int:  # asked from here: the child itself need not get the processor
            handle = kernel.OpenProcess(0x1000, False, child.pid)  # may only ask about it
            try:
                return kernel.GetPriorityClass(handle)
            finally:
                kernel.CloseHandle(handle)

        sleeper = [sys.executable, "-c", "import time; time.sleep(120)"]
        me = kernel.GetCurrentProcess()
        before = kernel.GetPriorityClass(me)
        children = []
        # This thread stays in front while the program is lowered (see LOWER_THEN_RECOVER): a program
        # hands on its priority class, not the priority of the thread that does the starting.
        kernel.SetThreadPriority(kernel.GetCurrentThread(), 15)
        kernel.SetPriorityClass(me, BELOW_NORMAL)  # as a background-program tamer would set it
        try:
            children.append(subprocess.Popen(sleeper))  # the control: started the plain way
            children.append(spawn_detached(sleeper, ROOT))
        finally:
            kernel.SetPriorityClass(me, before)
            kernel.SetThreadPriority(kernel.GetCurrentThread(), 0)
        try:
            self.assertEqual([priority_of(child) for child in children], [BELOW_NORMAL, NORMAL])
        finally:
            for child in children:
                child.kill()
                child.wait(60)

    @unittest.skipUnless(sys.platform == "win32", "process priority classes are a Windows matter")
    def test_git_and_the_other_helpers_are_started_at_normal_priority_too(self):
        from titip import update

        with mock.patch.object(update.subprocess, "run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="ok\n")
            self.assertEqual(update.run_command(["git", "status"], ROOT), (0, "ok"))
        self.assertTrue(run.call_args.kwargs["creationflags"] & subprocess.NORMAL_PRIORITY_CLASS)

    def test_the_priority_is_put_right_before_anything_slow(self):
        from titip import __main__ as start

        order = []
        with tempfile.TemporaryDirectory() as home, \
                mock.patch.object(start, "stay_responsive", side_effect=lambda: order.append("priority") or False), \
                mock.patch.object(start, "already_running", side_effect=lambda port: order.append("look") or True), \
                mock.patch.object(start, "say"):
            self.assertEqual(start.main(["--quiet", "--home", home]), 0)
        self.assertEqual(order, ["priority", "look"])


if __name__ == "__main__":
    unittest.main()
