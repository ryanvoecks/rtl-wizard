"""
Body-inspecting Docker API policy proxy.

Sits between the devcontainer and wollomatic/socket-proxy. Validates the
JSON body of mutating endpoints (containers/create, containers/{id}/update,
containers/{id}/exec, volumes/create) against a structural policy that
rejects host-escape primitives. All other requests are tunneled through
unchanged so hijacked endpoints (exec/start, attach, build) keep working.

Fail-closed: anything that doesn't parse, anything that hits a rule, or
any unexpected error returns 4xx/5xx to the client and never reaches
the upstream socket-proxy.
"""

import asyncio
import json
import os
import re
import sys
from urllib.parse import parse_qs, urlparse

UPSTREAM_HOST = os.environ.get("UPSTREAM_HOST", "socket-proxy")
UPSTREAM_PORT = int(os.environ.get("UPSTREAM_PORT", "2375"))
LISTEN_HOST = os.environ.get("LISTEN_HOST", "0.0.0.0")
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "2375"))
MAX_BODY = int(os.environ.get("MAX_BODY_BYTES", str(4 * 1024 * 1024)))

# Bind-mount source paths the policy will accept. Sibling containers
# spawned through this proxy can only mount these prefixes from the
# host. Every entry is a potential host-escape vector - keep minimal.
ALLOWED_BIND_PREFIXES = tuple(
    p.rstrip("/") or "/"
    for p in os.environ.get("ALLOWED_BIND_PREFIXES", "/tmp,/var/tmp").split(",")
    if p
)

CREATE_RE = re.compile(r"^/v\d+\.\d+/containers/create(?:\?|$)")
UPDATE_RE = re.compile(r"^/v\d+\.\d+/containers/[^/]+/update$")
EXEC_RE = re.compile(r"^/v\d+\.\d+/containers/[^/]+/exec$")
VOL_CREATE_RE = re.compile(r"^/v\d+\.\d+/volumes/create$")
BUILD_RE = re.compile(r"^/v\d+\.\d+/build(?:\?|$)")


class PolicyViolation(Exception):
    pass


def _path_allowed(src: str) -> bool:
    src = (src or "").rstrip("/") or "/"
    return any(src == p or src.startswith(p + "/") for p in ALLOWED_BIND_PREFIXES)


def check_create(body: bytes) -> None:
    spec = json.loads(body)
    hc = spec.get("HostConfig") or {}

    if hc.get("Privileged") is True:
        raise PolicyViolation("HostConfig.Privileged=true is not permitted")

    # Host namespace sharing -- any one of these collapses container isolation.
    for ns in (
        "PidMode",
        "NetworkMode",
        "IpcMode",
        "UTSMode",
        "UsernsMode",
        "CgroupnsMode",
    ):
        v = hc.get(ns)
        if isinstance(v, str) and (v == "host" or v.startswith("host:")):
            raise PolicyViolation(f"HostConfig.{ns}={v!r} is not permitted")

    if hc.get("CapAdd"):
        raise PolicyViolation(f"HostConfig.CapAdd={hc['CapAdd']!r} is not permitted")

    if hc.get("Devices"):
        raise PolicyViolation("HostConfig.Devices passthrough is not permitted")

    if hc.get("DeviceCgroupRules"):
        raise PolicyViolation("HostConfig.DeviceCgroupRules is not permitted")

    if hc.get("CgroupParent"):
        raise PolicyViolation("HostConfig.CgroupParent override is not permitted")

    runtime = hc.get("Runtime")
    if runtime not in (None, "", "runc"):
        raise PolicyViolation(f"HostConfig.Runtime={runtime!r} is not permitted")

    for so in hc.get("SecurityOpt") or []:
        s = str(so).lower().replace(" ", "")
        if (
            s.startswith("seccomp=unconfined")
            or s.startswith("apparmor=unconfined")
            or s in ("no-new-privileges:false", "no-new-privileges=false")
            or s.startswith("systempaths=unconfined")
        ):
            raise PolicyViolation(f"HostConfig.SecurityOpt={so!r} is not permitted")

    # User-namespace-safe sysctls are net.ipv4.* etc. kernel/vm/fs
    # tuning lets a container influence host kernel state.
    for k in hc.get("Sysctls") or {}:
        if k.startswith(("kernel.", "vm.", "fs.", "abi.")):
            raise PolicyViolation(f"Sysctl {k} is not permitted")

    for bind in hc.get("Binds") or []:
        src = str(bind).split(":", 1)[0]
        if not _path_allowed(src):
            raise PolicyViolation(f"bind source {src!r} not in allowlist")

    for mount in hc.get("Mounts") or []:
        mtype = mount.get("Type")
        if mtype == "bind":
            if not _path_allowed(mount.get("Source", "")):
                raise PolicyViolation(
                    f"bind mount Source={mount.get('Source')!r} not in allowlist"
                )
        elif mtype not in (None, "volume", "tmpfs"):
            raise PolicyViolation(f"mount Type={mtype!r} is not permitted")
        bo = mount.get("BindOptions") or {}
        if bo.get("Propagation") in ("shared", "rshared"):
            raise PolicyViolation("shared mount propagation is not permitted")


