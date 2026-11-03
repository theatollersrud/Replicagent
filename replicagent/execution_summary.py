"""Concise, observation-based summaries of cloud execution records."""
import os
import re
import shlex
from pathlib import PurePosixPath
from urllib.parse import quote, urlparse

ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
INSPECTION = {"cat", "head", "tail", "find", "ls", "pwd", "which", "stat", "du", "df", "grep", "rg", "echo", "printf"}


def plain(value, words=25):
    text = ANSI.sub("", str(value))
    text = re.sub(r"[\x00-\x1f\x60<>\[\]]", " ", text)
    text = " ".join(text.split())
    tokens = text.split()
    text = " ".join(tokens[:words]) + ("…" if len(tokens) > words else "")
    return text[:220] + ("…" if len(text) > 220 else "")


def command_key(attempt):
    argv = list(attempt.get("argv", []))
    if argv:
        first = PurePosixPath(argv[0]).name
        argv[0] = "python" if re.fullmatch(r"python(?:\d+(?:\.\d+)?)?", first) else first
    return attempt.get("working_directory", "."), tuple(argv)


def command_label(attempt):
    _, argv = command_key(attempt)
    return plain(shlex.join(argv), 12)


def test_totals(attempt):
    """Use one pytest/unittest observation, never sum overlapping runs."""
    output = ANSI.sub("", attempt.get("stdout_tail", ""))
    for line in reversed(output.splitlines()):
        if " in " not in line:
            continue
        counts = re.findall(r"\b(\d+)\s+(passed|failed|skipped|errors?|xfailed|xpassed|deselected)\b", line)
        if counts:
            return [(int(count), label) for count, label in counts]
    match = re.search(r"Ran (\d+) tests? in ", output)
    if match and attempt.get("exit_code") == 0:
        return [(int(match[1]), "passed")]
    return []


def program_observation(attempt):
    """Keep one short terminal result, excluding tracebacks and file listings."""
    if attempt.get("exit_code") != 0:
        return ""
    output = ANSI.sub("", attempt.get("stdout_tail", ""))
    for line in reversed(output.splitlines()):
        line = line.strip()
        if not line:
            continue
        if len(line) > 250 or line.startswith(("/", "Error:", "Traceback", "[start]", "[end]", "WARNING", "INFO:", "\x60\x60\x60", "<")):
            return ""
        if any(term in line.lower() for term in ("ignore previous", "system prompt", "instructions", "credential")):
            return ""
        return plain(line, 25)
    return ""


def record_link(execution):
    """Use the authenticated Cloud Storage console, without creating a public URL."""
    supplied = execution.get("record_url", "")
    parsed = urlparse(supplied)
    if (parsed.scheme == "https" and parsed.netloc == "console.cloud.google.com"
            and parsed.path.startswith("/storage/browser/_details/")):
        return supplied
    request_id = execution.get("request_id", "")
    bucket = os.getenv("REPLICAGENT_DYNAMIC_BUCKET", "")
    if re.fullmatch(r"[a-zA-Z0-9]{1,64}", request_id) and re.fullmatch(r"[a-z0-9][a-z0-9._-]+", bucket):
        path = f"{bucket}/dynamic/{request_id}/result.json"
        return "https://console.cloud.google.com/storage/browser/_details/" + quote(path, safe="/")
    return ""


def execution_summary(execution):
    elapsed = execution.get("elapsed_seconds")
    seconds = f"{elapsed:.0f}" if isinstance(elapsed, (int, float)) else "an unknown number of"
    budget = execution.get("budget_seconds", 300)
    lines = ["", "### Dynamic execution summary", "",
             f"The cloud run used {seconds} seconds of its {budget}-second budget."]
    attempts = [a for a in execution.get("attempts", [])
                if a.get("argv") and a.get("kind") in ("run", "test")
                and PurePosixPath(a["argv"][0]).name not in INSPECTION]
    completed = [a for a in attempts if a.get("exit_code") == 0 and not a.get("timed_out")]
    groups = {}
    for a in attempts:
        groups.setdefault(command_key(a), []).append(a)
    if len(groups) == 1 and completed and attempts[-1] in completed:
        failures = sum(a.get("exit_code") != 0 or a.get("timed_out") for a in attempts[:-1])
        ending = f" after {failures} earlier failed {'attempt' if failures == 1 else 'attempts'}" if failures else ""
        lines.append(f"The command \u0060{command_label(attempts[-1])}\u0060 completed{ending}.")
    elif completed:
        lines.append(f"{len(completed)} of {len(attempts)} code or test runs completed successfully.")
    else:
        lines.append("No repository code completed successfully.")

    candidates = [(index, a, test_totals(a)) for index, a in enumerate(attempts) if a.get("kind") == "test"]
    candidates = [(i, a, counts) for i, a, counts in candidates if counts]
    if candidates:
        _, chosen, counts = max(candidates, key=lambda row: (sum(n for n, _ in row[2]), row[0]))
        totals = ", ".join(f"{count} {label}" for count, label in counts)
        lines.append(f"The largest reported test run recorded {totals}.")
    elif completed:
        observation = program_observation(completed[-1])
        if observation:
            lines.append(f"The program reported: \u0060{observation}\u0060.")

    # Use the last outcome for each command so a recovered dependency failure
    # does not appear as an outstanding defect.
    unresolved = [group[-1] for group in groups.values()
                  if group[-1].get("exit_code") != 0 or group[-1].get("timed_out")]
    failed = [a for a in unresolved if not a.get("timed_out")]
    timed = [a for a in unresolved if a.get("timed_out")]
    if failed:
        a = failed[0]
        output = a.get("stdout_tail", "")
        if "ModuleNotFoundError" in output or "No module named" in output:
            issue = "a missing dependency"
        elif "FileNotFoundError" in output or "No such file or directory" in output:
            issue = "a missing input or executable"
        elif "No space left on device" in output:
            issue = "an installation space limit"
        else:
            issue = f"exit code {a.get('exit_code')}" if a.get("exit_code") is not None else "a command-launch failure"
        lines.append(f"The attempt \u0060{command_label(a)}\u0060 failed with {issue}.")
    if timed:
        lines.append(f"The command \u0060{command_label(timed[0])}\u0060 reached its time limit.")
    elif execution.get("timed_out"):
        lines.append("The overall execution time limit was reached.")

    setup = [a for a in execution.get("attempts", []) if a.get("kind") == "setup" and a.get("argv")]
    if completed and any(a.get("exit_code") != 0 for a in setup):
        if any("No space left on device" in a.get("stdout_tail", "") for a in setup):
            lines.append("The run continued after an installation space error.")
        else:
            lines.append("The run continued after dependency-setup failures.")
    elif not attempts and execution.get("reason"):
        lines.append("The recorded blocker was: " + plain(execution["reason"], 25) + ".")

    result_check = execution.get("criteria", {}).get("result_reproduction", {})
    if result_check.get("fraction") is None:
        lines.append("The paper's numerical results remain unverified.")
    else:
        lines.append("The score records the separate assessment of the paper's numerical results.")
    url = record_link(execution)
    if url:
        lines.extend(["", f"[Open the full execution record]({url})."])
    else:
        lines.extend(["", "The full command records remain in the saved audit data."])
    return lines
