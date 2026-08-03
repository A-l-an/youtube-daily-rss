#!/usr/bin/python3
"""Safe route resolver and atomic runner .env manager for youtube-daily-rss.

Only two route forms are ever accepted or emitted:
  * DIRECT
  * LOOPBACK:<port>

The resolver deliberately ignores VPN product names, nodes, subscriptions and
remote proxy addresses.  A configured Clash port is eligible only while a
Clash/mihomo process owns a loopback listener.  A LibCyber HTTP port is
eligible only when its fixed active root config, loopback listener, actual
core executable, privileged-helper lineage, and config argument all agree.
Every eligible route must pass repeated HTTP CONNECT/TLS transport probes to
both GitHub control-plane endpoints.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, MutableMapping, Optional, Sequence, Tuple


DIRECT = "DIRECT"
LOOPBACK_PREFIX = "LOOPBACK:"
BEGIN_MARKER = "# BEGIN YOUTUBE-DAILY-RSS MANAGED PROXY"
END_MARKER = "# END YOUTUBE-DAILY-RSS MANAGED PROXY"
ROUTE_MARKER_PREFIX = "# route="
PROBE_URLS = (
    "https://api.github.com/zen",
    "https://broker.actions.githubusercontent.com",
)
DENIED_LOOPBACK_PORTS = {10800}
PROXY_ENV_KEYS = (
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
)
RUNNER_PROXY_KEYS = ("http_proxy", "https_proxy", "no_proxy")
LIBCYBER_CONFIG_RELATIVE = Path("Library/Application Support/pirate/config.yaml")
LIBCYBER_CORE_EXECUTABLES = (
    "/Applications/LibCyber Desktop.app/Contents/Resources/libs/core-darwin-amd64",
    "/Applications/LibCyber Desktop.app/Contents/Resources/libs/core-darwin-arm64",
)
LIBCYBER_HELPER_EXECUTABLE = (
    "/Library/PrivilegedHelperTools/com.libcyber.pirate.helper"
)
PROC_PIDPATHINFO_MAXSIZE = 4096
MAX_ACTIVE_CONFIG_BYTES = 2 * 1024 * 1024


class AdaptiveProxyError(RuntimeError):
    pass


def validate_route(value: str) -> str:
    if value == DIRECT:
        return value
    match = re.fullmatch(r"LOOPBACK:([1-9][0-9]{0,4})", value or "")
    if not match:
        raise AdaptiveProxyError("invalid route")
    port = int(match.group(1))
    if port > 65535:
        raise AdaptiveProxyError("invalid route")
    return f"{LOOPBACK_PREFIX}{port}"


def route_port(route: str) -> Optional[int]:
    route = validate_route(route)
    if route == DIRECT:
        return None
    return int(route.split(":", 1)[1])


def route_hash(route: str) -> str:
    return hashlib.sha256(validate_route(route).encode("ascii")).hexdigest()


def command_env(route: str, base: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
    """Return a command-only environment with inherited proxy variables removed."""
    route = validate_route(route)
    result = dict(os.environ if base is None else base)
    for key in PROXY_ENV_KEYS:
        result.pop(key, None)
    if route != DIRECT:
        proxy_url = f"http://127.0.0.1:{route_port(route)}"
        result.update(
            {
                "http_proxy": proxy_url,
                "https_proxy": proxy_url,
                "HTTP_PROXY": proxy_url,
                "HTTPS_PROXY": proxy_url,
                "no_proxy": "localhost,127.0.0.1,::1",
                "NO_PROXY": "localhost,127.0.0.1,::1",
            }
        )
    return result


def parse_csv_ports(value: str) -> List[int]:
    ports: List[int] = []
    for token in value.split(","):
        token = token.strip()
        if not token:
            continue
        if not token.isdigit() or not (1 <= int(token) <= 65535):
            continue
        port = int(token)
        if port not in ports:
            ports.append(port)
    return ports


def parse_scutil_proxy(text: str) -> List[int]:
    fields: Dict[str, str] = {}
    for line in text.splitlines():
        match = re.match(r"\s*([A-Za-z]+)\s*:\s*(.*?)\s*$", line)
        if match:
            fields[match.group(1)] = match.group(2)
    ports: List[int] = []
    for prefix in ("HTTP", "HTTPS"):
        if fields.get(prefix + "Enable") != "1":
            continue
        host = fields.get(prefix + "Proxy", "").strip("[]").lower()
        if host not in ("127.0.0.1", "localhost", "::1"):
            continue
        value = fields.get(prefix + "Port", "")
        if value.isdigit() and 1 <= int(value) <= 65535 and int(value) not in ports:
            ports.append(int(value))
    return ports


def run_text(command: Sequence[str], timeout: float = 5.0) -> Tuple[int, str]:
    try:
        result = subprocess.run(
            list(command),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return 1, ""
    return result.returncode, result.stdout


def configured_clash_ports(home: Path) -> List[int]:
    # config.yaml is Clash Verge's generated active root configuration.  Never
    # scan subscription/profile YAML: nested provider `port:` fields are remote
    # metadata, not trusted local listeners.
    paths = [
        home
        / "Library/Application Support/io.github.clash-verge-rev.clash-verge-rev/config.yaml"
    ]
    ports: List[int] = []
    pattern = re.compile(r"^(mixed-port|port)\s*:\s*([0-9]{1,5})\s*(?:#.*)?$")
    for path in paths:
        try:
            if path.stat().st_size > 2 * 1024 * 1024:
                continue
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    match = pattern.match(line)
                    if not match:
                        continue
                    port = int(match.group(2))
                    if 1 <= port <= 65535 and port not in ports:
                        ports.append(port)
        except OSError:
            continue
    return ports


def libcyber_config_path(home: Path) -> Path:
    """Return LibCyber's one expected generated active root configuration."""
    return home / LIBCYBER_CONFIG_RELATIVE