def check_update(body: bytes) -> None:
    spec = json.loads(body)
    if spec.get("CpuRealtimeRuntime") or spec.get("CpuRealtimePeriod"):
        raise PolicyViolation("realtime CPU scheduling is not permitted")


def check_exec(body: bytes) -> None:
    spec = json.loads(body)
    if spec.get("Privileged") is True:
        # Privileged exec re-grants all caps to the exec'd process even
        # if the container itself was created without --privileged.
        raise PolicyViolation("exec Privileged=true is not permitted")


def check_volume_create(body: bytes) -> None:
    spec = json.loads(body)
    driver = spec.get("Driver") or "local"
    if driver != "local":
        raise PolicyViolation(f"volume Driver={driver!r} is not permitted")
    # The local driver with o=bind,device=/host/path is a bind mount in
    # disguise -- gate the device path through the same allowlist.
    do = spec.get("DriverOpts") or {}
    device = do.get("device")
    if device and not _path_allowed(str(device)):
        raise PolicyViolation(f"volume device path {device!r} not in allowlist")


def check_build(path: str) -> None:
    """Inspect /build query for host-escape primitives.

    /build configures the build via URL query params; the body is the
    context tar (potentially hundreds of MB) so we don't buffer it. We
    apply the rules from /containers/create that map across.

    Per-RUN Dockerfile directives like `RUN --network=host` and
    `RUN --security=insecure` are gated by daemon-level entitlements
    (network.host, security.insecure) that Docker Desktop does not grant
    by default, so blocking the build-wide knobs here is sufficient.
    /session is intentionally not checked -- it's a hijacked gRPC stream
    used for context filesync, secrets, and SSH forwarding, none of
    which expand the daemon's host-escape surface.
    """
    qs = parse_qs(urlparse(path).query)

    nm = (qs.get("networkmode") or [""])[0]
    if nm == "host" or nm.startswith("host:"):
        raise PolicyViolation(f"build networkmode={nm!r} is not permitted")

    cp = (qs.get("cgroupparent") or [""])[0]
    if cp:
        raise PolicyViolation(f"build cgroupparent={cp!r} override is not permitted")

    # extrahosts arrives as repeated query params, each "name:ip". The
    # special token `host-gateway` resolves to the host's IP, exposing
    # the host to build steps.
    for h in qs.get("extrahosts", []):
        if "host-gateway" in h.lower():
            raise PolicyViolation(f"build extrahost {h!r} is not permitted")


CHECKS = [
    (CREATE_RE, check_create),
    (UPDATE_RE, check_update),
    (EXEC_RE, check_exec),
    (VOL_CREATE_RE, check_volume_create),
]

# URL-only checks: validate the query string, then tunnel the request
# straight through so the body streams unbuffered. Use this for
# endpoints whose policy lives in the URL and whose body is too large
# to buffer (/build's context tar).
URL_CHECKS = [
    (BUILD_RE, check_build),
]


# ---- HTTP plumbing ----


async def _read_headers(reader: asyncio.StreamReader):
    request_line = await reader.readline()
    if not request_line:
        return b"", b"", {}
    raw = b""
    while True:
        line = await reader.readline()
        raw += line
        if line in (b"\r\n", b"\n", b""):
            break
    headers = {}
    for hl in raw.split(b"\r\n"):
        if b":" not in hl:
            continue
        k, _, v = hl.partition(b":")
        headers[k.decode("iso-8859-1").strip().lower()] = v.decode("iso-8859-1").strip()
    return request_line, raw, headers


