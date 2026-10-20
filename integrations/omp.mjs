import { spawn } from "node:child_process";
import { createHash, randomUUID } from "node:crypto";
import { closeSync, constants as fsConstants, existsSync, fstatSync, lstatSync, openSync, readSync } from "node:fs";
import { dirname, isAbsolute, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const outputLimit = 1024 * 1024;

function childEnvironment() {
  const env = { WRR_RUNTIME: "omp" };
  for (const key of ["HOME", "PATH", "LANG", "LC_ALL", "TMPDIR", "SSL_CERT_FILE", "SSL_CERT_DIR", "WRR_ENV", "WRR_COMMUNITY_CONFIG"]) {
    if (process.env[key] !== undefined) env[key] = process.env[key];
  }
  for (const key of (process.env.WRR_SHARED_ENV_KEYS ?? "").split(",").map(key => key.trim()).filter(Boolean)) {
    if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(key)) throw new Error("Invalid WRR_SHARED_ENV_KEYS entry");
    if (process.env[key] !== undefined) env[key] = process.env[key];
  }
  return env;
}

// `isolated` spawns (skill handoff) run a policy-chosen interpreter with an exact
// environment and a credential fd, stay in the adapter's process group and are
// stopped through the child handle. Without it every call behaves as before.
async function invokeCli(args, cwd, signal, input, isolated) {
  if (signal?.aborted) throw new DOMException("WRR call cancelled", "AbortError");
  let python;
  let env;
  if (isolated) {
    python = isolated.python;
    env = isolated.env;
  } else {
    python = process.env.WRR_PYTHON || join(root, ".venv", "bin", "python");
    if (!process.env.WRR_PYTHON && !existsSync(python)) {
      throw new Error("WRR Python environment unavailable; install WRR or explicitly set WRR_PYTHON");
    }
    env = childEnvironment();
  }
  // Official MCP stdio teardown can need its bounded stdin/TERM/KILL grace periods.
  const cancellationGrace = env.WRR_COMMUNITY_CONFIG ? 8000 : 1000;
  return new Promise((resolve, reject) => {
    let child;
    try {
      child = spawn(python, [join(root, "wrr-cli.py"), ...args], {
        cwd, env, shell: false, detached: isolated ? false : process.platform !== "win32",
        stdio: [input === undefined ? "ignore" : "pipe", "pipe", "pipe", ...(isolated ? [isolated.fd] : [])],
      });
    } finally {
      // The child holds its own copy of fd 3; never keep the credential fd open in this process.
      isolated?.closeFd();
    }
    let stopped;
    let killTimer;
    let exited = false;
    let bytes = 0;
    const chunks = [];
    const kill = signalName => {
      if (!child.pid) return;
      if (isolated) {
        if (!exited) child.kill(signalName);
        return;
      }
      try {
        if (process.platform === "win32") child.kill(signalName);
        else process.kill(-child.pid, signalName);
      } catch (error) {
        if (error.code !== "ESRCH") throw error;
      }
    };
    const stop = reason => {
      if (stopped) return;
      stopped = reason;
      kill("SIGINT");
      killTimer = setTimeout(() => kill("SIGKILL"), cancellationGrace);
    };
    const abort = () => stop("cancelled");
    const timeout = setTimeout(() => stop("timeout"), 180000);
    const cleanup = () => {
      clearTimeout(timeout);
      clearTimeout(killTimer);
      signal?.removeEventListener("abort", abort);
    };
    signal?.addEventListener("abort", abort, { once: true });
    // Account for an abort between the pre-spawn check and listener registration.
    if (signal?.aborted) abort();
    if (input !== undefined) {
      child.stdin.on("error", () => {});
      child.stdin.end(input);
    }
    child.once("exit", () => { exited = true; });
    child.stdout.on("data", chunk => {
      bytes += chunk.length;
      if (bytes > outputLimit) stop("output limit exceeded");
      else chunks.push(chunk);
    });
    // Never expose raw provider stderr; Python's JSON result is the diagnostic contract.
    child.stderr.on("data", chunk => {
      bytes += chunk.length;
      if (bytes > outputLimit) stop("output limit exceeded");
    });
    child.once("error", () => {
      cleanup();
      reject(new Error("WRR Python process could not start"));
    });
    child.once("close", code => {
      cleanup();
      if (stopped) {
        // The parent may exit before a child that ignored SIGINT; own the whole group.
        if (!isolated) kill("SIGKILL");
        reject(stopped === "cancelled"
          ? new DOMException("WRR call cancelled", "AbortError")
          : new Error(`WRR ${stopped}`));
        return;
      }
      let result;
      try { result = JSON.parse(Buffer.concat(chunks).toString("utf8")); }
      catch { reject(new Error(`WRR returned no valid JSON (exit ${code})`)); return; }
      resolve({
        content: [{ type: "text", text: JSON.stringify(result) }],
        details: result,
        isError: code !== 0 || result.ok !== true,
      });
    });
  });
}

function toolResult(details, isError = false) {
  return {
    content: [{ type: "text", text: JSON.stringify(details) }],
    details,
    isError,
  };
}