def metadata_signature(metadata: os.stat_result) -> Tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_uid,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def read_trusted_libcyber_config(home: Path) -> Optional[str]:
    """Open the fixed config without following symlinks and read one stable fd."""
    if not home.is_absolute():
        return None
    required_flags = ("O_DIRECTORY", "O_NOFOLLOW")
    if any(not hasattr(os, name) for name in required_flags):
        return None
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    file_flags = os.O_RDONLY | os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        directory_flags |= os.O_CLOEXEC
        file_flags |= os.O_CLOEXEC
    if hasattr(os, "O_NONBLOCK"):
        file_flags |= os.O_NONBLOCK

    directory_fds: List[int] = []
    file_fd: Optional[int] = None
    try:
        current_fd = os.open(str(home), directory_flags)
        directory_fds.append(current_fd)
        home_metadata = os.fstat(current_fd)
        if (
            not stat.S_ISDIR(home_metadata.st_mode)
            or home_metadata.st_uid != os.getuid()
        ):
            return None
        expected_owner = home_metadata.st_uid
        if home_metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            return None

        parts = LIBCYBER_CONFIG_RELATIVE.parts
        for part in parts[:-1]:
            next_fd = os.open(part, directory_flags, dir_fd=current_fd)
            directory_fds.append(next_fd)
            current_fd = next_fd
            directory_metadata = os.fstat(current_fd)
            if (
                not stat.S_ISDIR(directory_metadata.st_mode)
                or directory_metadata.st_uid != expected_owner
                or directory_metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
            ):
                return None

        file_name = parts[-1]
        file_fd = os.open(file_name, file_flags, dir_fd=current_fd)
        before = os.fstat(file_fd)
        forbidden_mode = (
            stat.S_IWGRP
            | stat.S_IWOTH
            | stat.S_IXUSR
            | stat.S_IXGRP
            | stat.S_IXOTH
        )
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != expected_owner
            or before.st_nlink != 1
            or before.st_mode & forbidden_mode
            or before.st_size > MAX_ACTIVE_CONFIG_BYTES
        ):
            return None

        payload = bytearray()
        while len(payload) <= MAX_ACTIVE_CONFIG_BYTES:
            chunk = os.read(
                file_fd,
                min(64 * 1024, MAX_ACTIVE_CONFIG_BYTES + 1 - len(payload)),
            )
            if not chunk:
                break
            payload.extend(chunk)
        if len(payload) > MAX_ACTIVE_CONFIG_BYTES:
            return None

        after = os.fstat(file_fd)
        named_after = os.stat(file_name, dir_fd=current_fd, follow_symlinks=False)
        if (
            metadata_signature(before) != metadata_signature(after)
            or metadata_signature(after) != metadata_signature(named_after)
        ):
            return None
        return bytes(payload).decode("utf-8", errors="strict")
    except (OSError, UnicodeError):
        return None
    finally:
        if file_fd is not None:
            try:
                os.close(file_fd)
            except OSError:
                pass
        for directory_fd in reversed(directory_fds):
            try:
                os.close(directory_fd)
            except OSError:
                pass


