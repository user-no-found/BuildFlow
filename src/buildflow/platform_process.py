"""Native process-tree ownership for POSIX sessions and Windows jobs."""
import os
import signal
import subprocess
import time


class ProcessTree:
    """Own a child session or a Windows kill-on-close job."""

    def __init__(self):
        self.job = None
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
            kernel.CreateJobObjectW.restype = wintypes.HANDLE
            kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
            kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
            kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            class BasicLimit(ctypes.Structure):
                _fields_ = [("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
                            ("flags", wintypes.DWORD), ("min_working", ctypes.c_size_t),
                            ("max_working", ctypes.c_size_t), ("active", wintypes.DWORD),
                            ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD),
                            ("scheduling", wintypes.DWORD)]
            class IO(ctypes.Structure):
                _fields_ = [(name, ctypes.c_uint64) for name in
                            ("read_ops", "write_ops", "other_ops", "read_bytes", "write_bytes", "other_bytes")]
            class ExtendedLimit(ctypes.Structure):
                _fields_ = [("basic", BasicLimit), ("io", IO), ("process_memory", ctypes.c_size_t),
                            ("job_memory", ctypes.c_size_t), ("peak_process", ctypes.c_size_t),
                            ("peak_job", ctypes.c_size_t)]
            self.kernel = kernel
            self.job = kernel.CreateJobObjectW(None, None)
            if not self.job:
                raise ctypes.WinError(ctypes.get_last_error())
            limit = ExtendedLimit()
            limit.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not kernel.SetInformationJobObject(self.job, 9, ctypes.byref(limit), ctypes.sizeof(limit)):
                self.close()
                raise ctypes.WinError(ctypes.get_last_error())

    def options(self) -> dict:
        """Return subprocess creation options for native ownership."""
        if os.name == "nt":
            # The bootstrap waits before executing project code, avoiding the
            # assignment race between CreateProcess and AssignProcessToJobObject.
            return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        return {"start_new_session": True}

    def attach(self, process: subprocess.Popen) -> None:
        """Attach the Windows bootstrap before permitting its command to run."""
        if self.job:
            import ctypes
            if not self.kernel.AssignProcessToJobObject(self.job, int(process._handle)):
                process.kill()
                process.wait()
                raise ctypes.WinError(ctypes.get_last_error())

    def cancel(self, process: subprocess.Popen) -> None:
        """Terminate descendants even when the direct child already exited."""
        if os.name == "nt":
            self.kernel.TerminateJobObject(self.job, 130)
        else:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                return
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline:
                try:
                    os.killpg(process.pid, 0)
                except ProcessLookupError:
                    return
                time.sleep(0.05)
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def close(self) -> None:
        """Release the job and all remaining owned processes."""
        if self.job:
            self.kernel.CloseHandle(self.job)
            self.job = None
