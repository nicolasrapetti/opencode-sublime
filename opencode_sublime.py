import html
import difflib
import base64
import mimetypes
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
import urllib.parse
import json
import threading
import urllib.error
import urllib.request
import uuid
from datetime import datetime

import sublime
import sublime_plugin

BASE_URL = "http://127.0.0.1:4096"

LAYOUT = {
    "cols": [0.0, 0.70, 1.0],
    "rows": [0.0, 1.0],
    "cells": [
        [0, 0, 1, 1],
        [1, 0, 2, 1],
    ],
}

_states = {}
_server_lock = threading.Lock()
_server_process = None
_session_lock = threading.Lock()


class OpenCodeConnectionError(RuntimeError):
    pass


def project_directory(window):
    folders = window.folders()
    active = window.active_view()
    filename = active.file_name() if active and not active.settings().get("opencode_composer") else None
    if filename:
        filename = os.path.abspath(filename)
        matches = []
        for folder in folders:
            try:
                if os.path.commonpath([filename, os.path.abspath(folder)]) == os.path.abspath(folder):
                    matches.append(folder)
            except ValueError:
                pass
        if matches:
            return os.path.abspath(max(matches, key=len))
    if folders:
        return os.path.abspath(folders[0])
    if filename:
        return os.path.dirname(filename)
    return None


def ensure_server(directory):
    """Reuse the existing server or start one, on a worker thread only."""
    global _server_process
    with _server_lock:
        try:
            health = request_json("GET", "/global/health", timeout=3)
            if isinstance(health, dict) and health.get("healthy"):
                return health
            raise RuntimeError("OpenCode answered but is not healthy.")
        except OpenCodeConnectionError:
            pass
        if not directory or not os.path.isdir(directory):
            raise RuntimeError("Open a project folder in Sublime before starting OpenCode.")
        settings = sublime.load_settings("OpenCodeSublime.sublime-settings")
        executable = settings.get("opencode_executable") or shutil.which("opencode")
        if not executable and sublime.platform() == "windows":
            candidate = os.path.join(os.path.expanduser("~"), "scoop", "shims", "opencode.exe")
            if os.path.isfile(candidate):
                executable = candidate
        if not executable:
            raise RuntimeError("OpenCode executable not found. Set opencode_executable in the plugin settings.")
        if _server_process is None or _server_process.poll() is not None:
            log_dir = os.path.join(sublime.cache_path(), "OpenCodeSublime")
            os.makedirs(log_dir, exist_ok=True)
            log_path = os.path.join(log_dir, "server.log")
            with open(log_path, "ab") as log:
                _server_process = subprocess.Popen(
                    [executable, "serve", "--hostname", "127.0.0.1", "--port", "4096"],
                    cwd=directory,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
                )
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                health = request_json("GET", "/global/health", timeout=1)
                if isinstance(health, dict) and health.get("healthy"):
                    return health
            except OpenCodeConnectionError:
                pass
            if _server_process.poll() not in (None, 0):
                break
            time.sleep(0.25)
        raise RuntimeError("OpenCode did not become ready. See OpenCodeSublime/server.log in Sublime's cache directory.")


def mention_at(view, point):
    before = view.substr(sublime.Region(1, point))
    match = re.search(r'(?<!\S)@([^\s@"<>]*)$', before)
    if match:
        return 1 + match.start(), point, match.group(1)
    return None


def index_project_files(directory):
    """Use Git's ignore rules when available, with a bounded fallback scan."""
    limit = 30000
    if not directory:
        return []
    settings = sublime.load_settings("OpenCodeSublime.sublime-settings")
    excluded = set(settings.get("context_excluded_folders") or [
        ".git", ".svn", ".hg", "node_modules", ".venv", "venv",
        "__pycache__", "dist", "build", ".next", ".nuxt",
    ])
    paths = None
    if shutil.which("git"):
        try:
            result = subprocess.run(
                ["git", "-C", directory, "ls-files", "--cached", "--others", "--exclude-standard", "-z", "--", "."],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=20,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
            )
            if result.returncode == 0:
                paths = result.stdout.decode("utf-8", errors="replace").split("\0")
        except (OSError, subprocess.TimeoutExpired):
            pass
    if paths is None:
        paths = []
        for base, dirs, files in os.walk(directory, followlinks=False):
            dirs[:] = sorted(d for d in dirs if d not in excluded and not os.path.islink(os.path.join(base, d)))
            paths.extend(os.path.relpath(os.path.join(base, f), directory) for f in files)
            if len(paths) >= limit:
                break
    result = []
    seen = set()
    for relative in paths:
        relative = relative.replace("\\", "/")
        if not relative or any(part in excluded for part in relative.split("/")[:-1]):
            continue
        absolute = os.path.realpath(os.path.join(directory, relative))
        try:
            if os.path.commonpath([os.path.realpath(directory), absolute]) != os.path.realpath(directory):
                continue
        except ValueError:
            continue
        if absolute not in seen and os.path.isfile(absolute):
            seen.add(absolute)
            result.append({"relative": relative, "absolute": absolute})
        if len(result) >= limit:
            break
    return sorted(result, key=lambda f: f["relative"].lower())


def refresh_files(window_id):
    state = _states.get(window_id)
    if not state or state.get("indexing"):
        return
    state["indexing"] = True
    directory = state["directory"]
    def worker():
        try:
            files = index_project_files(directory)
            error = ""
        except Exception as exc:
            files, error = [], str(exc)
        def finish():
            if _states.get(window_id) is not state:
                return
            state["files"] = files
            state["indexing"] = False
            if error:
                sublime.status_message("OpenCode file search: " + error)
            view = state["composer"]
            if view and view.is_valid() and view.sel() and mention_at(view, view.sel()[0].b):
                view.run_command("auto_complete", {"disable_auto_insert": True, "next_completion_if_showing": False})
        sublime.set_timeout(finish)
    threading.Thread(target=worker, daemon=True).start()


def context_parts(state, prompt):
    result, seen = [], set()
    directory = state.get("directory")
    if not directory:
        return result
    # Only actual files inside the current project become attachments.
    for match in re.finditer(r'(?<!\S)@(?:"([^"\n]+)"|([^\s@<>]+))', prompt):
        relative = match.group(1) or match.group(2)
        absolute = os.path.realpath(os.path.join(directory, relative))
        try:
            if os.path.commonpath([os.path.realpath(directory), absolute]) != os.path.realpath(directory):
                continue
        except ValueError:
            continue
        if not os.path.isfile(absolute) or absolute in seen:
            continue
        seen.add(absolute)
        mime = mimetypes.guess_type(absolute)[0] or "text/plain"
        if not mime.startswith("image/") and mime != "application/pdf":
            mime = "text/plain"
        result.append({"type": "file", "mime": mime, "filename": relative, "url": Path(absolute).as_uri()})
    return result



