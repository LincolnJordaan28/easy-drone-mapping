# -*- coding: utf-8 -*-
"""Minimal NodeODM REST client.

Pure standard library (no Qt, no QGIS, no third-party packages) so it works
in every QGIS Python environment and can be tested on its own.

API reference: https://github.com/OpenDroneMap/NodeODM/blob/master/docs/index.adoc
"""
import http.client
import json
import mimetypes
import os
import ssl
import time
import uuid as uuidlib
from urllib.parse import quote, urlencode, urlsplit

# Task status codes used by NodeODM
QUEUED, RUNNING, FAILED, COMPLETED, CANCELED = 10, 20, 30, 40, 50

STATUS_TEXT = {
    QUEUED: "Queued",
    RUNNING: "Running",
    FAILED: "Failed",
    COMPLETED: "Completed",
    CANCELED: "Canceled",
}

CHUNK = 1024 * 256


class NodeError(Exception):
    """A problem talking to the processing engine, with a readable message."""


class Canceled(Exception):
    """Raised when the caller's ``is_canceled`` callback returns True."""


class NodeClient:
    def __init__(self, url="http://localhost:3000", token="", timeout=30):
        url = (url or "").strip()
        if "://" not in url:
            url = "http://" + url
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise NodeError("The engine address '%s' is not valid. "
                            "It should look like http://localhost:3000" % url)
        self.scheme = parts.scheme
        self.host = parts.hostname
        self.port = parts.port or (443 if parts.scheme == "https" else 80)
        self.base_path = parts.path.rstrip("/")
        self.token = (token or "").strip()
        self.timeout = timeout

    # ------------------------------------------------------------------ helpers
    @property
    def url(self):
        return "%s://%s:%s%s" % (self.scheme, self.host, self.port, self.base_path)

    @property
    def is_local(self):
        return self.host in ("localhost", "127.0.0.1", "::1")

    def _connect(self, timeout=None):
        timeout = timeout or self.timeout
        if self.scheme == "https":
            return http.client.HTTPSConnection(
                self.host, self.port, timeout=timeout,
                context=ssl.create_default_context())
        return http.client.HTTPConnection(self.host, self.port, timeout=timeout)

    def _path(self, path, query=None):
        query = dict(query or {})
        if self.token:
            query["token"] = self.token
        full = self.base_path + path
        if query:
            full += "?" + urlencode(query)
        return full

    def _request(self, method, path, query=None, body=None, headers=None,
                 timeout=None):
        conn = self._connect(timeout)
        try:
            conn.request(method, self._path(path, query), body=body,
                         headers=headers or {})
            resp = conn.getresponse()
            data = resp.read()
            return resp.status, data
        except (OSError, http.client.HTTPException) as e:
            raise NodeError(self._friendly_network_error(e))
        finally:
            conn.close()

    def _friendly_network_error(self, e):
        if isinstance(e, ConnectionRefusedError):
            return ("The processing engine is not running at %s." % self.url)
        if isinstance(e, TimeoutError) or "timed out" in str(e).lower():
            return "The processing engine at %s did not answer in time." % self.url
        return "Could not reach the processing engine at %s (%s)." % (self.url, e)

    def _json(self, method, path, query=None, body=None, headers=None,
              timeout=None):
        status, data = self._request(method, path, query, body, headers, timeout)
        if status in (401, 403):
            raise NodeError("The engine rejected the access token. "
                            "Check the token in Settings.")
        try:
            parsed = json.loads(data.decode("utf-8", "replace"))
        except ValueError:
            raise NodeError(
                "Unexpected answer from %s (HTTP %s). Is this really an "
                "ODM engine (NodeODM)?" % (self.url, status))
        if isinstance(parsed, dict) and parsed.get("error"):
            raise NodeError(str(parsed["error"]))
        return parsed

    @staticmethod
    def _multipart_fields(fields):
        boundary = "----easyodm" + uuidlib.uuid4().hex
        out = []
        for k, v in fields.items():
            out.append("--%s\r\nContent-Disposition: form-data; name=\"%s\""
                       "\r\n\r\n%s\r\n" % (boundary, k, v))
        out.append("--%s--\r\n" % boundary)
        body = "".join(out).encode("utf-8")
        headers = {"Content-Type": "multipart/form-data; boundary=" + boundary,
                   "Content-Length": str(len(body))}
        return body, headers

    # --------------------------------------------------------------------- info
    def info(self, timeout=5):
        info = self._json("GET", "/info", timeout=timeout)
        if not isinstance(info, dict) or "version" not in info:
            raise NodeError("%s answered, but it does not look like an ODM "
                            "engine (NodeODM)." % self.url)
        return info

    def options(self):
        """Return the list of options the engine supports."""
        return self._json("GET", "/options")

    def task_list(self):
        return [t.get("uuid") for t in self._json("GET", "/task/list")]

    def task_info(self, uuid):
        return self._json("GET", "/task/%s/info" % quote(uuid))

    def task_output(self, uuid, line=0):
        out = self._json("GET", "/task/%s/output" % quote(uuid), {"line": line})
        return out if isinstance(out, list) else []

    # ----------------------------------------------------------------- creation
    def task_init(self, name, options, skip_post_processing=True):
        """options: dict of {option-name: value}. Returns the new task uuid."""
        opts = [{"name": k, "value": v} for k, v in options.items()]
        fields = {"name": name, "options": json.dumps(opts)}
        if skip_post_processing:
            # QGIS reads the GeoTIFF / LAZ directly; web tiles are not needed.
            fields["skipPostProcessing"] = "true"
        body, headers = self._multipart_fields(fields)
        res = self._json("POST", "/task/new/init", body=body, headers=headers)
        if "uuid" not in res:
            raise NodeError("The engine did not create the job.")
        return res["uuid"]

    def task_upload(self, uuid, filepath, is_canceled=None, retries=5):
        """Upload one file (streamed from disk, never loaded whole in memory)."""
        last = None
        for attempt in range(retries):
            if is_canceled and is_canceled():
                raise Canceled()
            try:
                self._upload_once(uuid, filepath, is_canceled)
                return
            except Canceled:
                raise
            except NodeError as e:
                last = e
                if getattr(e, "no_retry", False):
                    break
                time.sleep(min(2 ** attempt, 15))
        raise NodeError("Could not upload %s: %s"
                        % (os.path.basename(filepath), last))

    def _upload_once(self, uuid, filepath, is_canceled):
        filename = os.path.basename(filepath)
        size = os.path.getsize(filepath)
        ctype = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        boundary = "----easyodm" + uuidlib.uuid4().hex
        safe_name = filename.replace('"', "_")
        head = ("--%s\r\nContent-Disposition: form-data; name=\"images\"; "
                "filename=\"%s\"\r\nContent-Type: %s\r\n\r\n"
                % (boundary, safe_name, ctype)).encode("utf-8")
        tail = ("\r\n--%s--\r\n" % boundary).encode("utf-8")

        conn = self._connect(timeout=120)
        try:
            conn.putrequest("POST", self._path("/task/new/upload/%s" % quote(uuid)))
            conn.putheader("Content-Type",
                           "multipart/form-data; boundary=" + boundary)
            conn.putheader("Content-Length", str(len(head) + size + len(tail)))
            conn.endheaders()
            conn.send(head)
            with open(filepath, "rb") as f:
                while True:
                    if is_canceled and is_canceled():
                        raise Canceled()
                    chunk = f.read(CHUNK)
                    if not chunk:
                        break
                    conn.send(chunk)
            conn.send(tail)
            resp = conn.getresponse()
            data = resp.read()
        except (OSError, http.client.HTTPException) as e:
            raise NodeError(self._friendly_network_error(e))
        finally:
            conn.close()

        if resp.status != 200:
            raise NodeError("upload refused (HTTP %s)" % resp.status)
        try:
            parsed = json.loads(data.decode("utf-8", "replace"))
        except ValueError:
            raise NodeError("unexpected answer from the engine")
        if parsed.get("error"):
            err = NodeError(str(parsed["error"]))
            err.no_retry = bool(parsed.get("noRetry"))
            raise err

    def task_commit(self, uuid):
        res = self._json("POST", "/task/new/commit/%s" % quote(uuid),
                         headers={"Content-Length": "0"}, timeout=300)
        return res.get("uuid", uuid)

    # ------------------------------------------------------------------ control
    def _task_action(self, action, uuid):
        body = urlencode({"uuid": uuid}).encode("utf-8")
        headers = {"Content-Type": "application/x-www-form-urlencoded",
                   "Content-Length": str(len(body))}
        return self._json("POST", "/task/" + action, body=body, headers=headers)

    def task_cancel(self, uuid):
        return self._task_action("cancel", uuid)

    def task_remove(self, uuid):
        return self._task_action("remove", uuid)

    # ----------------------------------------------------------------- download
    def download_all(self, uuid, dest_path, progress=None, is_canceled=None):
        """Download all.zip to dest_path. progress(received_bytes, total_bytes)."""
        tmp = dest_path + ".part"
        conn = self._connect(timeout=120)
        try:
            conn.request("GET", self._path("/task/%s/download/all.zip" % quote(uuid)))
            resp = conn.getresponse()
            if resp.status != 200:
                detail = resp.read(2000).decode("utf-8", "replace")
                try:
                    detail = json.loads(detail).get("error", detail)
                except ValueError:
                    pass
                raise NodeError("The results could not be downloaded (HTTP %s). %s"
                                % (resp.status, detail))
            total = int(resp.getheader("Content-Length") or 0)
            received = 0
            with open(tmp, "wb") as f:
                while True:
                    if is_canceled and is_canceled():
                        raise Canceled()
                    chunk = resp.read(CHUNK)
                    if not chunk:
                        break
                    f.write(chunk)
                    received += len(chunk)
                    if progress:
                        progress(received, total)
            if total and received != total:
                raise NodeError("The download was interrupted "
                                "(%d of %d bytes)." % (received, total))
        except (OSError, http.client.HTTPException) as e:
            raise NodeError(self._friendly_network_error(e))
        finally:
            conn.close()
            if os.path.exists(tmp) and (is_canceled and is_canceled()):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
        if os.path.exists(dest_path):
            os.remove(dest_path)
        os.replace(tmp, dest_path)
        return dest_path
