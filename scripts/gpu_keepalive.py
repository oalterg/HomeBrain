#!/usr/bin/env python3
"""Give the Arc a little work so it never sits idle for minutes.

On the Arc Pro B60 the GuC firmware sometimes crashes on the first job after
the GPU has been idle for a long time. On .69, from 2026-11-13 to 11-20, none
of 918 llama-server jobs that came less than five idle minutes after the last
one crashed it, and 6 of 55 that came after more than 45 minutes did. Half of
those crashes wedge the card until it is rebound.

This fills and copies a small device buffer on the compute engine, the one
llama-server uses, through the same Level Zero driver. It never talks to
llama-server, so the agent's prompt cache stays where it is.

This puts a second context on the compute engine, so the GuC preempts
llama-server's to run it. llama-server must not run with NEO's
OverrideCsrAllocationSize: with a 1 MiB context save area, that preemption
page-faulted llama-server's context and aborted it on .69, on every overlap,
during a model load and during a reply alike.

Run it inside llama-server's SYCL environment (sycl-env.sh), so the loader
picks the same driver. --verbose prints how much compute-engine time the
work took, which proves the GPU ran it.
"""

import ctypes as C
import glob
import sys

STYPE_DEVICE_PROPERTIES = 0x3
STYPE_COMMAND_QUEUE_GROUP_PROPERTIES = 0x6
STYPE_CONTEXT_DESC = 0xD
STYPE_COMMAND_QUEUE_DESC = 0xE
STYPE_DEVICE_MEM_ALLOC_DESC = 0x15
INIT_FLAG_GPU_ONLY = 1
DEVICE_TYPE_GPU = 1
GROUP_FLAG_COMPUTE = 1
QUEUE_MODE_SYNCHRONOUS = 1
INTEL = 0x8086
SIZE = 64 << 20
ROUNDS = 8
WAIT_FOREVER = 2**64 - 1


class DeviceProps(C.Structure):
    _fields_ = [
        ("stype", C.c_uint32), ("pNext", C.c_void_p), ("type", C.c_uint32),
        ("vendorId", C.c_uint32), ("deviceId", C.c_uint32), ("flags", C.c_uint32),
        ("subdeviceId", C.c_uint32), ("coreClockRate", C.c_uint32),
        ("maxMemAllocSize", C.c_uint64), ("maxHardwareContexts", C.c_uint32),
        ("maxCommandQueuePriority", C.c_uint32), ("numThreadsPerEU", C.c_uint32),
        ("physicalEUSimdWidth", C.c_uint32), ("numEUsPerSubslice", C.c_uint32),
        ("numSubslicesPerSlice", C.c_uint32), ("numSlices", C.c_uint32),
        ("timerResolution", C.c_uint64), ("timestampValidBits", C.c_uint32),
        ("kernelTimestampValidBits", C.c_uint32), ("uuid", C.c_uint8 * 16),
        ("name", C.c_char * 256),
    ]


class GroupProps(C.Structure):
    _fields_ = [
        ("stype", C.c_uint32), ("pNext", C.c_void_p), ("flags", C.c_uint32),
        ("maxMemoryFillPatternSize", C.c_size_t), ("numQueues", C.c_uint32),
    ]


class ContextDesc(C.Structure):
    _fields_ = [("stype", C.c_uint32), ("pNext", C.c_void_p), ("flags", C.c_uint32)]


class QueueDesc(C.Structure):
    _fields_ = [
        ("stype", C.c_uint32), ("pNext", C.c_void_p), ("ordinal", C.c_uint32),
        ("index", C.c_uint32), ("flags", C.c_uint32), ("mode", C.c_uint32),
        ("priority", C.c_uint32),
    ]


class MemDesc(C.Structure):
    _fields_ = [
        ("stype", C.c_uint32), ("pNext", C.c_void_p), ("flags", C.c_uint32),
        ("ordinal", C.c_uint32),
    ]


class ZeError(Exception):
    pass


def bind(ze):
    def call(name):
        fn = getattr(ze, name)
        fn.restype = C.c_uint32

        def checked(*args):
            rc = fn(*args)
            if rc != 0:
                raise ZeError(f"{name} returned {rc:#x}")
        return checked
    return call