// resolveJudge speaks native noul; eval's public bool alias never crosses RPC.
function nativeQuestion(question) {
  return { ...question, type: question.type === "bool" ? "noul" : question.type };
}

function wireAnswer(answer) {
  if (answer.type === "bool") {
    const { bool, ...rest } = answer;
    return { ...rest, type: "noul", noul: bool };
  }
  return answer;
}

async function hostJudge(pi, ctx) {
  try {
    const { resolveJudge } = await import("@oh-my-pi/pi-coding-agent/judgment");
    return resolveJudge({
      settings: pi.pi.settings, registry: ctx.modelRegistry, sessionModel: ctx.model,
      sessionId: ctx.sessionManager.getSessionId(), purpose: "wrr-search",
    });
  } catch {
    // Installed hosts without the export use Python's TypeSafe/NoJudge resolver.
    return null;
  }
}

async function invokeSearchRpc(pi, params, signal, ctx, nativeSearch) {
  if (signal?.aborted) throw new DOMException("WRR call cancelled", "AbortError");
  const judge = await hostJudge(pi, ctx);
  if (signal?.aborted) throw new DOMException("WRR call cancelled", "AbortError");
  const python = process.env.WRR_PYTHON || join(root, ".venv", "bin", "python");
  if (!process.env.WRR_PYTHON && !existsSync(python)) {
    throw new Error("WRR Python environment unavailable; install WRR or explicitly set WRR_PYTHON");
  }
  const deadlineMs = params.deadline_ms ?? 15000;
  const env = childEnvironment();
  const cancellationGrace = env.WRR_COMMUNITY_CONFIG ? 8000 : 1000;
  return new Promise((resolve, reject) => {
    const child = spawn(python, [join(root, "wrr-cli.py"), "search", "--rpc", "-q"], {
      cwd: ctx.cwd, env, shell: false,
      detached: process.platform !== "win32", stdio: ["pipe", "pipe", "pipe"],
    });
    const active = new Map();
    let buffer = "";
    let bytes = 0;
    let result;
    let failure;
    let killTimer;
    let exited = false;
    const send = message => {
      if (!child.stdin.destroyed && !child.stdin.writableEnded) child.stdin.write(JSON.stringify(message) + "\n");
    };
    const kill = name => {
      if (!child.pid || exited) return;
      try {
        if (process.platform === "win32") child.kill(name);
        else process.kill(-child.pid, name);
      } catch (error) {
        if (error.code !== "ESRCH") failure ??= new Error("WRR process teardown failed");
      }
    };
    const stop = error => {
      if (failure) return;
      failure = error;
      send({ method: "cancel" });
      for (const controller of active.values()) controller.abort();
      // Let Python serialize cancelled evidence and tear down source tasks first.
      killTimer = setTimeout(() => {
        kill("SIGINT");
        killTimer = setTimeout(() => kill("SIGKILL"), cancellationGrace);
      }, 1000);
    };
    const abort = () => stop(new DOMException("WRR call cancelled", "AbortError"));
    const timer = setTimeout(() => stop(new Error("WRR deadline exceeded")), deadlineMs + 1000);
    const cleanup = () => {
      clearTimeout(timer);
      clearTimeout(killTimer);
      signal?.removeEventListener("abort", abort);
      for (const controller of active.values()) controller.abort();
    };
    const dispatch = async message => {
      if (message.method === "cancel") {
        active.get(message.id)?.abort();
        return;
      }
      if (Object.hasOwn(message, "result") && !Object.hasOwn(message, "id")) {
        result = message.result;
        return;
      }
      if (message.error && !message.id) {
        stop(new Error(`WRR RPC ${message.error.reason ?? "failed"}`));
        return;
      }
      if (!message.id || !["judge", "host_search"].includes(message.method)) {
        stop(new Error("WRR returned an invalid RPC message"));
        return;
      }
      const controller = new AbortController();
      active.set(message.id, controller);
      if (signal?.aborted || failure) controller.abort();
      try {
        let response;
        if (message.method === "judge") {
          if (!judge) throw new Error("unavailable");
          const questions = Object.fromEntries(Object.entries(message.params.questions).map(([id, q]) => [id, nativeQuestion(q)]));
          const value = await judge.judge({ state: message.params.state, questions }, { signal: controller.signal });
          response = {
            answers: Object.fromEntries(Object.entries(value.answers).map(([id, answer]) => [id, wireAnswer(answer)])),
            model: value.model,
            posts: 1,
            usage: { input_tokens: value.usage?.input ?? value.usage?.input_tokens ?? 0,
              output_tokens: value.usage?.output ?? value.usage?.output_tokens ?? 0 },
          };
        } else {
          if (!nativeSearch) throw new Error("unavailable");
          const value = await nativeSearch({
            query: message.params.query, limit: message.params.count,
          }, controller.signal);
          if (value.details?.error || value.isError) throw new Error("unavailable");
          const native = value.details?.response;
          if (!native || !Array.isArray(native.sources ?? native.citations)) throw new Error("malformed_response");
          response = {
            provider: native.provider,
            results: (native.sources?.length ? native.sources : native.citations ?? []).map(row => ({
              title: row.title ?? row.url, url: row.url, snippet: row.snippet ?? row.citedText ?? "",
            })),
          };
        }
        if (!controller.signal.aborted) send({ id: message.id, result: response });
      } catch (error) {
        const reason = controller.signal.aborted ? "cancelled"
          : ["unavailable", "malformed_response"].includes(error.message) ? error.message : "network";
        send({ id: message.id, error: { reason } });
      } finally {
        active.delete(message.id);
      }
    };
    child.stdin.on("error", () => {});
    child.stdout.setEncoding("utf8");
    child.stdout.on("data", chunk => {
      bytes += Buffer.byteLength(chunk);
      if (bytes > outputLimit) { stop(new Error("WRR output limit exceeded")); return; }
      buffer += chunk;
      let newline;
      while ((newline = buffer.indexOf("\n")) !== -1) {
        const line = buffer.slice(0, newline);
        buffer = buffer.slice(newline + 1);
        if (!line.trim()) continue;
        try {
          const message = JSON.parse(line);
          if (!message || typeof message !== "object" || Array.isArray(message)) throw new Error();
          void dispatch(message).catch(() => stop(new Error("WRR RPC dispatch failed")));
        } catch { stop(new Error("WRR returned invalid NDJSON")); }
      }
    });
    child.stderr.on("data", chunk => {
      bytes += chunk.length;
      if (bytes > outputLimit) stop(new Error("WRR output limit exceeded"));
    });
    child.once("error", () => { cleanup(); reject(new Error("WRR Python process could not start")); });
    child.once("exit", () => { exited = true; });
    child.once("close", code => {
      cleanup();
      if (failure) { reject(failure); return; }
      if (!result || buffer.trim()) { reject(new Error("WRR returned no complete Evidence")); return; }
      resolve(toolResult(result, code !== 0 || ["failed", "cancelled", "blocked"].includes(result.quality?.executed)));
    });
    signal?.addEventListener("abort", abort, { once: true });
    send({
      query: params.query, count: params.count ?? 8, sources: params.sources ?? (params.provider ? [params.provider] : null),
      deadline_ms: deadlineMs, host: { judge: judge !== null, search: !!nativeSearch },
    });
    if (signal?.aborted) abort();
  });
}

