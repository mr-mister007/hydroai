import socket
import threading
import select
import time
import os
import signal
import sys
from .storage import Storage, save_pid, clear_pid, save_session, load_session
from .watercalc import identify_tool, identify_api, estimate_water


class ProxyServer:
    def __init__(self, host="127.0.0.1", port=8090):
        self.host = host
        self.port = port
        self.server = None
        self.running = False
        self.storage = Storage()
        self.session_id = load_session() or f"session_{int(time.time())}"
        save_session(self.session_id)
        self.lock = threading.Lock()

    def start(self):
        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server.bind((self.host, self.port))
        self.server.listen(50)
        self.server.settimeout(1.0)
        self.running = True

        save_pid(os.getpid())

        print(f"🌊 watermeter proxy running on {self.host}:{self.port}")
        print(f"   Session: {self.session_id}")
        print(f"   Set HTTPS_PROXY=http://{self.host}:{self.port} in your shell")
        print("   Press Ctrl+C to stop\n")

        while self.running:
            try:
                client, addr = self.server.accept()
                t = threading.Thread(
                    target=self._handle_client, args=(client, addr), daemon=True
                )
                t.start()
            except socket.timeout:
                continue
            except OSError:
                break

        self.server.close()
        clear_pid()

    def stop(self):
        self.running = False

    def _handle_client(self, client: socket.socket, addr):
        try:
            data = client.recv(65536)
            if not data:
                client.close()
                return

            first_line = data.split(b"\r\n")[0].decode("utf-8", errors="replace")

            if data.startswith(b"CONNECT"):
                self._handle_connect(client, data, first_line)
            else:
                self._handle_http(client, data, first_line)
        except Exception as e:
            pass
        finally:
            try:
                client.close()
            except OSError:
                pass

    def _parse_headers(self, data: bytes) -> dict:
        headers = {}
        for line in data.split(b"\r\n")[1:]:
            if not line:
                break
            if b":" in line:
                key, val = line.split(b":", 1)
                headers[key.decode("utf-8", errors="replace").strip()] = (
                    val.decode("utf-8", errors="replace").strip()
                )
        return headers

    def _handle_http(self, client, data, first_line):
        parts = first_line.split()
        if len(parts) < 2:
            client.close()
            return

        method = parts[0]
        url = parts[1]
        headers = self._parse_headers(data)
        user_agent = headers.get("User-Agent", "unknown")
        tool = identify_tool(user_agent)

        from urllib.parse import urlparse

        parsed = urlparse(url)
        host = parsed.hostname or ""
        port = parsed.port or 80
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query

        api = identify_api(host)

        if host:
            try:
                remote = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                remote.connect((host, port))
                remote.sendall(data)

                response = b""
                while True:
                    chunk = remote.recv(65536)
                    if not chunk:
                        break
                    response += chunk

                client.sendall(response)

                with self.lock:
                    water_ml, in_tok, out_tok = estimate_water(
                        len(data), len(response), api
                    )
                    self.storage.record_call(
                        tool=tool,
                        api=api,
                        endpoint=f"{host}:{port}{path}",
                        bytes_sent=len(data),
                        bytes_received=len(response),
                        estimated_input_tokens=in_tok,
                        estimated_output_tokens=out_tok,
                        water_ml=water_ml,
                        duration_sec=0,
                        session_id=self.session_id,
                    )
                remote.close()
            except Exception:
                client.sendall(
                    b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n"
                )
        client.close()

    def _handle_connect(self, client, data, first_line):
        parts = first_line.split()
        if len(parts) < 2:
            client.close()
            return

        host_port = parts[1]
        headers = self._parse_headers(data)
        user_agent = headers.get("User-Agent", "unknown")
        tool = identify_tool(user_agent)

        if ":" in host_port:
            host, port_str = host_port.split(":")
            port = int(port_str)
        else:
            host = host_port
            port = 443

        api = identify_api(host)

        remote = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            remote.connect((host, port))
            client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        except Exception:
            client.sendall(
                b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n"
            )
            client.close()
            return

        bytes_to_server = 0
        bytes_from_server = 0
        start_time = time.time()

        client.settimeout(None)
        remote.settimeout(None)

        sockets = [client, remote]
        data_buffer = {client: b"", remote: b""}

        try:
            while True:
                readable, _, exceptional = select.select(sockets, [], sockets, 30.0)

                if exceptional:
                    break

                if not readable:
                    break

                for s in readable:
                    try:
                        chunk = s.recv(65536)
                    except OSError:
                        chunk = b""

                    if not chunk:
                        sockets.remove(s)
                        s.close()
                        if len(sockets) == 0:
                            raise StopIteration
                        continue

                    if s is client:
                        remote.sendall(chunk)
                        bytes_to_server += len(chunk)
                    else:
                        client.sendall(chunk)
                        bytes_from_server += len(chunk)
        except (StopIteration, OSError, ConnectionError):
            pass

        try:
            client.close()
        except OSError:
            pass
        try:
            remote.close()
        except OSError:
            pass

        elapsed = time.time() - start_time

        with self.lock:
            water_ml, in_tok, out_tok = estimate_water(
                bytes_to_server, bytes_from_server, api
            )
            if bytes_to_server > 0 or bytes_from_server > 0:
                self.storage.record_call(
                    tool=tool,
                    api=api,
                    endpoint=f"{host}:{port}",
                    bytes_sent=bytes_to_server,
                    bytes_received=bytes_from_server,
                    estimated_input_tokens=in_tok,
                    estimated_output_tokens=out_tok,
                    water_ml=water_ml,
                    duration_sec=elapsed,
                    session_id=self.session_id,
                )
