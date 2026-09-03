"""Compiling a `.vo` on request, without a second build system.

A warm check writes no `.vo`, and it never will by default: a real compile
costs what the warm check just saved.  But two things ask for one anyway --
`--compile`, for the file that just checked green, and `--rebuild`, for the
dependencies a check found stale -- and both run through here so that the
daemon knows what is in flight.  A check of a dependent that finds a `.vo`
stale because its compile has not finished yet waits for it rather than
refusing.

Rules that keep this honest:

* the `.vo` is written where `make` writes it, by the command `make` runs,
  with the flags from the same `_CoqProject`.  This is the build's own step
  run on the build's behalf, not an alternative to it;
* the finished `.vo` is stamped with the time the compile STARTED, not
  finished.  An edit that lands during the compile then leaves the `.v` newer
  than the `.vo`, and make -- and `staleness` -- rebuild it.  Stamping it with
  the finish time would let a `.vo` of the pre-edit text pass as current;
* `rocq compile` reads the `.v` from disk, so the compiler compiles whatever
  is on disk when it starts -- which is not always the text that was checked
  green.  A job carries the digest of the text it was asked to compile, and
  refuses to run (skips) if the file no longer hashes to it, and discards its
  output if the file changes while it runs.  Otherwise a green check of text A
  followed by an edit to B leaves a `.vo` of B that nothing ever checked, or
  of A that the edit was supposed to supersede -- either way a `.vo` whose
  contents do not match the check that authorised it;
* a job whose source changes underneath it is cancelled, and anything a
  cancelled or failed job wrote is removed.  A truncated `.vo` with a fresh
  mtime looks exactly like a good one to every mtime rule there is;
* a job that fails when the check said OK is a rocq-warm bug and is reported
  as one.
"""

import hashlib
import os
import signal
import subprocess
import sys
import threading
import time

DEFAULT_JOBS = 2

OUTPUT_SUFFIXES = (".vo", ".glob", ".vok", ".vos")


def log(fmt, *args):
    """One line to the daemon's log (its stderr), timestamped."""
    try:
        sys.stderr.write("%s %s\n" % (time.strftime("%H:%M:%S"), fmt % args))
        sys.stderr.flush()
    except Exception:
        pass


def digest_of(path=None, text=None):
    if text is None:
        try:
            with open(path, "rb") as f:
                text = f.read()
        except OSError:
            return None
    return hashlib.sha1(text).hexdigest()


def outputs_of(v):
    stem = v[:-2]
    outs = [stem + s for s in OUTPUT_SUFFIXES]
    outs.append(os.path.join(os.path.dirname(v),
                             "." + os.path.basename(stem) + ".aux"))
    return outs


class Job:
    __slots__ = ("v", "vo", "flags", "cwd", "rocq", "env", "digest", "after",
                 "seq", "state", "why", "submitted", "started", "started_ns",
                 "finished", "proc", "output", "rc")

    def __init__(self, v, flags, cwd, rocq, env, digest, after, seq):
        self.v = v
        self.vo = v[:-2] + ".vo"
        self.flags = list(flags)
        self.cwd, self.rocq, self.env = cwd, rocq, env
        self.digest = digest
        self.after = sorted(set(after))
        self.seq = seq
        # queued running ok failed cancelled skipped(source changed)
        self.state = "queued"
        self.why = ""
        self.submitted = time.time()
        self.started = self.finished = None
        self.started_ns = None
        self.proc = None
        self.output = b""
        self.rc = None

    @property
    def done(self):
        return self.state not in ("queued", "running")

    @property
    def succeeded(self):
        return self.state == "ok"

    def describe(self):
        return {"path": self.v, "state": self.state, "why": self.why,
                "seconds": ((self.finished or time.time()) - self.started
                            if self.started else 0.0),
                "queued": time.time() - self.submitted,
                "rc": self.rc}