async def _read_body(reader: asyncio.StreamReader, headers: dict) -> bytes:
    te = headers.get("transfer-encoding", "").lower()
    if "chunked" in te:
        out = bytearray()
        while True:
            size_line = await reader.readline()
            try:
                size = int(size_line.split(b";")[0].strip(), 16)
            except ValueError as e:
                raise PolicyViolation(f"bad chunk size: {e}")
            if size == 0:
                while True:
                    trailer = await reader.readline()
                    if trailer in (b"\r\n", b"\n", b""):
                        break
                break
            if len(out) + size > MAX_BODY:
                raise PolicyViolation("request body too large")
            out += await reader.readexactly(size)
            await reader.readexactly(2)
        return bytes(out)
    cl = int(headers.get("content-length", "0") or 0)
    if cl > MAX_BODY:
        raise PolicyViolation("request body too large")
    return await reader.readexactly(cl) if cl else b""


def _strip_header(raw: bytes, name: str) -> bytes:
    pat = re.compile(rb"(?im)^" + re.escape(name.encode()) + rb":[^\r\n]*\r?\n")
    return pat.sub(b"", raw)


def _set_header(raw: bytes, name: str, value: str) -> bytes:
    raw = _strip_header(raw, name)
    insertion = (name + ": " + value + "\r\n").encode()
    if raw.endswith(b"\r\n\r\n"):
        return raw[:-2] + insertion + b"\r\n"
    if raw.endswith(b"\r\n"):
        return raw + insertion + b"\r\n"
    return raw + b"\r\n" + insertion + b"\r\n"


async def _write_status(
    writer: asyncio.StreamWriter, code: int, reason: str, msg: str
) -> None:
    body = json.dumps({"message": msg}).encode()
    writer.write(
        f"HTTP/1.1 {code} {reason}\r\n".encode()
        + b"Content-Type: application/json\r\n"
        + f"Content-Length: {len(body)}\r\n".encode()
        + b"Connection: close\r\n\r\n"
        + body
    )
    await writer.drain()


