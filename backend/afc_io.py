"""Bounded AFC reads without repeated copying of growing socket buffers."""
from __future__ import annotations

import struct
from collections import deque

MAX_READ = 32 * 1024 * 1024


def _recv_exact(sock, count: int) -> bytearray:
    result = bytearray(count)
    view = memoryview(result)
    received = 0
    while received < count:
        size = sock.recv_into(view[received:])
        if not size:
            raise ConnectionAbortedError("The AFC socket closed during a read")
        received += size
    return result


def read(afc, handle: int, count: int):
    """One read, at most 32 MiB. Non-native connectors use their public API.

    pymobiledevice3 7.6 recvall repeatedly concatenates immutable bytes. A large
    response arriving in small USB packets can copy the same data thousands of
    times. Receive directly into one preallocated buffer and validate framing.
    """
    if not 0 < count <= MAX_READ:
        raise ValueError("AFC read size must be between 1 byte and 32 MiB")
    if not hasattr(afc, "_dispatch_packet") or not hasattr(afc, "service"):
        return afc.fread(handle, count)
    from pymobiledevice3.services.afc import AfcOpcode
    afc._dispatch_packet(AfcOpcode.READ, struct.pack("<QQ", handle, count))
    return _receive(afc, count)


def _receive(afc, count):
    from pymobiledevice3.services.afc import AfcOpcode, AfcError
    from pymobiledevice3.exceptions import AfcException
    raw = _recv_exact(afc.service.socket, 40)
    magic, entire, current, packet, operation = struct.unpack("<8sQQQQ", raw)
    if magic != b"CFA6LPAA" or entire < 40 or current < 40 or current > entire or entire - 40 > max(count, 8):
        raise ConnectionAbortedError("Invalid AFC response framing")
    data = _recv_exact(afc.service.socket, entire - 40)
    if operation == int(AfcOpcode.STATUS):
        if len(data) != 8:
            raise ConnectionAbortedError("Invalid AFC status response")
        status = struct.unpack("<Q", data)[0]
        if status != int(AfcError.SUCCESS):
            raise AfcException("fread error", status)
        return bytearray()
    if operation != int(AfcOpcode.DATA):
        raise ConnectionAbortedError("Unexpected AFC response operation")
    return data


def iter_read(afc, handle: int, total: int, chunk_size: int = 4 * 1024 * 1024,
              depth: int = 2, check=None):
    """Read in order with two outstanding requests; memory holds one response.

    AFC on this iPhone caps each reply at 4 MiB. Requests are limited to that
    size so queued ranges cannot skip bytes. Callers must close this generator
    on early exit; closing its socket discards pending replies before FILE_CLOSE
    or any later operation can consume an outstanding READ reply.
    """
    if total < 0 or not 0 < chunk_size <= 4 * 1024 * 1024 or not 1 <= depth <= 4:
        raise ValueError("Invalid bounded AFC read parameters")
    check = check or (lambda: None)
    if not hasattr(afc, "_dispatch_packet") or not hasattr(afc, "service"):
        done = 0
        while done < total:
            check()
            requested = min(chunk_size, total-done)
            data = afc.fread(handle, requested)
            if not data or len(data) > requested:
                raise EOFError("The Device returned incomplete file data")
            done += len(data)
            yield data
        return
    from pymobiledevice3.services.afc import AfcOpcode
    pending = deque()
    requested_total = 0
    finished = False
    try:
        while requested_total < total or pending:
            check()
            while len(pending) < depth and requested_total < total:
                requested = min(chunk_size, total-requested_total)
                afc._dispatch_packet(AfcOpcode.READ, struct.pack("<QQ", handle, requested))
                pending.append(requested)
                requested_total += requested
            requested = pending[0]
            data = _receive(afc, requested)
            pending.popleft()
            if len(data) != requested:
                raise EOFError("The Device returned incomplete file data")
            yield data
        finished = True
    finally:
        if not finished:
            # This service belongs exclusively to the enclosing operation.
            afc.close()