def configured_libcyber_ports(home: Path) -> Dict[str, int]:
    """Read only top-level HTTP/SOCKS ports from LibCyber's active config.

    The parser is intentionally narrower than YAML.  Duplicate, multiline,
    non-decimal, or out-of-range recognized fields reject the entire LibCyber
    configuration.  Subscription/profile files are never scanned.
    """
    text = read_trusted_libcyber_config(home)
    if text is None:
        return {}
    if "\x00" in text:
        return {}

    ports: Dict[str, int] = {}
    recognized_prefix = re.compile(r"^(port|socks-port)(?:\s*:|\s+)")
    valid_field = re.compile(
        r"^(port|socks-port)\s*:\s*([0-9]{1,5})\s*(?:#.*)?$"
    )
    for line in text.splitlines():
        prefix = recognized_prefix.match(line)
        if not prefix:
            continue
        match = valid_field.fullmatch(line)
        if not match:
            return {}
        key = match.group(1)
        port = int(match.group(2))
        if key in ports or not (1 <= port <= 65535):
            return {}
        ports[key] = port
    return ports


def process_executable(pid: int) -> str:
    """Return the kernel-reported executable path for a macOS process."""
    if sys.platform != "darwin" or pid <= 0:
        return ""
    try:
        libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        proc_pidpath = libproc.proc_pidpath
        proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
        proc_pidpath.restype = ctypes.c_int
        buffer = ctypes.create_string_buffer(PROC_PIDPATHINFO_MAXSIZE)
        length = proc_pidpath(pid, buffer, len(buffer))
    except (AttributeError, OSError):
        return ""
    if length <= 0:
        return ""
    try:
        return buffer.value.decode("utf-8", errors="strict")
    except UnicodeError:
        return ""


def process_parent_and_args(
    pid: int, ps_bin: str = "/bin/ps"
) -> Optional[Tuple[int, str]]:
    if pid <= 0:
        return None
    rc, output = run_text(
        [ps_bin, "-p", str(pid), "-o", "ppid=", "-o", "args="], timeout=4.0
    )
    if rc != 0 or "\x00" in output:
        return None
    lines = [line for line in output.splitlines() if line.strip()]
    if len(lines) != 1:
        return None
    match = re.fullmatch(r"\s*([0-9]+)\s+(.+?)\s*", lines[0])
    if not match:
        return None
    return int(match.group(1)), match.group(2)


def parse_lsof_loopback_listener_pid(port: int, output: str) -> Optional[int]:
    if not (1 <= port <= 65535) or "\x00" in output:
        return None

    names_by_pid: Dict[int, List[str]] = {}
    current_pid: Optional[int] = None
    for line in output.splitlines():
        if line.startswith("p"):
            value = line[1:]
            if not value.isdigit() or int(value) <= 0:
                return None
            current_pid = int(value)
            names_by_pid.setdefault(current_pid, [])
        elif line.startswith("n"):
            if current_pid is None:
                return None
            names_by_pid[current_pid].append(line[1:])

    if len(names_by_pid) != 1:
        return None
    pid, listener_names = next(iter(names_by_pid.items()))
    if not listener_names:
        return None
    loopback = re.compile(
        rf"(?:127\.0\.0\.1|localhost|\[::1\]|::1):{port}(?:\s+\(LISTEN\))?"
    )
    if any(not loopback.fullmatch(name) for name in listener_names):
        return None
    return pid


def netstat_line_pid(port: int, fields: Sequence[str]) -> Optional[int]:
    """Parse one strict macOS `netstat -anv -p tcp` LISTEN record."""
    if len(fields) < 19 or fields[0] not in ("tcp4", "tcp6"):
        return None
    expected_local = (
        f"127.0.0.1.{port}" if fields[0] == "tcp4" else f"::1.{port}"
    )
    if fields[3] != expected_local:
        return None
    if fields[4] != "*.*" or fields[5] != "LISTEN":
        return None
    if any(not fields[index].isdigit() for index in (1, 2, 6, 7, 8, 9)):
        return None

    trailing = fields[-8:]
    if (
        any(not re.fullmatch(r"[0-9A-Fa-f]+", value) for value in trailing[:5])
        or not trailing[5].isdigit()
        or not trailing[6].isdigit()
        or not re.fullmatch(r"[0-9A-Fa-f]+", trailing[7])
    ):
        return None
    process_field = " ".join(fields[10:-8])
    process_match = re.fullmatch(r".+:([1-9][0-9]*)", process_field)
    if not process_match:
        return None
    return int(process_match.group(1))