function handoffReason(result) {
  const raw = result.details?.ok
    ? result.details?.quality?.reasons?.[0] ?? "quality_incomplete"
    : result.details?.error ?? "fetch_failed";
  const token = String(raw).toLowerCase().replace(/[^a-z0-9_.-]+/g, "_").replace(/^[^a-z]+/, "");
  return `wrr_${token || "fetch_failed"}`.slice(0, 64);
}

// ---------------------------------------------------------------------------
// Skill handoff (MH04 H3-S): recommendation -> host accept -> observed exact
// skill:// read -> typed, unverified host report. Never claims execution.
// ---------------------------------------------------------------------------

const POLICY_ENV = "WRR_SKILL_HANDOFF_POLICY";
const ENFORCE_ENV = "WRR_SKILL_HANDOFF_ENFORCE";
const SKILL_DEADLINE_MS = 300000;
const SKILL_CAPACITY = 32;
const ACTIVE_STATES = new Set(["accepted", "read_pending", "read_observed"]);
const TERMINAL_STATES = new Set(["rejected", "cancelled", "read_failed", "host_reported", "expired"]);
const HOST_OUTCOMES = new Set(["received", "declined", "aborted"]);
const LIFECYCLE_EVENTS = ["session_start", "session_switch", "session_branch", "session_tree", "session_shutdown"];
const BLOCK = Object.freeze({ block: true, reason: "mh04_private_policy" });
const NAME_RE = /^[a-z0-9][a-z0-9-]{0,63}$/;
const URL_PATH_RE = /^\/[A-Za-z0-9][A-Za-z0-9._-]{0,99}\/[A-Za-z0-9][A-Za-z0-9._-]{0,99}\/tree\/HEAD\/[A-Za-z0-9][A-Za-z0-9._/-]{0,511}$/;
const SHA256_RE = /^[0-9a-f]{64}$/;
// Node/libuv sets close-on-exec itself; pass the flag where the runtime understands it.
const O_CLOEXEC = fsConstants.O_CLOEXEC ?? 0;
const SECURE_OPEN = fsConstants.O_RDONLY | fsConstants.O_NOFOLLOW | fsConstants.O_NONBLOCK | O_CLOEXEC;
// One search budget per process, shared by every extension binding and re-import.
const processState = (globalThis[Symbol.for("wrr.mh04.skill_handoff")] ??= { searchIssued: false });

const sha256 = text => createHash("sha256").update(text, "utf8").digest("hex");
const isPlainObject = value => value !== null && typeof value === "object" && !Array.isArray(value)
  && Object.getPrototypeOf(value) === Object.prototype;
const hasExactKeys = (value, keys) => isPlainObject(value)
  && Object.keys(value).length === keys.length && keys.every(key => Object.hasOwn(value, key));