async def _splice(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
    try:
        while True:
            data = await src.read(65536)
            if not data:
                break
            dst.write(data)
            await dst.drain()
    except (
        ConnectionResetError,
        BrokenPipeError,
        OSError,
        asyncio.IncompleteReadError,
    ):
        # Routine for proxied connections -- the peer or upstream just
        # went away. Caller decides whether that's an error.
        pass
    finally:
        try:
            if dst.can_write_eof():
                dst.write_eof()
        except Exception:
            pass


async def _tunnel(
    creader, cwriter, request_line: bytes, raw_headers: bytes, headers: dict
) -> None:
    # CRITICAL: force Connection: close on every non-hijacked tunneled
    # request. Without this, the docker SDK reuses one TCP connection
    # for many API calls (HTTP/1.1 keep-alive). After the first call
    # we'd be in byte-pipe mode for the connection's whole lifetime,
    # so every subsequent request -- including containers/create with
    # Privileged: true -- would byte-relay through us without ever
    # being parsed or inspected. This is a fail-open hole.
    #
    # Forcing Connection: close on the request makes upstream close
    # after responding (RFC 7230 sec.6.6: receiver echoes Connection:
    # close in the response). The SDK sees that, closes its end, and
    # opens a fresh TCP connection for the next call -- which lands
    # back in handle() and goes through inspection if it matches.
    #
    # Hijacked endpoints (Connection: Upgrade -- attach, exec/start)
    # MUST NOT have this rewrite, or the upgrade dance breaks. Those
    # are dedicated bidirectional streams anyway, so the SDK doesn't
    # reuse them for further API calls.
    is_upgrade = "upgrade" in headers.get("connection", "").lower()
    if not is_upgrade:
        raw_headers = _strip_header(raw_headers, "Connection")
        raw_headers = _strip_header(raw_headers, "Keep-Alive")
        raw_headers = _set_header(raw_headers, "Connection", "close")

    ureader, uwriter = await asyncio.open_connection(UPSTREAM_HOST, UPSTREAM_PORT)
    try:
        uwriter.write(request_line + raw_headers)
        await uwriter.drain()
        # return_exceptions=True so a torn-down splice (RST, half-close
        # after a hijacked stream ends, etc.) doesn't bubble up as a
        # 500 to the client. We just want both directions to drain
        # whatever they can and then end.
        await asyncio.gather(
            _splice(creader, uwriter),
            _splice(ureader, cwriter),
            return_exceptions=True,
        )
    finally:
        try:
            uwriter.close()
        except Exception:
            pass


async def _forward_inspected(
    cwriter, request_line: bytes, raw_headers: bytes, body: bytes
) -> None:
    # We've already consumed the body, so re-frame the request as a
    # plain content-length POST and force-close upstream after to keep
    # connection bookkeeping simple.
    raw_headers = _set_header(raw_headers, "Content-Length", str(len(body)))
    raw_headers = _strip_header(raw_headers, "Transfer-Encoding")
    raw_headers = _set_header(raw_headers, "Connection", "close")

    ureader, uwriter = await asyncio.open_connection(UPSTREAM_HOST, UPSTREAM_PORT)
    try:
        uwriter.write(request_line + raw_headers + body)
        await uwriter.drain()
        await _splice(ureader, cwriter)
    finally:
        try:
            uwriter.close()
        except Exception:
            pass


async def handle(creader: asyncio.StreamReader, cwriter: asyncio.StreamWriter) -> None:
    peer = cwriter.get_extra_info("peername")
    method = "?"
    path = "?"
    try:
        request_line, raw_headers, headers = await _read_headers(creader)
        if not request_line:
            return
        try:
            method, path, _ = (
                request_line.decode("iso-8859-1").rstrip("\r\n").split(" ", 2)
            )
        except ValueError:
            await _write_status(cwriter, 400, "Bad Request", "malformed request line")
            return

        body_check = None
        url_check = None
        if method == "POST":
            for pattern, fn in CHECKS:
                if pattern.match(path):
                    body_check = fn
                    break
            if body_check is None:
                for pattern, fn in URL_CHECKS:
                    if pattern.match(path):
                        url_check = fn
                        break

        if body_check is not None:
            try:
                body = await _read_body(creader, headers)
                body_check(body)
            except PolicyViolation as e:
                print(
                    f"[deny]   {peer} {method} {path}: {e}", file=sys.stderr, flush=True
                )
                await _write_status(cwriter, 403, "Forbidden", str(e))
                return
            except (json.JSONDecodeError, asyncio.IncompleteReadError, ValueError) as e:
                print(
                    f"[err]    {peer} {method} {path}: {e}", file=sys.stderr, flush=True
                )
                await _write_status(
                    cwriter, 400, "Bad Request", f"unparseable body: {e}"
                )
                return
            print(f"[allow]  {peer} {method} {path}", file=sys.stderr, flush=True)
            await _forward_inspected(cwriter, request_line, raw_headers, body)
        elif url_check is not None:
            try:
                url_check(path)
            except PolicyViolation as e:
                print(
                    f"[deny]   {peer} {method} {path}: {e}", file=sys.stderr, flush=True
                )
                await _write_status(cwriter, 403, "Forbidden", str(e))
                return
            print(f"[allow]  {peer} {method} {path}", file=sys.stderr, flush=True)
            await _tunnel(creader, cwriter, request_line, raw_headers, headers)
        else:
            print(f"[tunnel] {peer} {method} {path}", file=sys.stderr, flush=True)
            await _tunnel(creader, cwriter, request_line, raw_headers, headers)
    except (ConnectionResetError, BrokenPipeError):
        pass
    except Exception as e:
        print(f"[fatal]  {peer} {method} {path}: {e!r}", file=sys.stderr, flush=True)
        try:
            await _write_status(cwriter, 500, "Internal Server Error", "proxy error")
        except Exception:
            pass
    finally:
        try:
            cwriter.close()
        except Exception:
            pass


async def main() -> None:
    server = await asyncio.start_server(handle, LISTEN_HOST, LISTEN_PORT)
    print(
        f"docker-policy-proxy listening on {LISTEN_HOST}:{LISTEN_PORT} "
        f"-> {UPSTREAM_HOST}:{UPSTREAM_PORT}; "
        f"bind allowlist={ALLOWED_BIND_PREFIXES}",
        file=sys.stderr,
        flush=True,
    )
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