def parse_netstat_loopback_listener_pid(port: int, output: str) -> Optional[int]:
    """Return one PID only when every target-port LISTEN record is trustworthy."""
    if not (1 <= port <= 65535) or "\x00" in output:
        return None
    listener_pids = set()
    target_suffix = f".{port}"
    for line in output.splitlines():
        fields = line.split()
        if not fields or fields[0] not in ("tcp4", "tcp6"):
            continue
        if len(fields) < 6:
            if any(value.endswith(target_suffix) for value in fields[1:]):
                return None
            continue
        local_address = fields[3]
        state_value = fields[5]
        if not local_address.endswith(target_suffix):
            if state_value == "LISTEN" and any(
                value.endswith(target_suffix) for value in fields[1:]
            ):
                return None
            continue
        if state_value != "LISTEN":
            continue
        if local_address not in (f"127.0.0.1.{port}", f"::1.{port}"):
            return None
        pid = netstat_line_pid(port, fields)
        if pid is None:
            return None
        listener_pids.add(pid)
    if len(listener_pids) != 1:
        return None
    return next(iter(listener_pids))


def loopback_listener_pid(
    port: int,
    lsof_bin: str = "/usr/sbin/lsof",
    netstat_bin: str = "/usr/sbin/netstat",
) -> Optional[int]:
    """Return the sole loopback listener PID, falling back if lsof is denied."""
    if not (1 <= port <= 65535):
        return None
    rc, output = run_text(
        [
            lsof_bin,
            "-nP",
            "-a",
            f"-iTCP:{port}",
            "-sTCP:LISTEN",
            "-Fpn",
        ],
        timeout=4.0,
    )
    if rc == 0:
        return parse_lsof_loopback_listener_pid(port, output)
    if output.strip():
        return None
    rc, output = run_text([netstat_bin, "-anv", "-p", "tcp"], timeout=5.0)
    if rc != 0:
        return None
    return parse_netstat_loopback_listener_pid(port, output)


def listener_is_trusted_libcyber(
    port: int,
    config_path: Path,
    lsof_bin: str = "/usr/sbin/lsof",
    ps_bin: str = "/bin/ps",
    netstat_bin: str = "/usr/sbin/netstat",
) -> bool:
    """Bind a LibCyber loopback listener to its core/helper/config lineage."""
    pid = loopback_listener_pid(
        port, lsof_bin=lsof_bin, netstat_bin=netstat_bin
    )
    if pid is None:
        return False
    core_executable = process_executable(pid)
    if core_executable not in LIBCYBER_CORE_EXECUTABLES:
        return False
    process = process_parent_and_args(pid, ps_bin=ps_bin)
    if process is None:
        return False
    parent_pid, args = process
    if parent_pid <= 1 or not args.startswith(core_executable + " "):
        return False
    config_argument = " -f " + str(config_path)
    if args.count(config_argument) != 1 or not args.endswith(config_argument):
        return False
    if process_executable(parent_pid) != LIBCYBER_HELPER_EXECUTABLE:
        return False
    parent = process_parent_and_args(parent_pid, ps_bin=ps_bin)
    if parent is None:
        return False
    _grandparent_pid, parent_args = parent
    if parent_args != LIBCYBER_HELPER_EXECUTABLE:
        return False
    # Re-read kernel-backed identity and the listener after the ps lookups so a
    # process exit/PID-reuse race cannot turn a stale observation into trust.
    return (
        process_executable(pid) == core_executable
        and process_executable(parent_pid) == LIBCYBER_HELPER_EXECUTABLE
        and loopback_listener_pid(
            port, lsof_bin=lsof_bin, netstat_bin=netstat_bin
        ) == pid
    )


