"""Starting up: Titip keeps normal priority, so a busy PC still answers the phone."""
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

LOWER_THEN_RECOVER = """
import ctypes
from titip.__main__ import NORMAL_PRIORITY, stay_responsive

kernel = ctypes.windll.kernel32
kernel.GetCurrentProcess.restype = ctypes.c_void_p
kernel.GetPriorityClass.argtypes = [ctypes.c_void_p]
kernel.GetPriorityClass.restype = ctypes.c_ulong
kernel.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
process = kernel.GetCurrentProcess()
kernel.SetPriorityClass(process, 0x4000)  # below normal, as a background-program tamer would set it
lowered = kernel.GetPriorityClass(process) == 0x4000
changed = stay_responsive()
normal = kernel.GetPriorityClass(process) == NORMAL_PRIORITY
print(lowered, changed, normal, stay_responsive())
"""


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


if __name__ == "__main__":
    unittest.main()