def request_json(method, path, body=None, timeout=120, directory=None):
    if directory:
        path += ("&" if "?" in path else "?") + urllib.parse.urlencode({"directory": directory})
    data = None
    headers = {"Accept": "application/json"}
    password = os.environ.get("OPENCODE_SERVER_PASSWORD")
    if password:
        username = os.environ.get("OPENCODE_SERVER_USERNAME", "opencode")
        encoded = base64.b64encode((username + ":" + password).encode("utf-8")).decode("ascii")
        headers["Authorization"] = "Basic " + encoded
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(
        BASE_URL + path,
        data=data,
        headers=headers,
        method=method,
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            raw = response.read()
            return json.loads(raw.decode("utf-8")) if raw else None
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise RuntimeError("HTTP {} {}\n{}".format(error.code, error.reason, detail))
    except urllib.error.URLError as error:
        raise OpenCodeConnectionError("Could not connect to OpenCode: {}".format(error.reason))


def get_window(window_id):
    for window in sublime.windows():
        if window.id() == window_id:
            return window
    return None


def project_key(directory):
    return os.path.normcase(os.path.realpath(os.path.abspath(directory))) if directory else None


def saved_project_session(directory):
    key = project_key(directory)
    with _session_lock:
        records = sublime.load_settings("OpenCodeSublime.sublime-settings").get("project_sessions") or {}
        record = records.get(key) if isinstance(records, dict) and key else None
        return dict(record) if isinstance(record, dict) else {}


def remember_project_session(state):
    key = project_key(state.get("directory"))
    if not key:
        return
    composer = state.get("composer")
    draft = draft_text(composer) if composer is not None and composer.is_valid() else state.get("saved_draft", "")
    with _session_lock:
        settings = sublime.load_settings("OpenCodeSublime.sublime-settings")
        previous = settings.get("project_sessions") or {}
        records = dict(previous) if isinstance(previous, dict) else {}
        sid = state.get("session_id") or (state.get("restore_id") if state.get("restore_pending") else None)
        records[key] = {"session_id": sid, "title": state.get("session_title") or "New chat", "draft": draft}
        settings.set("project_sessions", records)
        sublime.save_settings("OpenCodeSublime.sublime-settings")


def new_state(window):
    settings = sublime.load_settings("OpenCodeSublime.sublime-settings")
    color = settings.get("color_mode", "light")
    agent = settings.get("last_agent")
    agent = agent if isinstance(agent, str) and agent else None
    saved_model = settings.get("last_model")
    model = None
    if isinstance(saved_model, dict) and all(
        isinstance(saved_model.get(key), str) and saved_model[key]
        for key in ("provider_id", "model_id")
    ):
        model = {
            "provider_id": saved_model["provider_id"],
            "model_id": saved_model["model_id"],
            "label": "{}/{}".format(saved_model["provider_id"], saved_model["model_id"]),
        }
    directory = project_directory(window)
    remembered = saved_project_session(directory)
    saved_id = remembered.get("session_id")
    saved_id = saved_id if isinstance(saved_id, str) and saved_id.startswith("ses_") else None
    return {
        "window_id": window.id(),
        "directory": directory,
        "files": [],
        "indexing": False,
        "connecting": False,
        "mention_request": 0,
        "old_layout": window.layout(),
        "phantoms": None,
        "color_mode": color if color in ("light", "dark") else "light",
        "composer": None,
        "session_id": None,
        "restore_id": saved_id,
        "restore_pending": bool(saved_id),
        "session_title": remembered.get("title") or "New chat",
        "session_loading": False,
        "session_request": 0,
        "session_revision": 0,
        "session_error": "",
        "session_blocked": False,
        "rename_inflight": False,
        "diff_open": False,
        "diff_loading": False,
        "diff_error": "",
        "diffs": [],
        "diff_request": 0,
        "saved_draft": remembered.get("draft") if isinstance(remembered.get("draft"), str) else "",
        "turns": [],
        "busy": False,
        "active_turn": None,
        "connected": False,
        "version": "",
        "error": "",
        "agents": [],
        "agent": agent or "build",
        "preferred_agent": agent,
        "models": [],
        "selected_model": model,
        "config_model": None,
        "effective_model": None,
        "generation": 0,
    }


def save_preferences(state):
    settings = sublime.load_settings("OpenCodeSublime.sublime-settings")
    settings.set("color_mode", state["color_mode"])
    settings.set("last_agent", state["agent"])
    model = state["selected_model"]
    settings.set("last_model", {
        "provider_id": model["provider_id"],
        "model_id": model["model_id"],
    } if model else None)
    sublime.save_settings("OpenCodeSublime.sublime-settings")


def message_time(timestamp):
    if not timestamp:
        return ""
    try:
        # Keep the local wall-clock time and offset captured on the computer.
        return datetime.fromisoformat(timestamp).strftime("%H:%M")
    except (ValueError, TypeError):
        return ""


def state_for(window):
    state = _states.get(window.id())
    if state is None:
        state = new_state(window)
        _states[window.id()] = state
    return state


def esc(value):
    return html.escape(str(value or ""), quote=True)


def cmd_url(command, args=None):
    # command_url already returns an HTML-embeddable URL. Escaping it again
    # turns &quot; into &amp;quot; and breaks the JSON arguments on click.
    return sublime.command_url(command, args or {})


def parse_agents(data):
    agents = []
    for item in data if isinstance(data, list) else []:
        if not isinstance(item, dict) or not item.get("name"):
            continue
        if item.get("hidden") or item.get("disable"):
            continue
        if item.get("mode", "all") not in ("primary", "all", None):
            continue
        agents.append({
            "name": item["name"],
            "description": item.get("description") or "Primary agent",
            "model": item.get("model"),
        })
    agents.sort(key=lambda x: (0 if x["name"] == "build" else 1, x["name"]))
    return agents


def parse_models(data):
    if not isinstance(data, dict):
        return []

    connected = data.get("connected")
    connected = set(connected) if isinstance(connected, list) and connected else None
    result = []

    for provider in data.get("all") or []:
        if not isinstance(provider, dict) or not provider.get("id"):
            continue

        provider_id = provider["id"]
        if connected and provider_id not in connected:
            continue

        provider_name = provider.get("name") or provider_id
        models = provider.get("models") or {}
        entries = models.items() if isinstance(models, dict) else [
            (m.get("id"), m) for m in models if isinstance(m, dict)
        ]

        for fallback_id, model in entries:
            if not isinstance(model, dict):
                continue
            model_id = model.get("id") or fallback_id
            if not model_id:
                continue
            model_name = model.get("name") or model_id
            result.append({
                "provider_id": provider_id,
                "provider_name": provider_name,
                "model_id": model_id,
                "model_name": model_name,
                "label": "{} · {}".format(provider_name, model_name),
            })

    result.sort(key=lambda x: (x["provider_name"].lower(), x["model_name"].lower()))
    return result


def resolve_model(state, provider_id, model_id):
    for model in state["models"]:
        if model["provider_id"] == provider_id and model["model_id"] == model_id:
            return model["label"]
    return "{}/{}".format(provider_id, model_id)


def label_from_ref(state, ref):
    if isinstance(ref, dict):
        return resolve_model(state, ref.get("providerID", ""), ref.get("modelID", ""))
    if not ref or "/" not in ref:
        return ref
    provider_id, model_id = ref.split("/", 1)
    return resolve_model(state, provider_id, model_id)


def current_model_label(state):
    if state["selected_model"]:
        return state["selected_model"]["label"]

    if state["effective_model"]:
        m = state["effective_model"]
        return "Auto · " + resolve_model(state, m["provider_id"], m["model_id"])

    for agent in state["agents"]:
        if agent["name"] == state["agent"] and agent.get("model"):
            return "Auto · " + label_from_ref(state, agent["model"])

    if state["config_model"]:
        return "Auto · " + label_from_ref(state, state["config_model"])

    return "Auto · OpenCode default"


def markdown_segments(text):
    segments, lines = [], []
    fence, language = None, ""
    for line in str(text or "").splitlines(keepends=True):
        match = re.match(r"^ {0,3}(`{3,}|~{3,})([^\r\n]*)[\r\n]*$", line)
        if fence is None and match and not (match.group(1)[0] == "`" and "`" in match.group(2)):
            if lines:
                segments.append({"type": "text", "text": "".join(lines)})
            lines, fence, language = [], match.group(1), match.group(2).strip()
        elif fence and match and match.group(1)[0] == fence[0] and len(match.group(1)) >= len(fence) and not match.group(2).strip():
            segments.append({"type": "code", "text": "".join(lines), "language": language})
            lines, fence, language = [], None, ""
        else:
            lines.append(line)
    if lines or fence:
        segments.append({"type": "code" if fence else "text", "text": "".join(lines), "language": language})
    return segments


def message_html(text, code_context=None):
    out, code_index = [], 0
    for segment in markdown_segments(text):
        if segment["type"] == "code":
            caption = ""
            if code_context is not None:
                args = dict(code_context, block_index=code_index)
                caption = '<div class="code-caption">{} &nbsp; <a href="{}">Copy code</a></div>'.format(
                    esc(segment.get("language") or "Code"), cmd_url("opencode_copy_response", args))
            out.append('<div class="code">{}{}</div>'.format(caption, esc(segment["text"])))
            code_index += 1
        else:
            out.append("<br>".join(esc(line) for line in segment["text"].splitlines()))
    return "<br>".join(out)


def activity_html(activity):
    status = activity.get("status", "completed")
    icon = {"completed": "✓", "error": "!", "running": "●"}.get(status, "○")
    cls = {"completed": "good", "error": "bad", "running": "accent"}.get(status, "muted")
    return '<div class="activity"><span class="{}">{}</span> {}</div>'.format(
        cls, icon, esc(activity.get("title") or activity.get("tool") or "Tool")
    )


DARK_COLORS = {'#f7f8fa': '#1b202b', '#202632': '#e1e7f1', '#335cc5': '#8eafff', '#dfe3ea': '#354052', '#657184': '#a0aec2', '#dce1e9': '#3c485e', '#ffffff': '#252e3e', '#445166': '#c3cfdf', '#208450': '#72cf9b', '#bd3546': '#ff929f', '#eaf0fc': '#283956', '#263a5b': '#dce6ff', '#f0c9ce': '#68414e', '#fff1f3': '#3b2933', '#a92c3c': '#ffb0bb', '#eef1f5': '#242d3c', '#26364a': '#dae3f3'}


def duration_text(seconds):
    seconds = max(0, int(seconds))
    return "{}:{:02d}".format(seconds // 60, seconds % 60)


def thinking_html(state, turn, index):
    reasoning = turn.get("reasoning") or ""
    if not reasoning:
        return ""
    opened = turn.get("thinking_open", False)
    label = "Thinking…" if turn.get("phase") == "thinking" else "Thinking"
    link = cmd_url("opencode_toggle_thinking", {"window_id": state["window_id"], "turn_index": index})
    text = '<div class="thinking-text">{}</div>'.format(message_html(reasoning)) if opened else ""
    return '<div class="thinking"><a href="{}">{} {} · {}</a>{}</div>'.format(
        link, "▾" if opened else "▸", label, "Hide" if opened else "Show", text,
    )


def progress_html(state, turn):
    phase = turn.get("phase")
    if not phase:
        return ""
    now = turn["ended_at"] if turn.get("ended_at") is not None else time.monotonic()
    elapsed = duration_text(now - turn.get("started_at", now))
    labels = {
        "preparing": "Preparing OpenCode", "sending": "Sending message",
        "waiting": "Waiting for model", "thinking": "Thinking…",
        "writing": "Writing response…", "tool": "Running tool",
        "retry": "Provider retry", "permission": "Waiting for your permission",
        "question": "Waiting for your answer", "reconnecting": "Connection lost · checking again",
        "interrupted": "No active run",
        "cancelling": "Cancelling…", "completed": "Completed",
        "cancelled": "Cancelled", "error": "Error",
    }
    terminal = phase in ("completed", "cancelled", "error", "interrupted")
    icon = {"completed": "✓", "cancelled": "■", "error": "!"}.get(phase, "●" if int(now) % 2 else "○")
    label = labels.get(phase, "Agent working")
    detail = turn.get("status_detail") or ""
    if detail:
        label += " · " + detail
    meta = "Elapsed " + elapsed
    if not terminal:
        checked = turn.get("checked_at")
        if checked:
            meta += " · Last server check {} ago".format(duration_text(now - checked))
        idle = now - turn.get("last_activity_at", turn.get("started_at", now))
        if idle >= 45 and phase not in ("permission", "question", "cancelling"):
            meta += "<br>No new activity for {}. {}".format(duration_text(idle),
                "The server still reports a busy session; you can keep waiting or press Stop."
                if turn.get("server_status") in ("busy", "retry") and phase != "reconnecting"
                else "The current server state has not been confirmed; checking again.")
    if turn.get("monitor_error"):
        meta += "<br>" + esc(turn["monitor_error"])
    return '<div class="working"><b>{} {}</b><div class="progress-meta">{}</div></div>'.format(icon, esc(label), meta)


def pending_html(state, turn):
    out = []
    wid = state["window_id"]
    for item in turn.get("permissions", []):
        links = ""
        for reply, label in (("once", "Allow once"), ("reject", "Reject")):
            links += '<a class="button" href="{}">{}</a> '.format(cmd_url(
                "opencode_permission_reply", {"window_id": wid, "request_id": item["id"], "reply": reply}), label)
        out.append('<div class="pending"><b>Permission required: {}</b><br>{}<div class="pending-actions">{}</div></div>'.format(
            esc(item.get("permission", "Tool")), esc(" · ".join(item.get("patterns") or [])), links))
    for item in turn.get("questions", []):
        content = "<br>".join(esc(q.get("question", "")) for q in item.get("questions", []))
        link = cmd_url("opencode_question_reply", {"window_id": wid, "request_id": item["id"]})
        skip = cmd_url("opencode_question_reply", {"window_id": wid, "request_id": item["id"], "reject": True})
        out.append('<div class="pending"><b>Agent needs an answer</b><br>{}<div class="pending-actions"><a class="button" href="{}">Answer</a> <a class="button" href="{}">Skip</a></div></div>'.format(content, link, skip))
    if turn.get("action_error"):
        out.append('<div class="error">{}</div>'.format(esc(turn["action_error"])))
    return "".join(out)


def apply_color_mode(state):
    composer = state.get("composer")
    if composer is not None and composer.is_valid():
        name = "OpenCode Prompt Dark" if state["color_mode"] == "dark" else "OpenCode Prompt"
        composer.settings().set("color_scheme", "Packages/OpenCodeSublime/{}.sublime-color-scheme".format(name))


def reset_changes(state):
    state["diff_request"] = state.get("diff_request", 0) + 1
    state.update(diff_open=False, diff_loading=False, diff_error="", diffs=[])


def session_context_current(state, generation, session_id, serial):
    return (_states.get(state["window_id"]) is state and state["generation"] == generation
        and state.get("session_id") == session_id and state["session_revision"] == serial)


def normalize_session_diff(item):
    if not isinstance(item, dict):
        return None
    filename = item.get("file") or item.get("path")
    if not isinstance(filename, str) or not filename:
        return None
    patch = item.get("patch")
    if not isinstance(patch, str) and isinstance(item.get("before"), str) and isinstance(item.get("after"), str):
        before, after = item["before"], item["after"]
        old_name = "/dev/null" if item.get("status") == "added" else "a/" + filename
        new_name = "/dev/null" if item.get("status") == "deleted" else "b/" + filename
        lines = difflib.unified_diff(before.splitlines(keepends=True), after.splitlines(keepends=True), fromfile=old_name, tofile=new_name)
        patch = "".join(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n" for line in lines)
    def count(value):
        return max(0, value) if isinstance(value, int) and not isinstance(value, bool) else 0
    return {"file": filename, "patch": patch if isinstance(patch, str) else None,
        "additions": count(item.get("additions")), "deletions": count(item.get("deletions")),
        "status": item.get("status") if item.get("status") in ("added", "deleted", "modified") else "modified"}


def changes_html(state):
    if not state.get("diff_open"):
        return ""
    wid = state["window_id"]
    out = ['<div class="changes"><b>Recorded changes in this session</b>']
    out.append(' &nbsp; <a href="{}">Refresh</a>'.format(cmd_url("opencode_show_changes", {"window_id": wid, "refresh": True})))
    if state.get("diff_loading"):
        out.append('<div class="muted">Loading changes…</div>')
    if state.get("diff_error"):
        out.append('<div class="error">{}</div>'.format(esc(state["diff_error"])))
    if not state.get("diff_loading") and not state.get("diff_error") and not state.get("diffs"):
        out.append('<div class="muted">No recorded file changes in this session.</div>')
    if state.get("diffs"):
        for index, item in enumerate(state["diffs"]):
            args = {"window_id": wid, "diff_index": index, "request": state["diff_request"], "session_id": state["session_id"]}
            out.append('<div class="change-file">{}<br><span class="muted">{} · +{} / −{}</span> &nbsp; <a href="{}">Diff</a> &nbsp; <a href="{}">Open</a></div>'.format(
                esc(item["file"]), esc(item["status"]), item["additions"], item["deletions"],
                cmd_url("opencode_view_change", args), cmd_url("opencode_view_change", dict(args, open_file=True))))
    out.append('</div>')
    return "".join(out)


def refresh_session_changes(state):
    session_id = state.get("session_id")
    if not session_id or state.get("session_loading"):
        return
    state["diff_request"] += 1
    request, generation, serial = state["diff_request"], state["generation"], state["session_revision"]
    state.update(diff_loading=True, diff_error="", diffs=[])
    render(state["window_id"])
    def worker():
        error, diffs = "", []
        try:
            result = request_json("GET", "/session/{}/diff".format(urllib.parse.quote(session_id, safe="")), timeout=15, directory=state["directory"])
            if not isinstance(result, list):
                raise RuntimeError("OpenCode returned an invalid changes list.")
            diffs = [diff for diff in (normalize_session_diff(item) for item in result) if diff is not None]
        except Exception as failure:
            error = "Could not load changes: " + str(failure)
        def apply():
            if not session_context_current(state, generation, session_id, serial) or state["diff_request"] != request:
                return
            state.update(diff_loading=False, diff_error=error, diffs=diffs)
            render(state["window_id"])
        sublime.set_timeout(apply)
    threading.Thread(target=worker, daemon=True).start()


def safe_project_path(directory, filename):
    if not directory or not filename:
        raise RuntimeError("Open a project folder first.")
    root = os.path.realpath(directory)
    path = os.path.realpath(os.path.join(root, filename))
    try:
        inside = os.path.commonpath([os.path.normcase(root), os.path.normcase(path)]) == os.path.normcase(root)
    except ValueError:
        inside = False
    if not inside:
        raise RuntimeError("This file is outside the current project. Use Diff to inspect the recorded changes.")
    return path


def build_html(state):
    wid = state["window_id"]
    status = (
        '<span class="good">● Connected</span>'
        if state["connected"]
        else '<span class="bad">● Offline</span>'
    )

    selectors = '<span class="select-gap">&nbsp;&nbsp;&nbsp;</span>'.join([
        '<span class="control"><a class="select" href="{}">{} ▾</a></span>'.format(
            cmd_url("opencode_choose_agent", {"window_id": wid}),
            esc(state["agent"].upper()),
        ),
        '<span class="control"><a class="select" href="{}" title="{}">{} ▾</a></span>'.format(
            cmd_url("opencode_choose_model", {"window_id": wid}),
            esc(current_model_label(state)),
            esc(current_model_label(state) if len(current_model_label(state)) <= 24 else current_model_label(state)[:23] + "…"),
        ),
        '<span class="control"><a class="select" href="{}">{} ▾</a></span>'.format(
            cmd_url("opencode_choose_color", {"window_id": wid}),
            "Dark" if state["color_mode"] == "dark" else "Light",
        ),
    ])
    actions = "".join([
        '<a class="button primary" href="{}">Send</a> '.format(
            cmd_url("opencode_send_prompt", {"window_id": wid})
        ),
        '<a class="button" href="{}">New</a> '.format(
            cmd_url("opencode_new_session", {"window_id": wid})
        ),
        (
            '<a class="button danger" href="{}">Stop</a> '.format(
                cmd_url("opencode_abort", {"window_id": wid})
            )
            if state["busy"] else ""
        ),
        '<a class="button" href="{}">Close</a>'.format(
            cmd_url("opencode_close_agent", {"window_id": wid})
        ),
    ])

    session_label = "Loading session…" if state.get("session_loading") else state.get("session_title") or "New chat"
    session_bar = '<div class="session-bar">Session: <a href="{}">{} ▾</a></div>'.format(
        cmd_url("opencode_choose_session", {"window_id": wid}), esc(session_label))
    if state.get("session_id"):
        session_bar += '<div class="session-links"><a href="{}">Rename</a> &nbsp;&nbsp; <a href="{}">{} Changes</a></div>'.format(
            cmd_url("opencode_rename_session", {"window_id": wid}),
            cmd_url("opencode_show_changes", {"window_id": wid}), "▾" if state.get("diff_open") else "▸")
    session_bar += changes_html(state)
    if state.get("session_error"):
        session_bar += '<div class="error">{}</div>'.format(esc(state["session_error"]))

    body = []
    if not state["turns"]:
        body.append(
            '<div class="welcome"><b>Agent ready</b><br>'
            '<span class="muted">Write below and press Ctrl+Enter.</span></div>'
        )

    for turn_index, turn in enumerate(state["turns"]):
        activities = "".join(activity_html(a) for a in turn.get("activities", []))
        attached = turn.get("attachments") or []
        context = '<div class="context-files">Context: {}</div>'.format(
            esc(" · ".join(part["filename"] for part in attached))
        ) if attached else ""

        response = thinking_html(state, turn, turn_index)
        if turn.get("response"):
            copy_args = {"window_id": wid, "turn_index": turn_index, "generation": state["generation"], "message_id": turn.get("message_id")}
            response += '<div class="message-actions"><a href="{}">Copy response</a></div><div class="assistant">{}</div>'.format(
                cmd_url("opencode_copy_response", copy_args), message_html(turn["response"], copy_args))
        response += progress_html(state, turn)
        response += pending_html(state, turn)
        if turn.get("error"):
            response += '<div class="error">{}</div>'.format(esc(turn["error"]))

        body.append(
            '<div class="turn">'
            '<div class="caption">YOU <span class="timestamp">{}</span></div>'
            '<div class="user">{}</div>'
            '{}'
            '<div class="assistant-title"><b>OPENCODE</b> '
            '<span class="muted">{} · {}</span> <span class="timestamp">{}</span></div>'
            '{}{}'
            '</div>'.format(
                esc(message_time(turn.get("sent_at"))),
                message_html(turn["prompt"]),
                context,
                esc(turn["agent"].upper()),
                esc(turn["model"]),
                esc(message_time(turn.get("received_at"))),
                activities,
                response,
            )
        )

    if state.get("connecting"):
        status = '<span class="accent">● Connecting…</span>'
    server = "OpenCode {}".format(esc(state["version"])) if state["version"] else esc(state["error"])

    if state.get("directory"):
        server += " · " + esc(os.path.basename(state["directory"]))
    markup = """
<html><body id="opencode-agent"><style>
html {{ background-color: #f7f8fa; color: #202632; }}
body {{ margin: 18px; background-color: #f7f8fa; color: #202632; font-family: "Segoe UI", sans-serif; font-size: 14px; line-height: 1.5em; }}
a {{ text-decoration: none; color: #335cc5; }}
.header {{ padding-bottom: 16px; margin-bottom: 20px; border-bottom: 1px solid #dfe3ea; }}
.brand {{ font-size: 21px; font-weight: bold; color: #202632; }}
.meta {{ margin-top: 5px; font-size: 12px; color: #657184; }}
.toolbar {{ display: block; margin-top: 12px; padding-top: 8px; padding-bottom: 8px; line-height: 32px; }}
.control {{ display: inline-block; padding-right: 8px; }}
.select-gap {{ display: inline; font-size: 12px; }}
.toolbar-gap {{ display: block; padding-top: 6px; padding-bottom: 6px; font-size: 12px; line-height: 16px; }}
.actions {{ display: block; padding-top: 8px; padding-bottom: 8px; line-height: 32px; }}
.select,.button {{ display: inline-block; padding: 7px 10px; margin-right: 12px; margin-bottom: 6px; line-height: 20px; white-space: nowrap; border-radius: 6px; border: 1px solid #dce1e9; background-color: #ffffff; color: #445166; font-size: 12px; }}
.select {{ color: #335cc5; font-weight: bold; }}
.composer-label {{ margin-top: 24px; padding-top: 18px; padding-bottom: 10px; border-top: 1px solid #dfe3ea; color: #657184; font-size: 12px; }}
.primary {{ background-color: #335cc5; border-color: #335cc5; color: #ffffff; }}
.good {{ color: #208450; }}
.bad,.danger {{ color: #bd3546; }}
.accent {{ color: #335cc5; }}
.muted {{ color: #657184; }}
.welcome {{ margin-top: 26px; padding: 4px 0px; color: #202632; }}
.welcome b {{ font-size: 18px; }}
.turn {{ margin-bottom: 24px; }}
.caption,.assistant-title {{ margin-bottom: 8px; font-size: 11px; color: #657184; }}
.timestamp {{ padding-left: 8px; font-size: 11px; color: #657184; }}
.context-files {{ margin-top: 6px; font-size: 11px; color: #657184; }}
.assistant-title {{ margin-top: 18px; }}
.assistant-title b {{ color: #335cc5; }}
.user {{ padding: 12px 14px; border-radius: 8px; background-color: #eaf0fc; color: #263a5b; }}
.assistant {{ padding: 0px 2px; color: #202632; }}
.working {{ padding: 10px 0px; color: #335cc5; }}
.thinking {{ margin-top: 10px; margin-bottom: 12px; padding: 10px 12px; border: 1px solid #dfe3ea; border-radius: 6px; background-color: #eef1f5; }}
.thinking-text {{ padding-top: 10px; color: #445166; font-size: 12px; }}
.progress-meta {{ padding-top: 4px; font-size: 11px; color: #657184; }}
.pending {{ padding: 12px; margin-top: 10px; margin-bottom: 10px; border: 1px solid #dfe3ea; border-radius: 6px; background-color: #eaf0fc; }}
.pending-actions {{ padding-top: 8px; line-height: 32px; }}
.session-bar {{ padding-top: 14px; font-size: 12px; color: #657184; }}
.session-links {{ padding-top: 6px; line-height: 24px; }}
.message-actions {{ padding-bottom: 8px; font-size: 11px; }}
.code-caption {{ margin-bottom: 8px; white-space: normal; font-size: 11px; font-family: "Segoe UI", sans-serif; color: #657184; }}
.changes {{ margin-top: 12px; padding: 10px 12px; border: 1px solid #dfe3ea; border-radius: 6px; background-color: #eef1f5; }}
.change-file {{ padding-top: 8px; padding-bottom: 8px; border-bottom: 1px solid #dfe3ea; }}
.activity {{ padding: 6px 0px; margin-bottom: 3px; color: #445166; font-size: 12px; }}
.error {{ padding: 12px; border: 1px solid #f0c9ce; border-radius: 6px; background-color: #fff1f3; color: #a92c3c; }}
.code {{ padding: 12px; margin-top: 8px; margin-bottom: 8px; border: 1px solid #dfe3ea; border-radius: 6px; background-color: #eef1f5; color: #26364a; font-family: Consolas, monospace; white-space: pre-wrap; }}
</style>
<div class="header"><div class="brand">OpenCode Agent</div><div class="meta">{} &nbsp; {}</div><div class="toolbar">{}</div><div class="toolbar-gap"><br></div><div class="actions">{}</div>{}</div>
{}
<div class="composer-label"><b>Your message</b> · Ctrl+Enter to send</div>
</body></html>
""".format(status, server, selectors, actions, session_bar, "".join(body))
    if state["color_mode"] == "dark":
        # Replace tokens simultaneously so a new color cannot be replaced again.
        import re
        start, end = markup.index("<style>"), markup.index("</style>")
        css = re.sub(r"#[0-9a-f]{6}", lambda m: DARK_COLORS.get(m.group(), m.group()), markup[start:end])
        css = css.replace("color: #252e3e; }", "color: #17233c; }")
        markup = markup[:start] + css + markup[end:]
    return markup


def render(window_id):
    state = _states.get(window_id)
    if not state:
        return
    composer = state.get("composer")
    phantoms = state.get("phantoms")
    if composer is not None and composer.is_valid() and phantoms is not None:
        apply_color_mode(state)
        # The protected first newline anchors the HTML above the editable draft.
        # Edits start at point 1, so the phantom never moves when the user types.
        phantoms.update([
            sublime.Phantom(sublime.Region(0, 0), build_html(state), sublime.LAYOUT_BLOCK)
        ])


def draft_text(view):
    return view.substr(sublime.Region(1, view.size()))


def open_ui(window):
    state = state_for(window)
    composer = state["composer"]
    if composer is not None and composer.is_valid():
        window.focus_view(composer)
        return state

    state["old_layout"] = window.layout()
    window.set_layout(LAYOUT)
    window.focus_group(1)
    composer = window.new_file()
    composer.set_scratch(True)
    composer.set_name("OpenCode Agent")
    settings = composer.settings()
    for key, value in {
        "opencode_composer": True,
        "word_wrap": True,
        "line_numbers": False,
        "gutter": False,
        "spell_check": False,
        "font_face": "Segoe UI",
        "font_size": 11,
        "draw_indent_guides": False,
        "highlight_line": False,
        "auto_complete": False,
        "draw_centered": False,
        "margin": 16,
        "scroll_past_end": False,
    }.items():
        settings.set(key, value)
    composer.assign_syntax("Packages/Text/Plain text.tmLanguage")
    composer.set_status("opencode", "Ctrl+Enter to send · Enter for a new line")
    window.set_view_index(composer, 1, 0)
    state["composer"] = composer
    composer.run_command("opencode_replace_text", {"text": state.get("saved_draft", "")})
    state["phantoms"] = sublime.PhantomSet(composer, "opencode_agent")
    apply_color_mode(state)
    render(window.id())
    window.focus_view(composer)
    refresh_files(window.id())
    return state


def load_metadata(window_id):
    state = _states.get(window_id)
    if not state:
        return
    directory = state["directory"]
    expected_state = state
    try:
        health = ensure_server(directory)
        agents = parse_agents(request_json("GET", "/agent", timeout=10, directory=directory))
        models = parse_models(request_json("GET", "/provider", timeout=20, directory=directory))
        config = request_json("GET", "/config", timeout=10, directory=directory) or {}

        def apply():
            state = _states.get(window_id)
            if state is not expected_state:
                return
            state["connecting"] = False
            state["connected"] = bool(health.get("healthy"))
            state["version"] = health.get("version") or ""
            state["error"] = ""
            state["agents"] = agents
            state["models"] = models
            state["config_model"] = config.get("model")

            default_agent = config.get("default_agent") or "build"
            names = {a["name"] for a in agents}
            if state["preferred_agent"] in names:
                state["agent"] = state["preferred_agent"]
            elif default_agent in names:
                state["agent"] = default_agent
            elif "build" in names:
                state["agent"] = "build"
            elif agents:
                state["agent"] = agents[0]["name"]
            selected = state["selected_model"]
            if selected:
                match = next((model for model in models if (
                    model["provider_id"], model["model_id"]
                ) == (selected["provider_id"], selected["model_id"])), None)
                state["selected_model"] = match
                if match is None:
                    sublime.status_message("OpenCode: saved model unavailable; using Auto.")
            render(window_id)
            if state.get("restore_pending") and not state.get("session_loading"):
                load_session(state, state["restore_id"])

        sublime.set_timeout(apply)
    except Exception as error:
        def fail(message=str(error)):
            state = _states.get(window_id)
            if state is expected_state:
                state["connecting"] = False
                state["connected"] = False
                state["error"] = message
                render(window_id)
        sublime.set_timeout(fail)


def extract_text(result):
    return "\n\n".join(
        part.get("text", "")
        for part in (result or {}).get("parts", [])
        if part.get("type") == "text" and part.get("text")
    )


def extract_activities(result):
    activities = []
    for part in (result or {}).get("parts", []):
        if part.get("type") == "tool":
            tool_state = part.get("state") or {}
            activities.append({
                "tool": part.get("tool") or "tool",
                "title": tool_state.get("title") or part.get("tool") or "Tool",
                "status": tool_state.get("status") or "completed",
            })
        elif part.get("type") == "patch":
            files = part.get("files") or []
            activities.append({
                "tool": "patch",
                "title": "Changed {} file{}".format(len(files), "" if len(files) == 1 else "s"),
                "status": "completed",
            })
    return activities


def submit(window, prompt):
    state = state_for(window)
    prompt = prompt.strip()
    if not prompt:
        return
    if state.get("session_loading") or state.get("restore_pending") or state.get("session_blocked"):
        if state.get("session_blocked") and not state.get("session_loading"):
            load_session(state, state["session_id"])
        if state["connected"] and state.get("restore_pending") and not state.get("session_loading"):
            load_session(state, state["restore_id"])
        elif not state["connected"]:
            start_connection(window)
        sublime.status_message("OpenCode: loading the previous session. Your draft is kept.")
        return
    if not state["connected"]:
        start_connection(window)
        sublime.status_message("OpenCode: reconnecting. Your draft is kept; send when Connected.")
        return
    if state["busy"]:
        sublime.status_message("OpenCode is already working.")
        return

    selected = state["selected_model"]
    turn = {
        "prompt": prompt,
        "response": "",
        "error": "",
        "activities": [],
        "agent": state["agent"],
        "model": selected["label"] if selected else current_model_label(state),
        "attachments": context_parts(state, prompt),
        "sent_at": datetime.now().astimezone().isoformat(),
        "received_at": None,
        "message_id": new_message_id(),
        "reasoning": "",
        "thinking_open": False,
        "phase": "preparing",
        "status_detail": "",
        "started_at": time.monotonic(),
        "last_activity_at": time.monotonic(),
        "stop_event": threading.Event(),
        "abort_requested": False,
        "permissions": [],
        "questions": [],
    }
    save_preferences(state)
    state["turns"].append(turn)
    state["busy"] = True
    state["active_turn"] = turn
    render(window.id())

    if state["composer"] is not None:
        state["composer"].run_command("opencode_replace_text", {"text": ""})

    generation = state["generation"]
    agent = state["agent"]
    model = dict(selected) if selected else None
    run_heartbeat(window.id(), state, generation, turn)

    threading.Thread(
        target=send_worker,
        args=(window.id(), generation, turn, prompt, agent, model),
        daemon=True,
    ).start()


def new_message_id():
    # Same sortable timestamp prefix as OpenCode's ascending message IDs.
    stamp = (int(time.time() * 1000) * 0x1000) & ((1 << 48) - 1)
    return "msg_{:012x}{}".format(stamp, uuid.uuid4().hex[:14])


def run_current(window_id, expected, generation, turn):
    state = _states.get(window_id)
    return state is expected and state is not None and state["generation"] == generation and state.get("active_turn") is turn and state["busy"]


def run_heartbeat(window_id, expected, generation, turn):
    def tick():
        if not run_current(window_id, expected, generation, turn):
            return
        render(window_id)
        sublime.set_timeout(tick, 1000)
    sublime.set_timeout(tick, 1000)


def publish_run(window_id, expected, generation, turn, snapshot, terminal=False):
    def apply():
        if not run_current(window_id, expected, generation, turn):
            return
        turn.update(snapshot)
        if "server_reachable" in snapshot:
            expected["connected"] = snapshot["server_reachable"]
        info = snapshot.get("model_info") or {}
        if info.get("providerID") and info.get("modelID"):
            expected["effective_model"] = {"provider_id": info["providerID"], "model_id": info["modelID"]}
        if terminal:
            turn["ended_at"] = time.monotonic()
            turn["received_at"] = datetime.now().astimezone().isoformat()
            turn["permissions"], turn["questions"] = [], []
            turn["stop_event"].set()
            expected["busy"] = False
            refresh_files(window_id)
            if expected.get("diff_open"):
                refresh_session_changes(expected)
        render(window_id)
    sublime.set_timeout(apply)


def backend_error(error):
    if not error:
        return ""
    if isinstance(error, str):
        return error
    data = error.get("data") or {}
    return "{}: {}".format(error.get("name") or error.get("_tag") or "OpenCode error",
        data.get("message") or error.get("message") or json.dumps(data, ensure_ascii=False))


def run_snapshot(messages, message_id, status, cache):
    # parentID excludes earlier turns and messages submitted by other clients.
    for message in messages:
        info = message.get("info") or {}
        if info.get("role") == "assistant" and info.get("parentID") == message_id:
            cache[info["id"]] = message
    ordered = sorted(cache.values(), key=lambda m: ((m.get("info") or {}).get("time", {}).get("created", 0), (m.get("info") or {}).get("id", "")))
    response, reasoning, activities = [], [], []
    for message in ordered:
        text = extract_text(message)
        if text:
            response.append(text)
        reasoning.extend(part["text"] for part in message.get("parts", []) if part.get("type") == "reasoning" and part.get("text"))
        activities.extend(extract_activities(message))
    last = ordered[-1] if ordered else {}
    info = last.get("info") or {}
    parts = last.get("parts") or []
    phase, detail = "waiting", ""
    if status.get("type") == "retry":
        phase, detail = "retry", "Attempt {} · {}".format(status.get("attempt", "?"), status.get("message", ""))
    elif any(a["status"] in ("running", "pending") for a in activities):
        phase = "tool"
        detail = next(a["title"] for a in reversed(activities) if a["status"] in ("running", "pending"))
    elif parts and parts[-1].get("type") == "reasoning" and not (parts[-1].get("time") or {}).get("end"):
        phase = "thinking"
    elif response:
        phase = "writing"
    return {
        "response": "\n\n".join(response), "reasoning": "\n\n".join(reasoning), "activities": activities,
        "phase": phase, "status_detail": detail, "server_status": status.get("type", "idle"),
        "error": backend_error(info.get("error")), "model_info": info,
    }


def pending_requests(path, session_id, directory, unsupported):
    if path in unsupported:
        return []
    try:
        items = request_json("GET", path, timeout=3, directory=directory) or []
    except RuntimeError as error:
        if str(error).startswith("HTTP 404"):
            unsupported.add(path)
            return []
        raise
    return [item for item in items if item.get("sessionID") == session_id]


def monitor_run(window_id, expected, generation, turn, session_id, initial_messages=None):
    directory = expected["directory"]
    cache, unsupported = {}, set()
    if initial_messages:
        run_snapshot(initial_messages, turn["message_id"], {"type": "busy"}, cache)
    previous, last_activity, idle_since = None, time.monotonic(), None
    while run_current(window_id, expected, generation, turn) and not turn["stop_event"].is_set():
        try:
            if turn.get("abort_requested"):
                publish_run(window_id, expected, generation, turn, {"phase": "cancelling", "status_detail": ""})
                stopped = request_json("POST", "/session/{}/abort".format(session_id), timeout=10, directory=directory)
                if stopped is not True:
                    raise RuntimeError("OpenCode did not confirm cancellation.")
                publish_run(window_id, expected, generation, turn, {"phase": "cancelled", "status_detail": "", "monitor_error": ""}, terminal=True)
                return
            messages = request_json("GET", "/session/{}/message?limit=100".format(session_id), timeout=3, directory=directory) or []
            statuses = request_json("GET", "/session/status", timeout=3, directory=directory) or {}
            status = statuses.get(session_id) or {"type": "idle"}
            snapshot = run_snapshot(messages, turn["message_id"], status, cache)
            snapshot["permissions"] = pending_requests("/permission", session_id, directory, unsupported)
            snapshot["questions"] = pending_requests("/question", session_id, directory, unsupported)
            now = time.monotonic()
            fingerprint = json.dumps({key: snapshot[key] for key in ("response", "reasoning", "activities", "permissions", "questions", "error")}, sort_keys=True)
            if fingerprint != previous:
                previous, last_activity = fingerprint, now
            snapshot.update(checked_at=now, last_activity_at=last_activity, monitor_error="", server_reachable=True)
            if snapshot["permissions"]:
                snapshot.update(phase="permission", status_detail="")
            elif snapshot["questions"]:
                snapshot.update(phase="question", status_detail="")
            pending = snapshot["permissions"] or snapshot["questions"]
            info = snapshot["model_info"]
            if status.get("type") == "idle" and not pending:
                if snapshot["error"] or (info.get("time") or {}).get("completed"):
                    error_info = info.get("error") or {}
                    aborted = (error_info.get("name") or error_info.get("_tag")) == "MessageAbortedError"
                    snapshot.update(phase="cancelled" if aborted else "error" if snapshot["error"] else "completed", status_detail="")
                    if aborted:
                        snapshot["error"] = ""
                    publish_run(window_id, expected, generation, turn, snapshot, terminal=True)
                    return
                idle_since = idle_since or now
                if now - idle_since >= 30:
                    snapshot.update(phase="error", status_detail="", error="OpenCode reports an idle session without a completed answer. The run is no longer reported as active.")
                    publish_run(window_id, expected, generation, turn, snapshot, terminal=True)
                    return
            else:
                idle_since = None
            publish_run(window_id, expected, generation, turn, snapshot)
        except Exception as error:
            if turn.get("abort_requested"):
                turn["abort_requested"] = False
                publish_run(window_id, expected, generation, turn, {"action_error": "Stop failed: {}. Cancellation is not confirmed; monitoring continues.".format(error)})
            publish_run(window_id, expected, generation, turn, {"phase": "reconnecting", "status_detail": "", "monitor_error": str(error), "server_reachable": False})
        if turn["stop_event"].wait(1):
            return


def send_worker(window_id, generation, turn, prompt, agent, model):
    expected = _states.get(window_id)
    try:
        if not expected or expected["generation"] != generation:
            return
        directory = expected["directory"]
        ensure_server(directory)
        if _states.get(window_id) is not expected:
            return
        session_id = expected["session_id"]
        if not session_id:
            title = re.sub(r"\s+", " ", prompt).strip()[:80] or "Sublime Text"
            session = request_json("POST", "/session", {"title": title}, timeout=15, directory=directory)
            session_id = session["id"]
            if _states.get(window_id) is not expected or expected["generation"] != generation:
                return
            expected["session_id"] = session_id
            expected["session_title"] = session.get("title") or title
            # Persist the ID immediately, before the long-running agent work.
            remember_project_session(expected)
        if turn.get("abort_requested"):
            publish_run(window_id, expected, generation, turn, {"phase": "cancelled", "status_detail": ""}, terminal=True)
            return
        body = {"messageID": turn["message_id"], "agent": agent,
            "parts": [{"type": "text", "text": prompt}] + turn.get("attachments", [])}
        if model:
            body["model"] = {"providerID": model["provider_id"], "modelID": model["model_id"]}
        publish_run(window_id, expected, generation, turn, {"phase": "sending"})
        try:
            request_json("POST", "/session/{}/prompt_async".format(session_id), body, timeout=20, directory=directory)
        except (OpenCodeConnectionError, TimeoutError):
            # An interrupted HTTP acknowledgement does not prove the prompt
            # failed. Look for this exact message; never resend it automatically.
            publish_run(window_id, expected, generation, turn, {"phase": "reconnecting", "monitor_error": "Message delivery not confirmed; checking the session."})
        monitor_run(window_id, expected, generation, turn, session_id)
    except Exception as error:
        publish_run(window_id, expected, generation, turn, {"phase": "error", "error": "{}: {}".format(type(error).__name__, error)}, terminal=True)


class OpencodeReplaceTextCommand(sublime_plugin.TextCommand):
    def run(self, edit, text=""):
        self.view.replace(edit, sublime.Region(0, self.view.size()), "\n" + text)
        self.view.sel().clear()
        self.view.sel().add(sublime.Region(1, 1))


def epoch_timestamp(milliseconds):
    try:
        return datetime.fromtimestamp(milliseconds / 1000).astimezone().isoformat() if milliseconds else None
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def history_turns(messages, state):
    ordered = sorted(messages, key=lambda m: ((m.get("info") or {}).get("time", {}).get("created", 0), (m.get("info") or {}).get("id", "")))
    turns = []
    for message in ordered:
        info = message.get("info") or {}
        if info.get("role") != "user":
            continue
        parts = message.get("parts") or []
        model = info.get("model") or {}
        prompt = "\n\n".join(part.get("text", "") for part in parts if part.get("type") == "text" and not part.get("synthetic") and not part.get("ignored"))
        attachments = []
        for part in parts:
            if part.get("type") == "file":
                attached = dict(part)
                attached["filename"] = part.get("filename") or os.path.basename(urllib.parse.unquote(urllib.parse.urlsplit(part.get("url", "")).path)) or "File"
                attachments.append(attached)
        turn = {
            "prompt": prompt or "(Attached files)", "message_id": info["id"],
            "agent": info.get("agent") or state["agent"],
            "model": "{}/{}".format(model["providerID"], model["modelID"]) if model.get("providerID") and model.get("modelID") else "Auto",
            "attachments": attachments, "sent_at": epoch_timestamp((info.get("time") or {}).get("created")),
            "received_at": None, "thinking_open": False, "stop_event": threading.Event(),
            "abort_requested": False, "permissions": [], "questions": [],
        }
        turns.append(turn)
    assistants = [message for message in ordered if (message.get("info") or {}).get("role") == "assistant"]
    for turn in turns:
        snapshot = run_snapshot(assistants, turn["message_id"], {"type": "idle"}, {})
        info = snapshot["model_info"]
        completed = (info.get("time") or {}).get("completed")
        error = info.get("error") or {}
        aborted = (error.get("name") or error.get("_tag")) == "MessageAbortedError"
        snapshot.update(phase="cancelled" if aborted else "error" if snapshot["error"] else "completed" if completed else "interrupted", status_detail="" if completed or error else "No completed answer was recorded")
        if aborted:
            snapshot["error"] = ""
        turn.update(snapshot)
        turn["received_at"] = epoch_timestamp(completed)
        # Render historical duration without a wall clock that keeps ticking.
        sent = epoch_timestamp_ms(turn.get("sent_at"))
        turn["started_at"] = 0
        turn["ended_at"] = max(0, (completed - sent) / 1000) if completed and sent else 0
    return turns


def epoch_timestamp_ms(timestamp):
    try:
        return datetime.fromisoformat(timestamp).timestamp() * 1000 if timestamp else None
    except (ValueError, TypeError):
        return None


def session_request_current(state, generation, serial):
    return _states.get(state["window_id"]) is state and state["generation"] == generation and state["session_request"] == serial


def load_session(state, session_id):
    if state["busy"] or state.get("session_loading"):
        return
    state["session_request"] += 1
    state["session_revision"] += 1
    serial, generation = state["session_request"], state["generation"]
    state.update(session_loading=True, session_error="", rename_inflight=False)
    reset_changes(state)
    render(state["window_id"])
    def worker():
        try:
            ensure_server(state["directory"])
            path = "/session/{}".format(urllib.parse.quote(session_id, safe=""))
            session = request_json("GET", path, timeout=10, directory=state["directory"])
            if project_key(session.get("directory")) != project_key(state["directory"]):
                raise RuntimeError("This session belongs to another project. Choose a session from this project or press New.")
            messages = request_json("GET", path + "/message", timeout=20, directory=state["directory"]) or []
            # Reverted messages are retained by OpenCode but no longer context.
            revert_id = (session.get("revert") or {}).get("messageID")
            if revert_id:
                messages = [m for m in messages if (m.get("info") or {}).get("id", "") < revert_id]
            turns = history_turns(messages, state)
            statuses = request_json("GET", "/session/status", timeout=5, directory=state["directory"]) or {}
            status = statuses.get(session_id) or {"type": "idle"}
            def apply():
                if not session_request_current(state, generation, serial):
                    return
                state.update(session_id=session_id, session_title=session.get("title") or "Untitled session", turns=turns,
                    session_loading=False, restore_pending=False, restore_id=None, session_error="", session_blocked=False, connected=True, active_turn=None)
                reset_changes(state)
                state["effective_model"] = None
                if turns and turns[-1].get("model_info"):
                    info = turns[-1]["model_info"]
                    if info.get("providerID") and info.get("modelID"):
                        state["effective_model"] = {"provider_id": info["providerID"], "model_id": info["modelID"]}
                else:
                    state["effective_model"] = None
                remember_project_session(state)
                if status.get("type") in ("busy", "retry") and turns:
                    turn = turns[-1]
                    sent = epoch_timestamp_ms(turn.get("sent_at"))
                    now = time.monotonic()
                    turn.update(phase="waiting", status_detail="Reattached to active session", ended_at=None,
                        started_at=now - max(0, time.time() - sent / 1000) if sent else now, last_activity_at=now)
                    state.update(busy=True, active_turn=turn)
                    run_heartbeat(state["window_id"], state, generation, turn)
                    threading.Thread(target=monitor_run, args=(state["window_id"], state, generation, turn, session_id, messages), daemon=True).start()
                elif status.get("type") in ("busy", "retry"):
                    state["session_blocked"] = True
                    state["session_error"] = "OpenCode reports an active session without readable messages. Reopen it from Sessions before sending."
                render(state["window_id"])
            sublime.set_timeout(apply)
        except Exception as error:
            message = str(error)
            missing = message.startswith("HTTP 404")
            def fail():
                if not session_request_current(state, generation, serial):
                    return
                state["session_loading"] = False
                state["session_error"] = "Session unavailable: {}. Use Sessions to retry or choose another; New starts a separate chat.".format(message)
                if missing and state.get("restore_id") == session_id:
                    state.update(restore_pending=False, restore_id=None, session_title="New chat")
                    remember_project_session(state)
                render(state["window_id"])
            sublime.set_timeout(fail)
    threading.Thread(target=worker, daemon=True).start()


class OpencodeChooseSessionCommand(sublime_plugin.WindowCommand):
    def run(self, window_id=None):
        state = _states.get(window_id or self.window.id())
        if not state:
            return
        if state["busy"] or state.get("session_loading"):
            sublime.status_message("OpenCode: wait for loading or stop the current run before switching sessions.")
            return
        if not state["directory"]:
            sublime.status_message("OpenCode: open a project folder to list its sessions.")
            return
        state["session_request"] += 1
        serial, generation = state["session_request"], state["generation"]
        state.update(session_loading=True, session_error="")
        render(state["window_id"])
        def worker():
            try:
                ensure_server(state["directory"])
                sessions = request_json("GET", "/session?roots=true", timeout=15, directory=state["directory"]) or []
                sessions = [s for s in sessions if not s.get("parentID") and not (s.get("time") or {}).get("archived") and project_key(s.get("directory")) == project_key(state["directory"])]
                sessions.sort(key=lambda s: (s.get("time") or {}).get("updated", 0), reverse=True)
                def show():
                    if not session_request_current(state, generation, serial):
                        return
                    state["session_loading"] = False
                    window = get_window(state["window_id"])
                    if not window:
                        return
                    items = [["New chat", "Create a separate conversation in this project"]]
                    for session in sessions:
                        updated = epoch_timestamp((session.get("time") or {}).get("updated"))
                        date = datetime.fromisoformat(updated).strftime("%Y-%m-%d %H:%M") if updated else ""
                        items.append([("● " if session["id"] == state["session_id"] else "") + (session.get("title") or "Untitled session"), date + " · " + session["id"]])
                    def done(index):
                        if index < 0 or not session_request_current(state, generation, serial):
                            return
                        if index == 0:
                            window.run_command("opencode_new_session", {"window_id": state["window_id"]})
                        elif index <= len(sessions):
                            load_session(state, sessions[index - 1]["id"])
                    render(state["window_id"])
                    window.show_quick_panel(items, done)
                sublime.set_timeout(show)
            except Exception as error:
                message = str(error)
                def fail():
                    if session_request_current(state, generation, serial):
                        state.update(session_loading=False, session_error="Could not list sessions: " + message)
                        render(state["window_id"])
                sublime.set_timeout(fail)
        threading.Thread(target=worker, daemon=True).start()


def start_connection(window):
    state = state_for(window)
    if state["connecting"]:
        return
    state["connecting"] = True
    render(window.id())
    threading.Thread(target=load_metadata, args=(window.id(),), daemon=True).start()


class OpencodeOpenAgentCommand(sublime_plugin.WindowCommand):
    def run(self):
        state = open_ui(self.window)
        render(self.window.id())
        if not state["connected"]:
            start_connection(self.window)
        elif state.get("restore_pending") and not state.get("session_loading"):
            load_session(state, state["restore_id"])


class OpencodeSubmitComposerCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        if not self.view.settings().get("opencode_composer"):
            return
        window = self.view.window()
        if window:
            submit(window, draft_text(self.view))


class OpencodeSendPromptCommand(sublime_plugin.WindowCommand):
    def run(self, window_id=None):
        window = get_window(window_id or self.window.id())
        state = _states.get(window.id()) if window else None
        composer = state.get("composer") if state else None
        if composer is not None and composer.is_valid():
            submit(window, draft_text(composer))


class OpencodeChooseColorCommand(sublime_plugin.WindowCommand):
    def run(self, window_id=None):
        state = _states.get(window_id or self.window.id())
        if not state:
            return

        def done(index):
            if index not in (0, 1) or _states.get(state["window_id"]) is not state:
                return
            state["color_mode"] = ("light", "dark")[index]
            save_preferences(state)
            render(state["window_id"])

        self.window.show_quick_panel(["Light", "Dark"], done)


class OpencodeChooseAgentCommand(sublime_plugin.WindowCommand):
    def run(self, window_id=None):
        state = _states.get(window_id or self.window.id())
        if not state or not state["agents"]:
            sublime.status_message("OpenCode: no selectable agents found.")
            return

        agents = state["agents"]
        items = [
            [
                ("✓ " if a["name"] == state["agent"] else "") + a["name"].upper(),
                a["description"],
            ]
            for a in agents
        ]

        def done(index):
            if 0 <= index < len(agents) and _states.get(state["window_id"]) is state:
                state["agent"] = agents[index]["name"]
                state["preferred_agent"] = state["agent"]
                state["effective_model"] = None
                save_preferences(state)
                render(state["window_id"])

        self.window.show_quick_panel(items, done)


class OpencodeChooseModelCommand(sublime_plugin.WindowCommand):
    def run(self, window_id=None):
        state = _states.get(window_id or self.window.id())
        if not state:
            return

        models = state["models"]
        items = [["Auto (OpenCode)", "Use the model selected by OpenCode / the active agent"]]

        for model in models:
            selected = state["selected_model"]
            mark = "✓ " if selected and (
                selected["provider_id"], selected["model_id"]
            ) == (model["provider_id"], model["model_id"]) else ""
            items.append([
                mark + model["model_name"],
                model["provider_name"] + " · " + model["model_id"],
            ])

        def done(index):
            if index < 0 or index > len(models) or _states.get(state["window_id"]) is not state:
                return
            state["selected_model"] = None if index == 0 else models[index - 1]
            state["effective_model"] = None
            save_preferences(state)
            render(state["window_id"])

        self.window.show_quick_panel(items, done)


class OpencodeNewSessionCommand(sublime_plugin.WindowCommand):
    def run(self, window_id=None):
        state = _states.get(window_id or self.window.id())
        if not state:
            return
        if state["busy"]:
            sublime.status_message("OpenCode: stop the current run first.")
            return
        state["generation"] += 1
        state["session_request"] += 1
        state["session_id"] = None
        state["session_revision"] += 1
        state.update(session_title="New chat", restore_pending=False, restore_id=None, session_loading=False, session_error="", session_blocked=False, rename_inflight=False)
        state["turns"] = []
        state["active_turn"] = None
        state["effective_model"] = None
        reset_changes(state)
        remember_project_session(state)
        render(state["window_id"])


class OpencodeCopyResponseCommand(sublime_plugin.WindowCommand):
    def run(self, turn_index, generation, message_id=None, block_index=None, window_id=None):
        state = _states.get(window_id or self.window.id())
        if not state or state["generation"] != generation or not 0 <= turn_index < len(state["turns"]):
            return
        turn = state["turns"][turn_index]
        if turn.get("message_id") != message_id:
            return
        text = turn.get("response") or ""
        if block_index is not None:
            blocks = [segment["text"] for segment in markdown_segments(text) if segment["type"] == "code"]
            if not 0 <= block_index < len(blocks):
                return
            text = blocks[block_index]
        sublime.set_clipboard(text)
        sublime.status_message("OpenCode: {} copied.".format("code" if block_index is not None else "response"))


class OpencodeRenameSessionCommand(sublime_plugin.WindowCommand):
    def run(self, window_id=None):
        state = _states.get(window_id or self.window.id())
        if not state or not state.get("session_id") or state.get("session_loading") or state.get("rename_inflight"):
            sublime.status_message("OpenCode: open a session before renaming it.")
            return
        generation, session_id, serial = state["generation"], state["session_id"], state["session_revision"]
        def done(title):
            if not session_context_current(state, generation, session_id, serial) or state.get("rename_inflight"):
                return
            title = title.strip()
            if not title or title == state.get("session_title"):
                return
            state.update(rename_inflight=True, session_error="")
            render(state["window_id"])
            def worker():
                error, renamed = "", None
                try:
                    renamed = request_json("PATCH", "/session/{}".format(urllib.parse.quote(session_id, safe="")), {"title": title}, timeout=15, directory=state["directory"])
                    if not isinstance(renamed, dict) or not isinstance(renamed.get("title"), str) or not renamed["title"]:
                        raise RuntimeError("OpenCode did not confirm the new title.")
                except Exception as failure:
                    error = "Could not rename session: " + str(failure)
                def apply():
                    if not session_context_current(state, generation, session_id, serial):
                        return
                    state["rename_inflight"] = False
                    if error:
                        state["session_error"] = error
                    else:
                        state["session_title"] = renamed["title"]
                        remember_project_session(state)
                        sublime.status_message("OpenCode: session renamed.")
                    render(state["window_id"])
                sublime.set_timeout(apply)
            threading.Thread(target=worker, daemon=True).start()
        self.window.show_input_panel("Session name", state.get("session_title") or "", done, None, None)


class OpencodeShowChangesCommand(sublime_plugin.WindowCommand):
    def run(self, window_id=None, refresh=False):
        state = _states.get(window_id or self.window.id())
        if not state or not state.get("session_id") or state.get("session_loading"):
            sublime.status_message("OpenCode: open a session to view its recorded changes.")
            return
        if state.get("diff_open") and not refresh:
            reset_changes(state)
            render(state["window_id"])
        else:
            state["diff_open"] = True
            refresh_session_changes(state)


class OpencodeSetDiffTextCommand(sublime_plugin.TextCommand):
    def run(self, edit, text):
        self.view.replace(edit, sublime.Region(0, self.view.size()), text)
        self.view.sel().clear()
        self.view.sel().add(sublime.Region(0))


class OpencodeViewChangeCommand(sublime_plugin.WindowCommand):
    def run(self, diff_index, request, session_id, window_id=None, open_file=False):
        state = _states.get(window_id or self.window.id())
        if (not state or state.get("session_loading") or state["session_id"] != session_id
            or state["diff_request"] != request or not 0 <= diff_index < len(state["diffs"])):
            return
        item = state["diffs"][diff_index]
        if open_file:
            try:
                path = safe_project_path(state["directory"], item["file"])
                if not os.path.isfile(path):
                    raise RuntimeError("File no longer exists. Use Diff to inspect the recorded changes.")
                self.window.open_file(path, group=0)
            except Exception as error:
                state["diff_error"] = str(error)
                render(state["window_id"])
            return
        text = item["patch"]
        if not text:
            text = "{}: {} (+{} / -{})\n\nOpenCode supplied no text diff for this file.\n".format(item["file"], item["status"], item["additions"], item["deletions"])
        view = self.window.new_file()
        self.window.set_view_index(view, 0, 0)
        view.set_name("OpenCode Diff · " + os.path.basename(item["file"]))
        view.set_scratch(True)
        view.assign_syntax("Packages/Diff/Diff.sublime-syntax")
        view.run_command("opencode_set_diff_text", {"text": text})
        view.set_read_only(True)
        self.window.focus_view(view)


class OpencodeAbortCommand(sublime_plugin.WindowCommand):
    def run(self, window_id=None):
        state = _states.get(window_id or self.window.id())
        if not state or not state["busy"] or not state.get("active_turn"):
            return
        turn = state["active_turn"]
        turn.update(abort_requested=True, phase="cancelling", status_detail="", action_error="")
        render(state["window_id"])


class OpencodeToggleThinkingCommand(sublime_plugin.WindowCommand):
    def run(self, turn_index, window_id=None):
        state = _states.get(window_id or self.window.id())
        if not state or not 0 <= turn_index < len(state["turns"]):
            return
        turn = state["turns"][turn_index]
        turn["thinking_open"] = not turn.get("thinking_open", False)
        render(state["window_id"])


def reply_to_request(state, turn, path, body, request_id, kind):
    wid, generation = state["window_id"], state["generation"]
    if not run_current(wid, state, generation, turn) or turn.get("request_inflight"):
        return
    if not any(item["id"] == request_id for item in turn.get(kind, [])):
        return
    turn["request_inflight"] = True
    turn["action_error"] = ""
    render(wid)
    def worker():
        error = ""
        try:
            if not run_current(wid, state, generation, turn):
                return
            result = request_json("POST", path, body, timeout=10, directory=state["directory"])
            if result is False:
                raise RuntimeError("OpenCode did not confirm the reply.")
        except Exception as exc:
            error = "Reply failed: {}".format(exc)
        def finish():
            if not run_current(wid, state, generation, turn):
                return
            turn["request_inflight"] = False
            turn["action_error"] = error
            if not error:
                turn[kind] = [item for item in turn.get(kind, []) if item["id"] != request_id]
                turn.update(phase="waiting", status_detail="")
            render(wid)
        sublime.set_timeout(finish)
    threading.Thread(target=worker, daemon=True).start()


class OpencodePermissionReplyCommand(sublime_plugin.WindowCommand):
    def run(self, request_id, reply, window_id=None):
        state = _states.get(window_id or self.window.id())
        if not state or reply not in ("once", "reject") or not state.get("active_turn"):
            return
        reply_to_request(state, state["active_turn"], "/permission/{}/reply".format(urllib.parse.quote(request_id, safe="")),
            {"reply": reply}, request_id, "permissions")


class OpencodeQuestionReplyCommand(sublime_plugin.WindowCommand):
    def run(self, request_id, window_id=None, reject=False):
        state = _states.get(window_id or self.window.id())
        turn = state.get("active_turn") if state else None
        request = next((item for item in turn.get("questions", []) if item["id"] == request_id), None) if turn else None
        if not request or not state["busy"]:
            return
        path = "/question/{}".format(urllib.parse.quote(request_id, safe=""))
        if reject:
            reply_to_request(state, turn, path + "/reject", None, request_id, "questions")
            return
        window = get_window(state["window_id"])
        if not window:
            return
        questions, answers = request.get("questions", []), []
        generation = state["generation"]
        def ask(index, selected=None):
            if not run_current(state["window_id"], state, generation, turn):
                return
            if index == len(questions):
                reply_to_request(state, turn, path + "/reply", {"answers": answers}, request_id, "questions")
                return
            question = questions[index]
            options = question.get("options") or []
            selected = list(selected or [])
            multiple = question.get("multiple", False)
            items = [[("✓ " if option["label"] in selected else "") + option["label"], question.get("question", "") + " · " + option.get("description", "")] for option in options]
            custom_index = len(items) if question.get("custom", True) else None
            if custom_index is not None:
                items.append(["Write an answer…", question.get("question", "")])
            done_index = len(items) if multiple else None
            if multiple:
                items.append(["Confirm selected answers", " · ".join(selected)])
            def done(choice):
                if choice < 0 or not run_current(state["window_id"], state, generation, turn):
                    return
                if choice == custom_index:
                    def entered(text):
                        if text.strip():
                            answers.append(selected + [text.strip()] if multiple else [text.strip()])
                            sublime.set_timeout(lambda: ask(index + 1))
                    window.show_input_panel(question.get("question", "Answer"), "", entered, None, None)
                elif choice == done_index:
                    if selected:
                        answers.append(selected)
                        sublime.set_timeout(lambda: ask(index + 1))
                    else:
                        sublime.set_timeout(lambda: ask(index, selected))
                elif choice < len(options):
                    label = options[choice]["label"]
                    if multiple:
                        selected.remove(label) if label in selected else selected.append(label)
                        sublime.set_timeout(lambda: ask(index, selected))
                    else:
                        answers.append([label])
                        sublime.set_timeout(lambda: ask(index + 1))
            window.show_quick_panel(items, done)
        ask(0)


class OpencodeCloseAgentCommand(sublime_plugin.WindowCommand):
    def run(self, window_id=None):
        window_id = window_id or self.window.id()
        state = _states.pop(window_id, None)
        if not state:
            return

        remember_project_session(state)
        if state.get("active_turn"):
            state["active_turn"]["stop_event"].set()

        window = get_window(window_id)
        composer = state.get("composer")

        if state.get("phantoms") is not None:
            state["phantoms"].update([])
        if composer is not None:
            try:
                composer.close()
            except Exception:
                pass
        if window is not None:
            window.set_layout(state["old_layout"])


class OpencodeWindowListener(sublime_plugin.EventListener):
    def on_selection_modified(self, view):
        if not view.settings().get("opencode_composer") or view.size() < 1:
            return
        regions = list(view.sel())
        if any(min(r.a, r.b) < 1 for r in regions):
            view.sel().clear()
            for region in regions:
                view.sel().add(sublime.Region(max(1, region.a), max(1, region.b)))

    def on_text_command(self, view, command_name, args):
        if not view.settings().get("opencode_composer"):
            return
        if command_name in ("left_delete", "delete_word", "delete_to_mark", "delete_to_beginning_of_line"):
            if any(r.empty() and r.begin() <= 1 for r in view.sel()):
                return ("noop", {})

    def on_modified(self, view):
        if not view.settings().get("opencode_composer"):
            return
        if view.substr(sublime.Region(0, 1)) != "\n":
            view.run_command("opencode_repair_anchor")
        window = view.window()
        state = _states.get(window.id()) if window else None
        if not state:
            return
        state["mention_request"] += 1
        serial = state["mention_request"]
        def show():
            if _states.get(window.id()) is not state or state["mention_request"] != serial or not view.is_valid():
                return
            if len(view.sel()) == 1 and mention_at(view, view.sel()[0].b):
                view.run_command("auto_complete", {"disable_auto_insert": True, "next_completion_if_showing": False})
        sublime.set_timeout(show, 120)

    def on_query_completions(self, view, prefix, locations):
        if not view.settings().get("opencode_composer") or len(locations) != 1:
            return None
        window = view.window()
        state = _states.get(window.id()) if window else None
        mention = mention_at(view, locations[0])
        if not state or not mention:
            return None
        start, end, query = mention
        query = query.lower()
        entries = [f for f in state["files"] if query in f["relative"].lower()]
        items = []
        for entry in entries[:200]:
            item = sublime.CompletionItem.command_completion(
                entry["relative"], "opencode_insert_file_reference",
                {"start": start, "end": end, "relative": entry["relative"], "expected": view.substr(sublime.Region(start, end))},
                annotation="Project file",
            )
            # The command replaces the whole @token itself. Otherwise Sublime
            # erases the completion prefix first and our validation rejects it.
            item.flags = sublime.COMPLETION_FLAG_KEEP_PREFIX
            items.append(item)
        return (items, sublime.INHIBIT_WORD_COMPLETIONS | sublime.INHIBIT_EXPLICIT_COMPLETIONS | sublime.DYNAMIC_COMPLETIONS)

    def on_pre_close_window(self, window):
        state = _states.pop(window.id(), None)
        if state:
            remember_project_session(state)
        if state and state.get("active_turn"):
            state["active_turn"]["stop_event"].set()


class OpencodeRepairAnchorCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        if self.view.substr(sublime.Region(0, 1)) != "\n":
            self.view.insert(edit, 0, "\n")
        window = self.view.window()
        if window:
            render(window.id())


class OpencodeInsertFileReferenceCommand(sublime_plugin.TextCommand):
    def run(self, edit, start, end, relative, expected):
        if not self.view.settings().get("opencode_composer") or start < 1:
            return
        region = sublime.Region(start, end)
        if self.view.substr(region) != expected:
            return
        reference = '@"' + relative + '" ' if any(ch.isspace() for ch in relative) else "@" + relative + " "
        self.view.replace(edit, region, reference)
        self.view.sel().clear()
        self.view.sel().add(sublime.Region(start + len(reference)))


class OpencodeRefreshFilesCommand(sublime_plugin.WindowCommand):
    def run(self):
        refresh_files(self.window.id())