def listener_is_trusted_clash(port: int, lsof_bin: str = "/usr/sbin/lsof") -> bool:
    override = os.environ.get("YDR_ADAPTIVE_TEST_LISTENERS", "")
    if override:
        listeners: Dict[int, str] = {}
        for item in override.split(","):
            if "=" not in item:
                continue
            key, value = item.split("=", 1)
            if key.strip().isdigit():
                listeners[int(key.strip())] = value.strip().lower()
        owner = listeners.get(port, "")
        return owner in ("clash", "mihomo", "clashx", "clash-verge")

    rc, output = run_text(
        [lsof_bin, "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"], timeout=4.0
    )
    if rc != 0:
        return False
    for line in output.splitlines()[1:]:
        lowered = line.lower()
        if not re.search(r"(^|\s)(clash|mihomo)", lowered):
            continue
        if re.search(rf"(127\.0\.0\.1|localhost|\[::1\]|::1):{port}\s+\(listen\)", lowered):
            return True
    return False


def discover_candidates(
    home: Path,
    scutil_bin: str,
    lsof_bin: str,
    ps_bin: str = "/bin/ps",
    netstat_bin: str = "/usr/sbin/netstat",
) -> List[str]:
    """Discover DIRECT plus active-config allowlisted loopback HTTP ports."""
    candidates = [DIRECT]
    clash_override = os.environ.get("YDR_ADAPTIVE_TEST_CLASH_PORTS")
    clash_ports = (
        parse_csv_ports(clash_override)
        if clash_override is not None
        else configured_clash_ports(home)
    )
    allowed_ports = set(clash_ports) - DENIED_LOOPBACK_PORTS

    system_override = os.environ.get("YDR_ADAPTIVE_TEST_SYSTEM_PROXY_PORTS")
    if system_override is not None:
        system_ports = parse_csv_ports(system_override)
    else:
        rc, output = run_text([scutil_bin, "--proxy"], timeout=4.0)
        system_ports = parse_scutil_proxy(output) if rc == 0 else []
    for port in system_ports:
        if port not in allowed_ports or not listener_is_trusted_clash(port, lsof_bin=lsof_bin):
            continue
        route = f"{LOOPBACK_PREFIX}{port}"
        if route not in candidates:
            candidates.append(route)

    for port in clash_ports:
        if port in DENIED_LOOPBACK_PORTS:
            continue
        if not listener_is_trusted_clash(port, lsof_bin=lsof_bin):
            continue
        route = f"{LOOPBACK_PREFIX}{port}"
        if route not in candidates:
            candidates.append(route)

    # LibCyber's `port` is its HTTP listener and is compatible with the
    # runner's HTTP_PROXY contract.  `socks-port` is parsed and validated so it
    # cannot be confused with HTTP, but a SOCKS-only config is not emitted as a
    # route by this HTTP-only runner manager.
    libcyber_ports = configured_libcyber_ports(home)
    libcyber_http_port = libcyber_ports.get("port")
    if (
        libcyber_http_port is not None
        and libcyber_http_port not in DENIED_LOOPBACK_PORTS
        and listener_is_trusted_libcyber(
            libcyber_http_port,
            libcyber_config_path(home),
            lsof_bin=lsof_bin,
            ps_bin=ps_bin,
            netstat_bin=netstat_bin,
        )
    ):
        route = f"{LOOPBACK_PREFIX}{libcyber_http_port}"
        if route not in candidates:
            candidates.append(route)
    return candidates


def parse_test_health(value: str) -> Dict[str, bool]:
    result: Dict[str, bool] = {}
    for item in value.split(","):
        if "=" not in item:
            continue
        route, status_value = item.split("=", 1)
        try:
            route = validate_route(route.strip())
        except AdaptiveProxyError:
            continue
        result[route] = status_value.strip().lower() == "ok"
    return result


def probe_route(
    route: str,
    curl_bin: str,
    successes: int,
    connect_timeout: int,
    max_time: int,
    urls: Sequence[str] = PROBE_URLS,
) -> bool:
    route = validate_route(route)
    test_health = os.environ.get("YDR_ADAPTIVE_TEST_HEALTH")
    if test_health is not None:
        return parse_test_health(test_health).get(route, False)
    env = command_env(route)
    for url in urls:
        for _ in range(successes):
            command = [
                curl_bin,
                "--silent",
                "--show-error",
                "--output",
                "/dev/null",
                "--connect-timeout",
                str(connect_timeout),
                "--max-time",
                str(max_time),
            ]
            if route == DIRECT:
                command.extend(["--proxy", "", "--noproxy", "*"])
            else:
                command.extend(
                    [
                        "--proxy",
                        f"http://127.0.0.1:{route_port(route)}",
                        "--noproxy",
                        "",
                    ]
                )
            command.append(url)
            try:
                result = subprocess.run(
                    command,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    env=env,
                    timeout=max_time + 2,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                return False
            if result.returncode != 0:
                return False
    return True


def probe_candidates(
    candidates: Sequence[str],
    curl_bin: str,
    successes: int,
    connect_timeout: int,
    max_time: int,
) -> Dict[str, bool]:
    if not candidates:
        return {}
    workers = min(6, len(candidates))
    result: Dict[str, bool] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(
                probe_route,
                route,
                curl_bin,
                successes,
                connect_timeout,
                max_time,
            ): route
            for route in candidates
        }
        for future in as_completed(futures):
            route = futures[future]
            try:
                result[route] = bool(future.result())
            except Exception:
                result[route] = False
    return result