const utf8Length = text => Buffer.byteLength(text, "utf8");

function validName(name) {
  return typeof name === "string" && NAME_RE.test(name);
}

function validUrl(url) {
  if (typeof url !== "string" || !/^[\x21-\x7e]+$/.test(url)) return false;
  if (url.length < 34 || url.length > 743 || url.includes("%")) return false;
  let parsed;
  try { parsed = new URL(url); } catch { return false; }
  if (parsed.protocol !== "https:" || parsed.hostname !== "github.com" || parsed.port !== ""
    || parsed.username !== "" || parsed.password !== "" || parsed.search !== "" || parsed.hash !== "") return false;
  if (parsed.href !== url || !URL_PATH_RE.test(parsed.pathname)) return false;
  return parsed.pathname.split("/").slice(1).every(segment => segment !== "" && segment !== "." && segment !== "..");
}

function ownedSafe(stat, allowRoot) {
  const uid = process.getuid();
  return (stat.uid === uid || (allowRoot && stat.uid === 0)) && (stat.mode & 0o022) === 0;
}

function trustedPython(path) {
  try {
    const file = lstatSync(path);
    const parent = lstatSync(dirname(path));
    return file.isFile() && !file.isSymbolicLink() && (file.mode & 0o111) !== 0 && ownedSafe(file, true)
      && parent.isDirectory() && !parent.isSymbolicLink() && ownedSafe(parent, true);
  } catch {
    return false;
  }
}

function privateDirectory(path) {
  try {
    const stat = lstatSync(path);
    return stat.isDirectory() && !stat.isSymbolicLink() && stat.uid === process.getuid() && (stat.mode & 0o022) === 0;
  } catch {
    return false;
  }
}

// Open a private regular file per the contract's file rules; returns the fd or null.
function openPrivateFile(path, maxSize) {
  let fd;
  try {
    fd = openSync(path, SECURE_OPEN);
    const stat = fstatSync(fd);
    if (!stat.isFile() || stat.uid !== process.getuid() || (stat.mode & 0o077) !== 0 || stat.nlink !== 1
      || (maxSize !== undefined && stat.size > maxSize)) {
      closeSync(fd);
      return null;
    }
    return fd;
  } catch {
    if (fd !== undefined) closeSync(fd);
    return null;
  }
}

function validatePolicy(value) {
  if (!hasExactKeys(value, ["schema_version", "allow", "search", "child"]) || value.schema_version !== 1) return null;
  const { allow, search, child } = value;
  if (!Array.isArray(allow) || allow.length < 1 || allow.length > 8) return null;
  for (const entry of allow) {
    if (!hasExactKeys(entry, ["name", "url", "resolved_path_sha256"])) return null;
    if (!validName(entry.name) || !validUrl(entry.url) || typeof entry.resolved_path_sha256 !== "string"
      || !SHA256_RE.test(entry.resolved_path_sha256)) return null;
  }
  if (!hasExactKeys(search, ["query"]) || typeof search.query !== "string") return null;
  const queryBytes = utf8Length(search.query);
  if (queryBytes < 1 || queryBytes > 256) return null;
  if (!hasExactKeys(child, ["python", "home", "tmpdir", "env_file"])) return null;
  for (const key of ["python", "home", "tmpdir", "env_file"]) {
    if (typeof child[key] !== "string" || !isAbsolute(child[key])) return null;
  }
  if (!trustedPython(child.python) || !privateDirectory(child.home) || !privateDirectory(child.tmpdir)) return null;
  const envFd = openPrivateFile(child.env_file);
  if (envFd === null) return null;
  closeSync(envFd);
  return value;
}

function loadPolicy(path) {
  const fd = openPrivateFile(path, 16384);
  if (fd === null) return null;
  try {
    const size = fstatSync(fd).size;
    const buffer = Buffer.alloc(size);
    let offset = 0;
    while (offset < size) {
      const read = readSync(fd, buffer, offset, size - offset, offset);
      if (read === 0) break;
      offset += read;
    }
    return validatePolicy(JSON.parse(buffer.subarray(0, offset).toString("utf8")));
  } catch {
    return null;
  } finally {
    closeSync(fd);
  }
}

export function projectCandidate(details) {
  if (!isPlainObject(details) || details.quality?.executed !== "ok") return null;
  if (!Array.isArray(details.plan?.sources) || details.plan.sources.length !== 1 || details.plan.sources[0] !== "skill") return null;
  if (!Array.isArray(details.runs) || details.runs.length !== 1 || details.runs[0]?.source_id !== "skill" || details.runs[0]?.status !== "ok") return null;
  if (!Array.isArray(details.results) || details.results.length !== 1 || !isPlainObject(details.results[0])) return null;
  const { title, url, contributions } = details.results[0];
  if (typeof title !== "string" || typeof url !== "string" || !Array.isArray(contributions)
      || contributions.length !== 1 || contributions[0]?.source_id !== "skill") return null;
  return { name: title, url, source_tag: "skill" };
}

