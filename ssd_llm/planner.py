from __future__ import annotations

from dataclasses import dataclass
import os

GiB = 1024**3


@dataclass(frozen=True)
class Machine:
    logical_cpus: int
    available_ram_bytes: int


@dataclass(frozen=True)
class RunPlan:
    threads: int
    context: int
    batch: int
    gpu_layers: int = 0
    mmap: bool = True
    reserved_ram_bytes: int = 0


def discover_machine() -> Machine:
    """Return conservative machine facts without requiring third-party packages."""
    cpus = os.cpu_count() or 1
    # Windows has no POSIX sysconf; GlobalMemoryStatusEx is reliable there.
    if os.name == "nt":
        import ctypes

        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        status = MEMORYSTATUSEX()
        status.dwLength = ctypes.sizeof(status)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            raise OSError("GlobalMemoryStatusEx failed")
        available = int(status.ullAvailPhys)
    else:
        available = os.sysconf("SC_AVPHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
    return Machine(logical_cpus=cpus, available_ram_bytes=available)


def plan_run(machine: Machine, *, threads: int | None = None,
             context: int | None = None, batch: int | None = None) -> RunPlan:
    """Choose a no-swap CPU plan. Explicit CLI values always take precedence."""
    if machine.logical_cpus < 1 or machine.available_ram_bytes <= 0:
        raise ValueError("machine values must be positive")

    reserve = max(2 * GiB, machine.available_ram_bytes // 5)
    usable = max(0, machine.available_ram_bytes - reserve)
    chosen_threads = threads if threads is not None else max(1, machine.logical_cpus - 1)
    if chosen_threads < 1:
        raise ValueError("threads must be at least 1")

    # KV cache grows with context; these tiers intentionally leave headroom for the
    # OS page cache that makes SSD-backed mmap viable.
    auto_context = 512 if usable < 2 * GiB else 2048 if usable < 6 * GiB else 4096
    chosen_context = context if context is not None else auto_context
    if chosen_context < 128:
        raise ValueError("context must be at least 128")
    # Modern llama.cpp defaults to batch 2048 / ubatch 512. Larger batches
    # dramatically speed up prompt prefill on CPU, so use the server default
    # tiers instead of the old conservative 256.
    auto_batch = 512 if usable < 2 * GiB else 1024 if usable < 6 * GiB else 2048
    chosen_batch = batch if batch is not None else auto_batch
    if chosen_batch < 1:
        raise ValueError("batch must be at least 1")
    return RunPlan(chosen_threads, chosen_context, chosen_batch, reserved_ram_bytes=reserve)