def read_state(path: Path) -> Dict[str, str]:
    result: Dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except FileNotFoundError:
        return result
    except OSError as exc:
        raise AdaptiveProxyError("state read failed") from exc
    for line in lines:
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        if re.fullmatch(r"[a-z0-9_]+", key):
            result[key] = value
    return result


def atomic_write(path: Path, data: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=path.name + ".tmp.", dir=str(path.parent))
    temporary = Path(temporary_name)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(temporary), str(path))
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            temporary.unlink()
        except OSError:
            pass
        raise


def update_state(path: Path, updates: Mapping[str, str]) -> None:
    existing: List[str]
    try:
        existing = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except FileNotFoundError:
        existing = []
    except OSError as exc:
        raise AdaptiveProxyError("state read failed") from exc
    keys = set(updates)
    kept = [line for line in existing if line.split("=", 1)[0] not in keys]
    for key, value in updates.items():
        if not re.fullmatch(r"[a-z0-9_]+", key) or "\n" in value:
            raise AdaptiveProxyError("invalid state value")
        kept.append(f"{key}={value}")
    payload = ("\n".join(kept) + "\n").encode("utf-8")
    atomic_write(path, payload, 0o600)


def managed_block_bounds(lines: Sequence[str]) -> Optional[Tuple[int, int]]:
    begin = [index for index, line in enumerate(lines) if line == BEGIN_MARKER]
    end = [index for index, line in enumerate(lines) if line == END_MARKER]
    if not begin and not end:
        return None
    if len(begin) != 1 or len(end) != 1 or begin[0] >= end[0]:
        raise AdaptiveProxyError("malformed managed block")
    return begin[0], end[0]


def route_from_env(path: Path) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except FileNotFoundError:
        return DIRECT
    except OSError as exc:
        raise AdaptiveProxyError("runner env read failed") from exc
    bounds = managed_block_bounds(lines)
    if bounds is None:
        return DIRECT
    start, end = bounds
    for line in lines[start + 1 : end]:
        if line.startswith(ROUTE_MARKER_PREFIX):
            return validate_route(line[len(ROUTE_MARKER_PREFIX) :])
    raise AdaptiveProxyError("managed block route missing")


def safe_int(value: Optional[str], default: int = 0) -> int:
    if value is None or not re.fullmatch(r"[0-9]+", value):
        return default
    return int(value)


def evaluate(
    candidates: Sequence[str],
    health: Mapping[str, bool],
    env_current: str,
    state: Mapping[str, str],
    now_epoch: int = 0,
) -> Dict[str, str]:
    current = validate_route(env_current)
    stored_current = state.get("proxy_current_route", current)
    if stored_current != current:
        current_fail_count = 0
        pending_route = ""
        pending_success_count = 0
    else:
        current_fail_count = safe_int(state.get("proxy_current_fail_count"))
        pending_route = state.get("proxy_pending_route", "")
        pending_success_count = safe_int(state.get("proxy_pending_success_count"))

    current_ok = bool(health.get(current, False))
    candidate_route = ""
    decision = "stable"
    if current_ok:
        current_fail_count = 0
        pending_route = ""
        pending_success_count = 0
    else:
        current_fail_count += 1
        quarantine_route = state.get("proxy_quarantine_route", "")
        quarantine_until = safe_int(state.get("proxy_quarantine_until_epoch"))
        for route in candidates:
            if (
                route == quarantine_route
                and quarantine_until > now_epoch
            ):
                continue
            if route != current and health.get(route, False):
                candidate_route = route
                break
        if not candidate_route:
            pending_route = ""
            pending_success_count = 0
            decision = "no_candidate"
        else:
            if pending_route == candidate_route:
                pending_success_count += 1
            else:
                pending_route = candidate_route
                pending_success_count = 1
            if current_fail_count >= 2 and pending_success_count >= 2:
                decision = "switch_ready"
            else:
                decision = "pending"

    return {
        "current_route": current,
        "current_hash": route_hash(current),
        "candidate_route": candidate_route,
        "candidate_hash": route_hash(candidate_route) if candidate_route else "",
        "decision": decision,
        "current_ok": "1" if current_ok else "0",
        "current_fail_count": str(current_fail_count),
        "pending_route": pending_route,
        "pending_success_count": str(pending_success_count),
        "last_switch_epoch": str(safe_int(state.get("proxy_last_switch_epoch"))),
    }