class Compiler:
    def __init__(self, jobs=None, available=None, min_free=0):
        if jobs is None:
            env = os.environ.get("ROCQ_WARM_COMPILE_JOBS")
            jobs = int(env) if env not in (None, "") else DEFAULT_JOBS
        self.max_jobs = max(0, jobs)
        self.available = available or (lambda: None)
        self.min_free = min_free
        self.jobs = {}              # abs .v -> latest Job
        self.cv = threading.Condition()
        self._seq = 0
        self._closed = False
        self._thread = None
        if self.max_jobs > 0:
            self._thread = threading.Thread(target=self._loop, daemon=True)
            self._thread.start()

    @property
    def enabled(self):
        return self.max_jobs > 0 and not self._closed

    # ------------------------------------------------------------- submit

    def submit(self, v, flags, cwd, rocq="rocq", env=None, digest=None,
               after=()):
        """Queue a compile of `v`; returns the Job.

        A queued or running job for the same text and flags is reused.  One
        for a different text is cancelled: its `.vo` would describe a file
        that no longer exists.
        """
        v = os.path.abspath(v)
        if digest is None:
            digest = digest_of(v)
        with self.cv:
            cur = self.jobs.get(v)
            if (cur is not None and not cur.done and cur.digest == digest
                    and cur.flags == list(flags) and cur.rocq == rocq):
                if cur.state == "queued":
                    cur.after = sorted(set(cur.after) | set(after))
                return cur
            if cur is not None and not cur.done:
                log("compile: cancelling job %d for %s (text changed)", cur.seq, v)
                self._cancel(cur)
            self._seq += 1
            job = Job(v, flags, cwd, rocq, env, digest, after, self._seq)
            self.jobs[v] = job
            log("compile: job %d queued for %s (digest %s)", job.seq, v,
                (digest or "?")[:8])
            self.cv.notify_all()
            return job

    def cancel(self, v):
        with self.cv:
            cur = self.jobs.get(os.path.abspath(v))
            if cur is not None and not cur.done:
                log("compile: cancelling job %d for %s", cur.seq, v)
                self._cancel(cur)

    def _cancel(self, job):
        job.state = "cancelled"
        job.finished = time.time()
        if job.proc is not None:
            try:
                os.killpg(os.getpgid(job.proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass
        self.cv.notify_all()

    def pending(self, vo):
        """The unfinished job that will produce `vo`, if any."""
        with self.cv:
            job = self.jobs.get(vo[:-3] + ".v")
            return job if job is not None and not job.done else None

    def wait(self, v, timeout=None):
        """Block until the latest job for `v` is done; returns it (or None)."""
        v = os.path.abspath(v)
        deadline = None if timeout is None else time.time() + timeout
        with self.cv:
            while True:
                job = self.jobs.get(v)
                if job is None or job.done:
                    return job
                if deadline is not None and time.time() >= deadline:
                    return job
                self.cv.wait(0.5 if deadline is None
                             else max(0.0, min(0.5, deadline - time.time())))

    def wait_all(self, jobs, timeout=None):
        deadline = None if timeout is None else time.time() + timeout
        for job in jobs:
            left = None if deadline is None else max(0.0, deadline - time.time())
            self.wait(job.v, timeout=left)
        return all(j.done for j in jobs)

    def stop(self):
        with self.cv:
            self._closed = True
            for job in self.jobs.values():
                if not job.done:
                    self._cancel(job)
            self.cv.notify_all()

    def status(self):
        with self.cv:
            return [j.describe() for j in sorted(self.jobs.values(),
                                                 key=lambda j: j.seq)
                    if not j.done or j.state == "failed"]

    def rss_bytes(self):
        total = 0
        with self.cv:
            procs = [j.proc for j in self.jobs.values() if j.proc is not None]
        for p in procs:
            try:
                with open("/proc/%d/statm" % p.pid) as f:
                    total += int(f.read().split()[1]) * os.sysconf("SC_PAGE_SIZE")
            except Exception:
                pass
        return total

    # --------------------------------------------------------------- loop

    def _loop(self):
        while True:
            with self.cv:
                self.cv.wait(1.0)
                if self._closed:
                    return
                running = sum(1 for j in self.jobs.values()
                              if j.state == "running")
                avail = self.available()
                if avail is not None and avail < self.min_free:
                    continue            # the machine needs the room more
                queued = sorted((j for j in self.jobs.values()
                                 if j.state == "queued"), key=lambda j: j.seq)
                for job in queued:
                    if running >= self.max_jobs:
                        break
                    if self._blocked(job):
                        continue
                    job.state = "running"
                    job.started = time.time()
                    job.started_ns = time.time_ns()
                    running += 1
                    threading.Thread(target=self._run, args=(job,),
                                     daemon=True).start()

    def _blocked(self, job):
        """Is `job` waiting on a job for one of the .vo it requires?"""
        for d in job.after:
            dep = self.jobs.get(d[:-3] + ".v")
            if dep is None or dep is job:
                continue
            if not dep.done:
                return True
            if not dep.succeeded:
                job.state = "skipped"
                job.why = "%s was not rebuilt" % dep.v
                job.finished = time.time()
                self.cv.notify_all()
                return True
        return False

    def _run(self, job):
        # rocq compile reads the file from disk.  If it no longer holds the
        # text this job was asked to compile, compiling it would write a .vo
        # of some other text under this job's name; a newer job covers the
        # newer text, so this one steps aside.
        if digest_of(job.v) != job.digest:
            with self.cv:
                job.state = "skipped"
                job.why = "%s changed before its compile started" % job.v
                job.finished = time.time()
                log("compile: job %d skipped for %s (source changed before "
                    "start)", job.seq, job.v)
                self.cv.notify_all()
            return
        argv = [job.rocq, "compile", "-q"] + job.flags + [
            os.path.relpath(job.v, job.cwd)]
        try:
            proc = subprocess.Popen(argv, cwd=job.cwd, env=job.env,
                                    stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT,
                                    start_new_session=True)
        except OSError as e:
            with self.cv:
                job.state, job.why = "failed", "cannot run %s: %s" % (argv[0], e)
                job.output = job.why.encode()
                job.finished = time.time()
                self.cv.notify_all()
            return
        with self.cv:
            if job.state == "cancelled":
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except OSError:
                    pass
            else:
                job.proc = proc
        out, _ = proc.communicate()
        with self.cv:
            job.proc = None
            job.output = out
            job.rc = proc.returncode
            if job.state == "cancelled":
                self._remove_outputs(job)
            elif digest_of(job.v) != job.digest:
                # The file changed WHILE rocq was reading or compiling it, so
                # what landed in the .vo is neither reliably the old text nor
                # reliably the new one.  Throw it away; the newer job is the
                # source of truth.
                self._remove_outputs(job)
                job.state = "skipped"
                job.why = "%s changed during its compile" % job.v
            elif proc.returncode == 0:
                for path in outputs_of(job.v):
                    try:
                        os.utime(path, ns=(job.started_ns, job.started_ns))
                    except OSError:
                        pass
                job.state = "ok"
            else:
                self._remove_outputs(job)
                job.state = "failed"
                job.why = "rocq compile exited with %s" % proc.returncode
            job.finished = time.time()
            log("compile: job %d %s for %s (exit %s, %.1fs)", job.seq, job.state,
                job.v, proc.returncode, job.finished - job.started)
            self.cv.notify_all()

    @staticmethod
    def _remove_outputs(job):
        """Drop whatever this job wrote: a partial `.vo` with a fresh mtime is
        indistinguishable from a good one to every rule that reads mtimes."""
        for path in outputs_of(job.v):
            try:
                if os.stat(path).st_mtime_ns >= job.started_ns:
                    os.unlink(path)
            except OSError:
                pass