function createSkillHandoff(pi) {
  const policyPath = process.env[POLICY_ENV];
  const policyRequested = typeof policyPath === "string" && isAbsolute(policyPath);
  const policy = policyRequested ? loadPolicy(policyPath) : null;
  const enforceRequested = process.env[ENFORCE_ENV] === "1";
  const enforcementActive = enforceRequested && policy !== null;

  const entries = new Map();
  const reservations = new Set();
  const inFlight = new Set();
  let lifecycleGeneration = 0;

  const activeEntry = exclude => {
    for (const entry of entries.values()) {
      if (entry !== exclude && ACTIVE_STATES.has(entry.status)) return entry;
    }
    return null;
  };
  const purge = () => {
    const now = Date.now();
    for (const [id, entry] of entries) {
      if (entry.deadlineAt <= now) entries.delete(id);
    }
  };
  const canTransition = (entry, action, params) => {
    if (!entry || TERMINAL_STATES.has(entry.status)) return false;
    switch (action) {
      case "reject": return entry.status === "recommended";
      case "accept": return entry.status === "recommended" && params.active_skill_name === entry.candidate.name && !activeEntry(entry);
      case "cancel": return true;
      case "report": return entry.status === "read_observed" && HOST_OUTCOMES.has(params.host_outcome);
      default: return false;
    }
  };
  const response = (entry, correlationId, extra) => toolResult({
    operation: "skill_handoff",
    correlation_id: correlationId,
    ...(entry ? { candidate_sha256: entry.candidateSha256 } : {}),
    ...extra,
  }, extra.ok !== true);
  const unavailable = reason => toolResult({ operation: "search", ok: false, error: "skill_handoff_unavailable", reason }, true);

  async function search(params, signal, ctx) {
    purge();
    if (params.skill_handoff !== "request" || params.provider !== "skill" || params.count !== 1 || params.mode != null) {
      return unavailable("invalid_request");
    }
    if (policy === null) return unavailable("policy_unavailable");
    if (activeEntry(null)) return unavailable("active_exists");
    if (entries.size + reservations.size >= SKILL_CAPACITY) return unavailable("capacity");
    const token = Symbol("reservation");
    reservations.add(token);
    const release = () => reservations.delete(token);
    try {
      if (enforcementActive && params.query !== policy.search.query) return unavailable("query_mismatch");
      if (!trustedPython(policy.child.python) || !privateDirectory(policy.child.home) || !privateDirectory(policy.child.tmpdir)) {
        return unavailable("policy_unavailable");
      }
      const fd = openPrivateFile(policy.child.env_file);
      if (fd === null) return unavailable("policy_unavailable");
      let fdOpen = true;
      const closeFd = () => {
        if (fdOpen) { fdOpen = false; closeSync(fd); }
      };
      const env = { HOME: policy.child.home, TMPDIR: policy.child.tmpdir, WRR_ENV: "/dev/fd/3", WRR_RUNTIME: "omp" };
      for (const key of ["PATH", "LANG"]) {
        if (process.env[key] !== undefined) env[key] = process.env[key];
      }
      const generation = lifecycleGeneration;
      // Lifecycle events stop the in-flight child through this controller.
      const controller = new AbortController();
      const forward = () => controller.abort();
      signal?.addEventListener("abort", forward, { once: true });
      if (signal?.aborted) controller.abort();
      inFlight.add(controller);
      let result;
      try {
        result = await invokeCli(
          ["search", "--count", "1", "--json", "-q", "--provider", "skill", "--", params.query],
          ctx.cwd, controller.signal, undefined, { python: policy.child.python, env, fd, closeFd },
        );
      } catch (error) {
        if (generation !== lifecycleGeneration && !signal?.aborted) {
          return toolResult({ operation: "search", ok: false, handoff: { kind: "host_skill", status: "unavailable", reason: "lifecycle" } }, true);
        }
        throw error;
      } finally {
        closeFd();
        inFlight.delete(controller);
        signal?.removeEventListener("abort", forward);
      }
      const withHandoff = handoff => toolResult({ ...result.details, handoff }, result.isError);
      if (generation !== lifecycleGeneration) return withHandoff({ kind: "host_skill", status: "unavailable", reason: "lifecycle" });
      const candidate = projectCandidate(result.details);
      if (!candidate) return withHandoff({ kind: "host_skill", status: "unavailable", reason: "projection_failed" });
      if (!validName(candidate.name) || !validUrl(candidate.url)) {
        return withHandoff({ kind: "host_skill", status: "unavailable", reason: "grammar_failed" });
      }
      const allow = policy.allow.find(item => item.name === candidate.name && item.url === candidate.url);
      if (!allow) return withHandoff({ kind: "host_skill", status: "unavailable", reason: "not_allowlisted" });
      const correlationId = randomUUID();
      const candidateSha256 = sha256(JSON.stringify(candidate));
      const createdAt = Date.now();
      const entry = {
        status: "recommended", candidate, candidateSha256, allow,
        createdAt, deadlineAt: createdAt + SKILL_DEADLINE_MS, readCallId: null, timer: null, permit: null,
      };
      release();
      entries.set(correlationId, entry);
      entry.timer = ctx.setTimeout(() => {
        if (entries.get(correlationId) === entry) {
          if (!TERMINAL_STATES.has(entry.status)) entry.status = "expired";
          entries.delete(correlationId);
        }
      }, SKILL_DEADLINE_MS);
      return withHandoff({
        schema_version: 1, kind: "host_skill", status: "recommended",
        correlation_id: correlationId, candidate, candidate_sha256: candidateSha256,
      });
    } finally {
      release();
    }
  }

  function transition(params, toolCallId) {
    purge();
    const correlationId = params.correlation_id;
    const entry = entries.get(correlationId);
    // Under enforcement only the call the tool_call gate permitted may transition.
    const permitted = !enforcementActive || (entry && entry.permit === toolCallId);
    if (entry && permitted) entry.permit = null;
    if (!permitted || !canTransition(entry, params.action, params)) {
      return response(entry, correlationId, { ok: false, handoff_status: entry?.status ?? "unknown", error: "invalid_transition" });
    }
    switch (params.action) {
      case "reject": entry.status = "rejected"; break;
      case "cancel": entry.status = "cancelled"; break;
      case "report": entry.status = "host_reported"; entry.hostOutcome = params.host_outcome; break;
      case "accept": entry.status = "accepted"; break;
    }
    const extra = { ok: true, handoff_status: entry.status };
    if (params.action === "accept") {
      extra.active_skill_name = entry.candidate.name;
      extra.target = { tool: "read", arguments: { path: `skill://${entry.candidate.name}` } };
    }
    if (params.action === "report") extra.host_reported_outcome = entry.hostOutcome;
    return response(entry, correlationId, extra);
  }

  const singleEntry = () => entries.values().next().value ?? null;

  function enforce(event) {
    const input = isPlainObject(event.input) ? event.input : {};
    if (event.toolName === "wrr_search") {
      const legal = !processState.searchIssued
        && hasExactKeys(input, ["query", "provider", "count", "skill_handoff"])
        && input.query === policy.search.query && input.provider === "skill"
        && input.count === 1 && input.skill_handoff === "request";
      if (!legal) return BLOCK;
      processState.searchIssued = true;
      return undefined;
    }
    const entry = singleEntry();
    if (event.toolName === "wrr_skill_handoff") {
      if (!entry || entries.get(input.correlation_id) !== entry || entry.permit != null
        || !canTransition(entry, input.action, input)) return BLOCK;
      entry.permit = event.toolCallId;
      return undefined;
    }
    if (event.toolName === "read") {
      const id = event.toolCallId;
      return entry && entry.status === "accepted" && entry.readCallId === null && entry.permit == null
        && input.path === `skill://${entry.candidate.name}`
        && typeof id === "string" && utf8Length(id) >= 1 && utf8Length(id) <= 256 ? undefined : BLOCK;
    }
    return BLOCK;
  }

  // A second tool_call carrying the bound id fails the read whether or not it is
  // permitted; only a permitted call may bind a new read.
  function observeCall(event, permitted) {
    for (const entry of entries.values()) {
      if (entry.status === "read_pending" && event.toolCallId === entry.readCallId) {
        entry.status = "read_failed";
        return;
      }
    }
    if (!permitted || event.toolName !== "read") return;
    const entry = activeEntry(null);
    const id = event.toolCallId;
    if (entry?.status === "accepted" && event.input?.path === `skill://${entry.candidate.name}`
      && typeof id === "string" && utf8Length(id) >= 1 && utf8Length(id) <= 256) {
      entry.status = "read_pending";
      entry.readCallId = id;
    }
  }

  function observeResult(event) {
    for (const entry of entries.values()) {
      if (entry.status !== "read_pending" || event.toolCallId !== entry.readCallId) continue;
      const resolvedPath = event.details?.resolvedPath;
      const ok = event.toolName === "read" && event.input?.path === `skill://${entry.candidate.name}`
        && event.isError === false && typeof resolvedPath === "string"
        && sha256(resolvedPath) === entry.allow.resolved_path_sha256;
      entry.status = ok ? "read_observed" : "read_failed";
      return;
    }
  }

  function register(z, registerTool) {
    // Fail closed when enforcement was requested without a usable policy.
    if (enforceRequested && !enforcementActive) pi.on("tool_call", () => BLOCK);
    if (!policyRequested) return;
    pi.on("tool_call", event => {
      purge();
      const decision = enforcementActive ? enforce(event) : undefined;
      observeCall(event, decision === undefined);
      return decision;
    });
    pi.on("tool_result", event => {
      purge();
      observeResult(event);
      return undefined;
    });
    for (const name of LIFECYCLE_EVENTS) {
      pi.on(name, (_event, ctx) => {
        lifecycleGeneration += 1;
        for (const entry of entries.values()) {
          if (entry.timer) ctx.clearTimer(entry.timer);
        }
        entries.clear();
        reservations.clear();
        for (const controller of inFlight) controller.abort();
      });
    }
    registerTool({
      name: "wrr_skill_handoff", label: "WRR Skill Handoff",
      description: "Accept, reject, cancel, or report on a WRR-recommended host skill. Accept never invokes the skill; the host reads the returned skill:// target itself. A report is an unverified host statement, not proof of execution.",
      parameters: z.object({
        action: z.enum(["accept", "reject", "cancel", "report"]),
        correlation_id: z.string().min(36).max(36),
        active_skill_name: z.string().max(64).nullable().optional(),
        host_outcome: z.enum(["received", "declined", "aborted"]).nullable().optional(),
      }),
      loadMode: "essential",
      ...(enforcementActive ? { approval: () => "write" } : {}),
      async execute(id, params) {
        return transition(params, id);
      },
    });
  }

  return { search, register, enforcementActive, purge };
}