def evaluation_state_updates(result: Mapping[str, str]) -> Dict[str, str]:
    return {
        "proxy_current_route": result["current_route"],
        "proxy_current_hash": result["current_hash"],
        "proxy_current_fail_count": result["current_fail_count"],
        "proxy_pending_route": result["pending_route"],
        "proxy_pending_success_count": result["pending_success_count"],
    }


def render_managed_block(route: str) -> List[str]:
    route = validate_route(route)
    lines = [BEGIN_MARKER, ROUTE_MARKER_PREFIX + route]
    if route != DIRECT:
        url = f"http://127.0.0.1:{route_port(route)}"
        lines.extend(
            [
                f"http_proxy={url}",
                f"https_proxy={url}",
                "no_proxy=localhost,127.0.0.1,::1",
            ]
        )
    lines.append(END_MARKER)
    return lines


def build_env_payload(original: bytes, route: str) -> bytes:
    text = original.decode("utf-8")
    had_final_newline = text.endswith("\n")
    lines = text.splitlines()
    bounds = managed_block_bounds(lines)
    if bounds is not None:
        start, end = bounds
        lines = lines[:start] + lines[end + 1 :]
    unmanaged = re.compile(
        r"^(?:export\s+)?(?:http_proxy|https_proxy|all_proxy|no_proxy)\s*=", re.I
    )
    if any(unmanaged.match(line.strip()) for line in lines):
        raise AdaptiveProxyError("unmanaged proxy assignment present")
    while lines and lines[-1] == "":
        lines.pop()
    if lines:
        lines.append("")
    lines.extend(render_managed_block(route))
    return ("\n".join(lines) + "\n").encode("utf-8")


def write_backup(path: Path, data: bytes, original_mode: int) -> None:
    atomic_write(path, data, 0o600)
    atomic_write(Path(str(path) + ".mode"), f"{original_mode:o}\n".encode("ascii"), 0o600)


def restore_backup(target: Path, backup: Path) -> None:
    try:
        data = backup.read_bytes()
        mode_text = Path(str(backup) + ".mode").read_text(encoding="ascii").strip()
        mode = int(mode_text, 8)
    except (OSError, ValueError) as exc:
        raise AdaptiveProxyError("backup read failed") from exc
    atomic_write(target, data, mode)


def remove_backup(backup: Path) -> None:
    for path in (backup, Path(str(backup) + ".mode")):
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def apply_env(target: Path, backup: Path, route: str, inject_failure: bool = False) -> None:
    route = validate_route(route)
    try:
        original = target.read_bytes()
        original_mode = stat.S_IMODE(target.stat().st_mode)
    except OSError as exc:
        raise AdaptiveProxyError("runner env read failed") from exc
    payload = build_env_payload(original, route)
    write_backup(backup, original, original_mode)
    replaced = False
    try:
        atomic_write(target, payload, 0o600)
        replaced = True
        if inject_failure:
            raise AdaptiveProxyError("injected apply failure")
        if route_from_env(target) != route:
            raise AdaptiveProxyError("runner env verification failed")
        if stat.S_IMODE(target.stat().st_mode) != 0o600:
            raise AdaptiveProxyError("runner env mode verification failed")
    except Exception:
        if replaced:
            restore_backup(target, backup)
        remove_backup(backup)
        raise