def compute_cycles():
    """Sum of drm-cycles-ccs over this process's open DRM files."""
    total = 0
    for path in glob.glob("/proc/self/fdinfo/*"):
        try:
            with open(path) as f:
                for line in f:
                    if line.startswith("drm-cycles-ccs:"):
                        total += int(line.split()[1])
        except OSError:
            continue
    return total


def find_gpu(call):
    count = C.c_uint32(0)
    call("zeDriverGet")(C.byref(count), None)
    drivers = (C.c_void_p * count.value)()
    call("zeDriverGet")(C.byref(count), drivers)
    for driver in drivers:
        n = C.c_uint32(0)
        call("zeDeviceGet")(C.c_void_p(driver), C.byref(n), None)
        if not n.value:
            continue
        devices = (C.c_void_p * n.value)()
        call("zeDeviceGet")(C.c_void_p(driver), C.byref(n), devices)
        for device in devices:
            props = DeviceProps(stype=STYPE_DEVICE_PROPERTIES)
            call("zeDeviceGetProperties")(C.c_void_p(device), C.byref(props))
            if props.type == DEVICE_TYPE_GPU and props.vendorId == INTEL:
                return C.c_void_p(driver), C.c_void_p(device), props.name.decode(errors="replace")
    raise ZeError("no Intel GPU")


def compute_ordinal(call, device):
    n = C.c_uint32(0)
    call("zeDeviceGetCommandQueueGroupProperties")(device, C.byref(n), None)
    groups = (GroupProps * n.value)()
    for g in groups:
        g.stype = STYPE_COMMAND_QUEUE_GROUP_PROPERTIES
    call("zeDeviceGetCommandQueueGroupProperties")(device, C.byref(n), groups)
    for i, g in enumerate(groups):
        if g.flags & GROUP_FLAG_COMPUTE:
            return i
    raise ZeError("no compute queue group")


def main():
    verbose = "--verbose" in sys.argv[1:]
    ze = C.CDLL("libze_loader.so.1")
    call = bind(ze)
    call("zeInit")(INIT_FLAG_GPU_ONLY)
    driver, device, name = find_gpu(call)
    ordinal = compute_ordinal(call, device)

    ctx = C.c_void_p()
    call("zeContextCreate")(driver, C.byref(ContextDesc(stype=STYPE_CONTEXT_DESC)), C.byref(ctx))
    cmdlist = C.c_void_p()
    bufs = []
    try:
        qdesc = QueueDesc(stype=STYPE_COMMAND_QUEUE_DESC, ordinal=ordinal,
                          mode=QUEUE_MODE_SYNCHRONOUS)
        call("zeCommandListCreateImmediate")(ctx, device, C.byref(qdesc), C.byref(cmdlist))
        for _ in range(2):
            ptr = C.c_void_p()
            call("zeMemAllocDevice")(ctx, C.byref(MemDesc(stype=STYPE_DEVICE_MEM_ALLOC_DESC)),
                                     C.c_size_t(SIZE), C.c_size_t(64), device, C.byref(ptr))
            bufs.append(ptr)
        pattern = C.c_uint32(0x48424B41)
        for _ in range(ROUNDS):
            call("zeCommandListAppendMemoryFill")(cmdlist, bufs[0], C.byref(pattern),
                                                  C.c_size_t(4), C.c_size_t(SIZE), None, 0, None)
            call("zeCommandListAppendMemoryCopy")(cmdlist, bufs[1], bufs[0],
                                                  C.c_size_t(SIZE), None, 0, None)
        call("zeCommandListHostSynchronize")(cmdlist, C.c_uint64(WAIT_FOREVER))
        if verbose:
            print(f"{name}: compute queue group {ordinal}, "
                  f"{compute_cycles()} ccs cycles for {ROUNDS} x {SIZE >> 20} MiB fill+copy")
    finally:
        for ptr in bufs:
            ze.zeMemFree(ctx, ptr)
        if cmdlist:
            ze.zeCommandListDestroy(cmdlist)
        ze.zeContextDestroy(ctx)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ZeError) as e:
        print(f"gpu keepalive: {e}", file=sys.stderr)
        sys.exit(1)