export default function wrrExtension(pi) {
  const z = pi.zod;
  const provider = z.string().nullable().optional();
  const count = z.number().int().min(1).max(20).nullable().optional();
  const skillHandoff = createSkillHandoff(pi);
  const handoffs = new Map();
  const handoffTtlMs = 5 * 60 * 1000;
  const handoffCapacity = 32;
  const purgeHandoffs = () => {
    const cutoff = Date.now() - handoffTtlMs;
    for (const [id, entry] of handoffs) {
      if (entry.createdAt < cutoff) handoffs.delete(id);
    }
  };
  const invalidHandoff = (correlationId, detail) => toolResult({
    operation: "host_fetch_handoff",
    ok: false,
    handoff_status: "invalid",
    execution_success: false,
    correlation_id: correlationId,
    error: "invalid_handoff_transition",
    detail,
  }, true);
  const definitions = [
    {
      name: "wrr_search", operation: "search", label: "WRR Search",
      description: "Search through WRR's inventory, shared Jev routing and quality policy. Returns Evidence with source runs and judgments; use web_search to also include the host-native search chain.",
      parameters: z.object({
        query: z.string().min(1), count, provider,
        sources: z.array(z.string().min(1)).nullable().optional(),
        deadline_ms: z.number().int().min(1).max(300000).nullable().optional(),
        skill_handoff: z.enum(["request"]).nullable().optional().describe("Request a host skill recommendation. Valid only with provider=skill and count=1; requires a WRR skill-handoff policy. WRR never invokes the skill."),
      }),
      args: p => [p.query, ...(p.count == null ? [] : ["--count", String(p.count)])],
    },
    {
      name: "wrr_fetch", operation: "fetch", label: "WRR Fetch",
      description: "Fetch one HTTP(S) URL through WRR; returns the actual provider, page content and quality evidence. No browser or host fallback is invoked by this adapter.",
      parameters: z.object({
        url: z.string().min(1), max_characters: z.number().int().min(1).max(50000).nullable().optional(), provider,
        allow_private_origin: z.string().nullable().optional().describe("Null or omitted means no private-network grant; never use an empty string. Otherwise, exact scheme/host/port explicitly authorized by the user for this invocation only, requiring provider=brave. Never infer permission from the URL."),
        host_fallback: z.enum(["request"]).nullable().optional().describe("Explicitly request a host-native handoff only if WRR fails or quality is not complete. WRR never invokes the host tool."),
      }),
      args: p => [
        p.url, ...(p.max_characters == null ? [] : ["--max-chars", String(p.max_characters)]),
        ...(p.allow_private_origin == null ? [] : ["--allow-private-origin", p.allow_private_origin]),
        ...(p.host_fallback === "request" ? ["--host-fallback", "request"] : []),
      ],
    },
    {
      name: "wrr_similar", operation: "similar", label: "WRR Similar",
      description: "Find similar pages through WRR's shared provider policy; results are search excerpts.",
      parameters: z.object({ url: z.string().min(1), count, provider }),
      args: p => [p.url, ...(p.count == null ? [] : ["--count", String(p.count)])],
    },
  ];
  for (const definition of definitions) {
    pi.registerTool({
      name: definition.name, label: definition.label, description: definition.description,
      parameters: definition.parameters, loadMode: "essential",
      ...(definition.operation === "search" && skillHandoff.enforcementActive ? { approval: () => "write" } : {}),
      async execute(_id, params, signal, _onUpdate, ctx) {
        skillHandoff.purge();
        if (definition.operation === "search" && params.skill_handoff != null) {
          return skillHandoff.search(params, signal, ctx);
        }
        if (definition.operation === "search") return invokeSearchRpc(pi, params, signal, ctx);
        const [identifier, ...options] = definition.args(params);
        const args = [definition.operation, ...options, "--json", "-q"];
        if (params.provider) args.push("--provider", params.provider);
        args.push("--", identifier);
        let hostFallbackEligible = false;
        if (definition.operation === "fetch" && params.host_fallback === "request") {
          const eligibility = await invokeCli(
            ["host-fetch-eligibility", "--", params.url],
            ctx.cwd,
            signal,
          );
          hostFallbackEligible = (
            eligibility.details?.ok === true && eligibility.details?.eligible === true
          );
        }
        const result = await invokeCli(args, ctx.cwd, signal);
        if (definition.operation !== "fetch" || params.host_fallback !== "request") return result;
        if (result.details?.host_fallback?.status === "denied") return result;
        const quality = result.details?.quality?.verdict;
        if (!hostFallbackEligible || (result.details?.ok === true && quality === "complete")) {
          return result;
        }

        purgeHandoffs();
        if (handoffs.size >= handoffCapacity) {
          const details = {
            ...result.details,
            handoff: {
              schema_version: 1, kind: "host_fetch", status: "unavailable",
              execution_success: false, reason: "state_capacity",
            },
          };
          return toolResult(details, result.isError);
        }
        const correlationId = randomUUID();
        const fallbackReason = handoffReason(result);
        const proposal = {
          schema_version: 1,
          kind: "host_fetch",
          status: "recommended",
          execution_success: false,
          correlation_id: correlationId,
          requested_url: params.url,
          fallback_reason: fallbackReason,
          target: { tool: "read", arguments: { path: params.url } },
          hop: 1,
          max_hops: 1,
        };
        handoffs.set(correlationId, {
          createdAt: Date.now(),
          status: "recommended",
          url: params.url,
          maxCharacters: params.max_characters ?? 5000,
          fallbackReason,
        });
        const details = { ...result.details, handoff: proposal };
        return toolResult(details, result.isError);
      },
    });
  }

  pi.registerTool({
    name: "web_search", label: "WRR Web Search",
    description: "Search public web through WRR's shared Jev routing and evidence-quality pipeline, including this session's native search role and fallback chain.",
    parameters: z.object({
      query: z.string().min(1), count,
      limit: count, num_search_results: count,
      sources: z.array(z.string().min(1)).nullable().optional(),
      deadline_ms: z.number().int().min(1).max(300000).nullable().optional(),
    }),
    loadMode: "essential", approval: "read",
    async execute(_id, params, signal, _onUpdate, ctx) {
      // Only this same-name context is allowed to reach the original built-in.
      const nativeSearch = typeof ctx.invokeTool === "function"
        ? (query, nativeSignal) => ctx.invokeTool(query, { signal: nativeSignal }) : null;
      return invokeSearchRpc(pi, {
        ...params, count: params.count ?? params.num_search_results ?? params.limit,
      }, signal, ctx, nativeSearch);
    },
  });

  pi.registerTool({
    name: "wrr_host_fetch_handoff",
    label: "WRR Host Fetch Handoff",
    description: "Accept, reject, or return evidence for a WRR-recommended host-native read. Accept never invokes read; the host must call read itself, then complete with the same correlation ID.",
    parameters: z.object({
      action: z.enum(["accept", "reject", "complete"]),
      correlation_id: z.string().min(36).max(36),
      content: z.string().max(50000).nullable().optional(),
    }),
    loadMode: "essential",
    async execute(_id, params, signal, _onUpdate, ctx) {
      skillHandoff.purge();
      purgeHandoffs();
      const entry = handoffs.get(params.correlation_id);
      if (!entry) return invalidHandoff(params.correlation_id, "Unknown or expired correlation ID.");
      if (params.action === "reject") {
        if (entry.status !== "recommended") {
          return invalidHandoff(params.correlation_id, "Reject requires recommended state.");
        }
        entry.status = "rejected";
        return toolResult({
          operation: "host_fetch_handoff",
          ok: true,
          handoff_status: "rejected",
          execution_success: false,
          correlation_id: params.correlation_id,
        });
      }
      if (params.action === "accept") {
        if (entry.status !== "recommended") {
          return invalidHandoff(params.correlation_id, "Accept requires recommended state.");
        }
        entry.status = "accepted";
        return toolResult({
          operation: "host_fetch_handoff",
          ok: true,
          handoff_status: "accepted",
          execution_success: false,
          correlation_id: params.correlation_id,
          target: { tool: "read", arguments: { path: entry.url } },
          hop: 1,
          max_hops: 1,
        });
      }
      if (entry.status !== "accepted") {
        return invalidHandoff(params.correlation_id, "Complete requires accepted state.");
      }
      if (params.content == null) {
        return invalidHandoff(params.correlation_id, "Complete requires returned content.");
      }
      if (signal?.aborted) {
        entry.status = "cancelled";
        throw new DOMException("WRR host handoff cancelled", "AbortError");
      }
      entry.status = "evaluating";
      const payload = {
        schema_version: 1,
        correlation_id: params.correlation_id,
        url: entry.url,
        executor: "omp:read",
        content: params.content,
        content_kind: "unknown",
        content_type: "text/markdown",
        source_complete: false,
        max_characters: entry.maxCharacters,
        fallback_reason: entry.fallbackReason,
        hop: 1,
        max_hops: 1,
      };
      try {
        const result = await invokeCli(
          ["host-fetch-result"],
          ctx.cwd,
          signal,
          JSON.stringify(payload),
        );
        entry.status = "completed";
        return result;
      } catch (error) {
        entry.status = signal?.aborted ? "cancelled" : "accepted";
        throw error;
      }
    },
  });

  skillHandoff.register(z, tool => pi.registerTool(tool));
}