def emit_result(result: Mapping[str, str]) -> None:
    allowed = re.compile(r"[A-Za-z0-9_:.-]*")
    for key in (
        "current_route",
        "current_hash",
        "candidate_route",
        "candidate_hash",
        "decision",
        "current_ok",
        "current_fail_count",
        "pending_route",
        "pending_success_count",
        "last_switch_epoch",
    ):
        value = result.get(key, "")
        if not allowed.fullmatch(value):
            raise AdaptiveProxyError("unsafe result")
        print(f"{key}={value}")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser()
    sub = result.add_subparsers(dest="command", required=True)

    evaluate_parser = sub.add_parser("evaluate")
    evaluate_parser.add_argument("--state-file", required=True)
    evaluate_parser.add_argument("--runner-env", required=True)
    evaluate_parser.add_argument("--curl-bin", default="curl")
    evaluate_parser.add_argument("--scutil-bin", default="/usr/sbin/scutil")
    evaluate_parser.add_argument("--lsof-bin", default="/usr/sbin/lsof")
    evaluate_parser.add_argument("--ps-bin", default="/bin/ps")
    evaluate_parser.add_argument("--netstat-bin", default="/usr/sbin/netstat")
    evaluate_parser.add_argument("--probe-successes", type=int, default=2)
    evaluate_parser.add_argument("--connect-timeout", type=int, default=5)
    evaluate_parser.add_argument("--max-time", type=int, default=10)
    evaluate_parser.add_argument("--now-epoch", type=int, default=0)
    evaluate_parser.add_argument("--dry-run", action="store_true")

    apply_parser = sub.add_parser("env-apply")
    apply_parser.add_argument("--runner-env", required=True)
    apply_parser.add_argument("--backup", required=True)
    apply_parser.add_argument("--route", required=True)
    apply_parser.add_argument("--inject-failure-after-replace", action="store_true")

    restore_parser = sub.add_parser("env-restore")
    restore_parser.add_argument("--runner-env", required=True)
    restore_parser.add_argument("--backup", required=True)

    commit_parser = sub.add_parser("env-commit")
    commit_parser.add_argument("--backup", required=True)

    mark_parser = sub.add_parser("mark-switched")
    mark_parser.add_argument("--state-file", required=True)
    mark_parser.add_argument("--route", required=True)
    mark_parser.add_argument("--now-epoch", type=int, required=True)

    current_parser = sub.add_parser("current-route")
    current_parser.add_argument("--runner-env", required=True)
    return result


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "evaluate":
            if args.probe_successes < 1 or args.probe_successes > 5:
                raise AdaptiveProxyError("invalid probe count")
            test_candidates = os.environ.get("YDR_ADAPTIVE_TEST_CANDIDATES")
            if test_candidates is not None:
                candidates: List[str] = []
                for value in test_candidates.split(","):
                    route = validate_route(value.strip())
                    if route not in candidates:
                        candidates.append(route)
                if DIRECT not in candidates:
                    candidates.insert(0, DIRECT)
            else:
                candidates = discover_candidates(
                    Path.home(),
                    args.scutil_bin,
                    args.lsof_bin,
                    args.ps_bin,
                    args.netstat_bin,
                )
            health = probe_candidates(
                candidates,
                args.curl_bin,
                args.probe_successes,
                args.connect_timeout,
                args.max_time,
            )
            state_path = Path(args.state_file)
            result = evaluate(
                candidates,
                health,
                route_from_env(Path(args.runner_env)),
                read_state(state_path),
                args.now_epoch,
            )
            if not args.dry_run:
                update_state(state_path, evaluation_state_updates(result))
            emit_result(result)
        elif args.command == "env-apply":
            apply_env(
                Path(args.runner_env),
                Path(args.backup),
                args.route,
                args.inject_failure_after_replace,
            )
        elif args.command == "env-restore":
            restore_backup(Path(args.runner_env), Path(args.backup))
            remove_backup(Path(args.backup))
        elif args.command == "env-commit":
            remove_backup(Path(args.backup))
        elif args.command == "mark-switched":
            route = validate_route(args.route)
            update_state(
                Path(args.state_file),
                {
                    "proxy_current_route": route,
                    "proxy_current_hash": route_hash(route),
                    "proxy_current_fail_count": "0",
                    "proxy_pending_route": "",
                    "proxy_pending_success_count": "0",
                    "proxy_last_switch_epoch": str(args.now_epoch),
                    "proxy_quarantine_route": "",
                    "proxy_quarantine_until_epoch": "0",
                },
            )
        elif args.command == "current-route":
            print(route_from_env(Path(args.runner_env)))
        return 0
    except (AdaptiveProxyError, UnicodeError, OSError) as exc:
        # Fixed-class error only: never print paths, config values, or raw exceptions.
        print(f"adaptive-proxy error={exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
