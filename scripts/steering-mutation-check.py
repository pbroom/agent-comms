"""The mutation check of Claude steering (step 3): one change per row, applied alone to an EXPORT of the branch
(never a working tree), the steering tests run first and, only if they did not catch it, the wider tests; the file
restored after each. The rows are review rounds 2 to 10's (DESIGN_NOTES "Mid-turn steering, step 3", each round's
"Mutation check") and the enabling PR's ("Enabling Claude steering"): each is (file, name, the exact text, what replaces it); a row whose text is no longer there once
is reported STALE.

    git archive HEAD | tar -x -C /tmp/export
    python scripts/steering-mutation-check.py /tmp/export [ROW|ROW...] > results.txt

scripts/steering-mutation-results.txt is the last run's output (and which commit it ran on)."""
import re
import subprocess
import sys
from pathlib import Path

FILES = {"A": "agent_comms/providers/claude.py", "H": "agent_comms/session_host.py", "ST": "agent_comms/hosted_steer.py",
         "HS": "agent_comms/hosted.py", "NS": "agent_comms/node_sessions.py", "API": "agent_comms/api.py",
         "G": "agent_comms/node_groups.py", "D": "agent_comms/dispatch.py",
         "L": "agent_comms/hostlog.py", "N": "agent_comms/node.py"}
ROWS = [('A', "F init naming S in N's bracket is a contradiction", '                if names_s:\n                    self._contradict("an init naming the steer inside the turn\'s own bracket")', '                if False:\n                    self._contradict("an init naming the steer inside the turn\'s own bracket")'),
    ('A', "F S's init: no origin", 'exact = shaped and listed == [s_] and scalar is None and quiet', 'exact = shaped and listed == [s_] and scalar is None'),
    ('A', "F S's init: no scalar", 'exact = shaped and listed == [s_] and scalar is None and quiet', 'exact = shaped and listed == [s_] and quiet'),
    ('A', 'F rule 10: no echo field at all', '            if (ev.get("is_error") is True and "user_message_uuid" not in ev and "user_message_uuids" not in ev', '            if (ev.get("is_error") is True and not _named(ev)'),
    ('A', 'F a result naming N in the hold is a contradiction', '        if names_n:\n            self._contradict("a result naming the turn whose end is held")\n            return True, "other"', '        if False:\n            return True, "other"'),
    ('A', 'F consumed: the measured origin', '            measured = quiet if self._s_named else (isinstance(origin, dict)\n                                                     and origin.get("kind") == "task-notification")', '            measured = True'),
    ('A', 'F echo shape: the scalar is the first', '        ok = bool(listed) and listed[0] == scalar\n', '        ok = True\n'),
    ('A', 'F echo shape: strings only', 'isinstance(listed, list) and all(isinstance(u, str) for u in listed))) and (', 'isinstance(listed, list))) and ('),
    ('H', 'G expiry not masked by an omission', '            self._expire(now)\n            return self.expired', '            self._expire(now)\n            return self.revoked == "contact"'),
    ('H', "G the watchdog's boundary cuts a full hold", '                elif s.held is not None:\n                    # (Review round 2, ruling G)', '                elif False:\n                    # (Review round 2, ruling G)'),
    ('H', 'H the adjudicator publishes outside the lock', '                retired = self._retire_hold(s, {"outcome": "next_turn", "why": "own_turn", "result": {', '                retired = self._publish_hold(s, out, {"outcome": "next_turn", "why": "own_turn", "result": {'),
    ('H', 'H stop_all saves after every SIGTERM', "        self._at_once = self._defer_save = True     # a held end is published here, at main's point (shutdown simplified)\n        try:\n            for s in live:", '        self._at_once = True\n        try:\n            for s in live:'),
    ('H', "H the retirement inside _steer_end's lock", '            done = getattr(s.provider, "steer_done", None)\n            if done is not None:\n                done()      # (Claude: the attempt is retired before any further frame is read; ruling H: atomically)\n        if slot.mark is None or s.child is None:', '        if slot.mark is None or s.child is None:'),
    ('H', 'I the outbox survives an exception', '            self._tick(self.clock(), self._outbox)\n        finally:\n            self._in_tick = False', '            self._tick(self.clock(), self._outbox)\n        finally:\n            self._in_tick = False\n            self._outbox = []'),
    ('H', 'I a deadline error publishes at once', '            log.exception("session %s: could not publish a held end at its deadline", s.session_id)\n            self._publish_now(s, out)', '            log.exception("session %s: could not publish a held end at its deadline", s.session_id)'),
    ('H', "I a host error's end is at once", '                self._at_once = True\n                try:\n                    self._end(s, f"host error', '                try:\n                    self._end(s, f"host error'),
    ('H', 'I a host error publishes in its finally', '        finally:\n            self._publish_now(s, out)\n', '        finally:\n            pass\n'),
    ('H', 'I the deadline first in every tick', '            for s in list(self.sessions.values()):\n                if s.held is not None:\n                    self._hold_deadline(s, now, out)', '            for s in []:\n                if s.held is not None:\n                    self._hold_deadline(s, now, out)'),
    ('ST', 'J the attempt records its capability', '         "holds": "held" in (r.get("modes") or [])}', '         "holds": True}'),
    ('ST', 'J a checkpoint needs the capability', '    if s is not None and not holds(s):\n        raise Conflict', '    if False:\n        raise Conflict'),
    ('ST', 'J next_turn needs the capability', '    if ext is not None and ext.get("outcome") == "next_turn" and not holds(s):', '    if False:'),
    ('H', 'J the write gate asks the capability', 'and (not fence or (not slot.fenced and (self.steer_capable is None or self.steer_capable(s))))', 'and (not fence or not slot.fenced)'),
    ('NS', "J the capability is never the attempt's authority", '        return not self._silent() and not self.stopping.is_set()\n\n    def _steer_capable', '        return not self._silent() and not self.stopping.is_set() and self._steer_capable(s)\n\n    def _steer_capable'),
    ('NS', "J the listing's offer needs held", '            self._steer_listed(sid, view, fetch_sent, steer_aware and (held_aware or not self.host.holds(sid)))', '            self._steer_listed(sid, view, fetch_sent, steer_aware)'),
    ('A', 'J the adapter says held', '    holds = _Steers()', '    holds = False'),
    ('NS', 'K checkpoints go to the sender', '            if self.checkpoint_thread is not None:\n                with self.checkpoint_cond:', '            if False:\n                with self.checkpoint_cond:'),
    ('API', 'K a checkpoint is no contact', '        if body.held is None:\n            # whatever path', '        if True:\n            # whatever path'),
    ('HS', 'S a mixed checkpoint report is refused', '    if held is not None and (events or any(x is not None for x in (', '    if False and (events or any(x is not None for x in ('),
    ('ST', 'S impossible next_turn shapes', '        if result is None or "message" in ev["result"] or not possible_next_turn(ev["why"], result):', '        if result is None:'),
    ('NS', 'S valid Unicode', '        return v.encode("utf-8", "replace").decode("utf-8") if not v.isascii() else v', '        return v'),
    ('ST', 'S a stale checkpoint after both settled', '        else:\n            _clear_held(c, r["session_id"], s.get("into"), now)     # (shoulds) both settled by an older hub', ''),
    ('H', 'S the grace from the SIGTERM', '                if s.held is not None and s.held.fenced_at is not None:\n                    s.held.fenced_at = self.clock()     # (shoulds) the bounded publication counts from the SIGTERM', ''),
    ('H', "S S's clock from its read", '                held.s_turn, s.busy_since = True, self.clock()', '                held.s_turn, s.busy_since = True, now'),
    ('A', 'L n_unproved downgrades S', '                        self._evidence = "n_unproved"      # N\'s result stands; S `unconfirmed`', '                        pass'),
    ('A', 'L s_end needs the measured terminal', '                if (shaped and quiet and listed == [s_] and scalar == s_ and _terminal(ev)\n                        and self._verified is True):', '                if (shaped and quiet and listed == [s_] and scalar == s_\n                        and self._verified is True):'),
    ('A', 'L consumed needs the measured terminal', '                    and _terminal(ev) and self._verified is True):\n                self._evidence = "s_consumed"', '                    and self._verified is True):\n                self._evidence = "s_consumed"'),
    ('A', 'L terminal: one turn or more', 'and isinstance(turns, int) and not isinstance(turns, bool) and turns >= 1)', 'and isinstance(turns, int) and not isinstance(turns, bool) and turns >= 0)'),
    ('A', 'L no origin key, not a null one', '        quiet = "origin" not in ev          # the measured origin of the host\'s lines: no `origin` key at all', '        quiet = ev.get("origin") is None'),
    ('A', 'L no eviction: the bound refuses', '        if len(self._retired) >= RETIRED_MAX:\n', '        if False:\n'),
    ('A', 'L the whole echo list is read', 'v if isinstance(v, list) else []):     # (whole: ruling L)', 'v[:MAX_ECHO] if isinstance(v, list) else []):'),
    ('A', "L an init naming neither in S's turn is no loss", '                if names_s or names_n:\n                    self._contradict("an init naming the steer or the held turn inside the steer\'s own turn")', '                if True:\n                    self._contradict("an init naming the steer or the held turn inside the steer\'s own turn")'),
    ('A', 'L the live attempt first', '        if not live and named & self._retired and', '        if named & self._retired and'),
    ('A', 'L the three-outcome rule per bracket', '        if self._bracket != UNATTRIBUTED:\n            self._s_named = False', '        if False:\n            self._s_named = False'),
    ('A', 'L retired S checked first', '        if not live and named & self._retired and (self._current is None or self._current not in named):', '        if False:'),
    ('A', 'L retired kept at retirement', '            self._retired.add(a["s"])       # (never evicted', '            pass       # (never evicted'),
    ('A', 'L n_with_s needs the verified incarnation', '                    measured = shaped and quiet and _terminal(ev) and self._verified is True', '                    measured = shaped and quiet and _terminal(ev)'),
    ('A', 'L n_with_s needs no origin', '                    measured = shaped and quiet and _terminal(ev) and self._verified is True', '                    measured = shaped and _terminal(ev) and self._verified is True'),
    ('A', 'L s_end needs no origin', '                if (shaped and quiet and listed == [s_] and scalar == s_ and _terminal(ev)\n                        and self._verified is True):', '                if (shaped and listed == [s_] and scalar == s_ and _terminal(ev)\n                        and self._verified is True):'),
    ('A', 'L N named without its identity is a contradiction', '                self._contradict("a result naming the turn in flight without its identity")\n                return True, "other"', '                return False, None'),
    ('H', 'M forget purges', '        self._outbox.purge(session_id)      # (ruling M) nothing of it is delivered once it is forgotten', '        pass'),
    ('H', 'M the incarnation tag', '        return [e for e, g in zip(self, self._gens) if g is None or g == self._gen(e)]', '        return [e for e, g in zip(self, self._gens)]'),
    ('H', 'M the held end before its log line', '        out.append(dataclasses.replace(held.end, steer=dict(steer)))\n        out.extend(held.after)      # (ruling M', '        log.info("session %s: the steer into message %s: %s (%s)", s.session_id, held.end.message_id,\n                 steer["outcome"], steer["why"])\n        out.append(dataclasses.replace(held.end, steer=dict(steer)))\n        out.extend(held.after)      # (ruling M'),
    ('H', 'N stop_turns per session', '                    try:        # (ruling N) one session\'s fault never skips the others\n                        self._fail(s, detail[:300], out)\n                        self._stop(s, now)\n                    except Exception:\n                        log.exception("session %s: could not stop its turn", s.session_id)', '                    self._fail(s, detail[:300], out)\n                    self._stop(s, now)'),
    ('H', 'N stop_all per session', '                try:        # (ruling N) one session\'s fault never skips the others\n                    self._fail(s, "the session host shut down", out)\n                    self._stop(s, now)\n                except Exception:\n                    log.exception("session %s: could not stop it at shutdown", s.session_id)', '                self._fail(s, "the session host shut down", out)\n                self._stop(s, now)'),
    ('H', 'N the watchdog publishes a due fence', '                    if s.held is not None:\n                        self._hold_deadline(s, self.clock(), out)', '                    if False:\n                        self._hold_deadline(s, self.clock(), out)'),
    ('NS', "O held's change resends the modes", '              or (("held" in features) != ("held" in self.hub_features))):', '              ):'),
    ('HS', "O next_turn only with N's answered end", '                and ev.get("answered") is not True):\n            # (Review round 3, ruling O)', '                and False):\n            # (Review round 3, ruling O)'),
    ('NS', 'P the sender before the driving thread', '        self.start_checkpoint_sender()      # (round 3, ruling P) before the driving thread: no inline window\n', ''),
    ('NS', 'P never once shutdown began', '        if self.stopping.is_set():\n            return False        # (ruling P) never once shutdown began', '        if False:\n            return False        # (ruling P) never once shutdown began'),
    ('H', "parity a revoked Codex attempt's contact", ' or (self.revoked is not None and not self.holds)', ''),
    ('H', 'parity a Claude slot holds', '            slot.holds = getattr(s.provider, "steer_named", None) is not None', '            slot.holds = False'),
    ('A', 'Q the terminal check is total', '    return (isinstance(sub, str) and sub in MEASURED_TERMINALS and', '    return (sub in MEASURED_TERMINALS and'),
    ('A', "Q N's identity before the cap", '                and self._current == a["n"] and _identifies(ev, a["n"])):', '                and False):'),
    ('A', "Q rule 10 only while S's init is the last", 'and _terminal(ev) and self._s_init_last):', 'and _terminal(ev)):'),
    ('A', 'Q no steering advertised at RETIRED_MAX', ' and len(obj._retired) < RETIRED_MAX', ''),
    ('NS', 'R a rejection forgets no feature', '                                    spare=not last)', '                                    spare=False)'),
    ('NS', 'R a fallback asks for a fresh listing', '                self.last_sync = float("-inf")      # (ruling R) a fresh listing says what the hub takes now', '                pass'),
    ('NS', 'R the write gate asks the pair', '        return not self.host.holds(s.session_id) or (self._pair(self.hub_features) and self.features_known)', '        return not self.host.holds(s.session_id) or ("held" in self.hub_features and self.features_known)'),
    ('NS', 'R the checkpoint gate asks the pair', '        if not (self._pair(self.hub_features) and self.features_known):\n            # (Review round 1, ruling C)', '        if not ("held" in self.hub_features and self.features_known):\n            # (Review round 1, ruling C)'),
    ('H', "S a self-stop's own events kept on a fault", '                self._outbox.extend(mine.current())\n', '                pass\n'),
    ('H', "T a Claude steering session is the bulk stop's after the latch", '        return s.held is not None or (s.steer is not None and s.steer.outcome is None and s.steer.holds)', '        return False'),
    ('H', 'T the claim checks the latch (re-anchored)', '            if self.steer_stopped or (s.steer is not None and s.steer.stopping):\n                return False\n            self._claimer = threading.get_ident()\n            try:\n                decide()', '            self._claimer = threading.get_ident()\n            try:\n                decide()'),
    ('H', 'T no log handler under the cutoff lock (successor: a host lock)', '        self._steer_stop_lock = hostlog.HostLock(threading.RLock())', '        self._steer_stop_lock = threading.RLock()'),
    ('H', 'T nothing saved under the cutoff lock (re-anchored)', '            if self._claimer == threading.get_ident():\n                self._save_due = True', '            if False:\n                self._save_due = True'),
    ('H', 'T a host error decides nothing new after the latch', '        if held.decided is None and self.steer_stopped and not self._at_once:', '        if False:'),
    ('H', 'T the under-lock latch check in the hold cut', '            if self.steer_stopped or (slot is not None and slot.stopping):\n                return\n            why = None', '            why = None'),
    ('H', "T stop_all's steps apart", '                for step in steps:\n                    try:\n                        step()\n                    except Exception:', '                for step in steps:\n                    if True:\n                        step()\n                    if False:'),
    ('NS', "T the watchdog's steps apart", '            except Exception:\n                log.exception("hosted sessions: the self-stop failed")', '            except ZeroDivisionError:\n                log.exception("hosted sessions: the self-stop failed")'),
    ('H', "T no save on the watchdog's boundary", "        self._defer_save = True     # (ruling T) the watchdog's path persists nothing", "        self._defer_save = False     # (ruling T) the watchdog's path persists nothing"),
    ('NS', "U the entry's own generation", ' and s.gen == entry.gen\n', '\n'),
    ('H', 'T a reap with survivors claimed', '                if self._steering(s):\n                    if not self._claim(s, lambda: self._went_by_itself', '                if False:\n                    if not self._claim(s, lambda: self._went_by_itself'),
    ('H', "T no save on the watchdog's cut", "        if self._defer_save:\n            self._save_due = True       # (ruling T) the watchdog's boundary persists nothing", "        if False:\n            self._save_due = True       # (ruling T) the watchdog's boundary persists nothing"),
    ('A', "Q7 any init clears rule 10's marker first", '            self._s_init_last = False\n        named = _named(ev)', '            pass\n        named = _named(ev)'),
    ('NS', "Y the acknowledged modes are the accepted rung's", '                    self.acked_modes[sid] = list(taken["modes"])    # (ruling Y) what the hub acknowledged', '                    self.acked_modes[sid] = list(body["modes"])    # (ruling Y) what the hub acknowledged'),
    ('NS', 'Y an explicit report corrects modes acknowledged with less', '                    set(self.acked_modes[sid]) < set(self._modes_for(st[4], features))):', '                    False):'),
    ('NS', "Y rung (iii) is main's plain retry of rung (ii)", '        three = {k: v for k, v in two.items() if k != "events"} | {', '        three = {k: v for k, v in two.items() if k not in ("events", "modes")} | {"modes": [m for m in two.get("modes", []) if m != "steer"]} | {'),
    ('NS', 'X a second put of one identity is a no-op', '        if e.ident is not None and e.ident in self.receipts:\n            return\n        if e.kind == "held":', '        if False:\n            return\n        if e.kind == "held":'),
    ('NS', "X the receipt is forgotten only after the source's removal", '            if e.ident is not None:\n                self.receipts.discard(e.ident)\n\n    def _dispatch', '            if e.ident is not None:\n                pass\n\n    def _dispatch'),
    ('NS', 'X publication order across lanes', '            _, src, e = min(heads, key=lambda h: (h[0], h[1]))', '            _, src, e = min(heads, key=lambda h: h[1])'),
    ('NS', 'X the host keeps what it published until the node took it', '        self.host.keep_outbox = True', '        self.host.keep_outbox = False'),
    ('H', 'X the self-stop puts its own events first, also when it raises', '                if into is not None:\n                    into.extend(mine.current())\n                elif not done:\n                    self._outbox.extend(mine.current())     # (raised: kept behind what the outbox held)', '                if into is not None and done:\n                    into.extend(mine.current())\n                elif not done and into is None:\n                    self._outbox.extend(mine.current())     # (raised: kept behind what the outbox held)'),
    ('H', 'X the boundary puts its own events into watched_steer', '                if into is not None:\n                    into.extend(mine.current())\n                else:\n                    self._outbox.extend(mine.current())\n        return mine.current() if into is not None else self._take_outbox()', '                if into is not None:\n                    self._outbox.extend(mine.current())\n                else:\n                    self._outbox.extend(mine.current())\n        return mine.current() if into is not None else self._take_outbox()'),
    ('H', "W no save on the watchdog's thread", "        if on_watch():\n            # (Review round 5, ruling W) never on the watchdog's thread", "        if False:\n            # (Review round 5, ruling W) never on the watchdog's thread"),
    ('H', "W no transcript write on the watchdog's thread", '        if on_watch():\n            s.tee_pending.append(line)', '        if False:\n            s.tee_pending.append(line)'),
    ('H', 'W no Claude partial cut on the watchdog', '                        and not (watchdog and slot.holds) and not self._steer_gate(s, slot, now)):', '                        and not self._steer_gate(s, slot, now)):'),
    ('H', "W no drain in the watchdog's hold cut", '                    self._hold_cut(s, self.clock(), out, drain=not watchdog)', '                    self._hold_cut(s, self.clock(), out, drain=True)'),
    ('H', 'W the self-stop never saves', '                if mine:\n                    self._save_due = True\n            elif out:', '                if mine:\n                    self._save_state()\n            elif out:'),
    ('H', "Z a survivor's registration is no SIGTERM", '                s.parked = True\n                self.groups.stopping(s.group_token)\n                return', '                s.parked = True\n                s.stopping_since = now\n                self.groups.stopping(s.group_token)\n                return'),
    ('H', "Z a parked session stays the bulk stop's", 'and self.steer_stopped and (s.parked or self._steering(s)):', 'and self.steer_stopped and self._steering(s):'),
    ('NS', 'S the self-stop takes only its own', 'self.host.stop_turns(self._self_stop_detail(), own=True, into=self.watched)', 'self.host.stop_turns(self._self_stop_detail(), own=False, into=self.watched)'),
    ('NS', "N1 the self-stop and kills under main's unbounded lock", '        with self.lock:\n            if self.stopping.is_set():\n                return          # shutdown stops everything itself\n            now = self.clock()\n            allowed = self.watch_seconds + LOCK_SLACK_SECONDS\n            due = self._self_stop_due()', '        if True:\n            if self.stopping.is_set():\n                return          # shutdown stops everything itself\n            now = self.clock()\n            allowed = self.watch_seconds + LOCK_SLACK_SECONDS\n            due = self._self_stop_due()'),
    ('H', 'N2 the bulk signal pass before any transcript', '            for s in live:\n                try:\n                    if going(s):\n                        s.child.kill()\n                except Exception:\n                    failed.add(s.session_id)\n                    log.exception("session %s: its SIGKILL at shutdown failed", s.session_id)', '            for s in live:\n                self._flush_tee(s)\n                try:\n                    if going(s):\n                        s.child.kill()\n                except Exception:\n                    failed.add(s.session_id)\n                    log.exception("session %s: its SIGKILL at shutdown failed", s.session_id)'),
    ('L', 'W2 a lifecycle thread never waits (the predicate)', '    return getattr(_local, "depth", 0) == 0 and threading.get_ident() not in ENQUEUE_ONLY', '    return getattr(_local, "depth", 0) == 0'),
    ('NS', "Y main's literal legacy rung", '                if mains and last and refused is not None and refused[1] == 422', '                if refused is not None and refused[1] == 422'),
    ('NS', 'X the receipt with its entry, undone together', '        try:\n            self.receipts.add(ident)        # (the receipt and the queue entry, together)\n        except BaseException:\n            undo()\n            raise', '        pass'),
    ('NS', 'X a checkpoint commits with its receipt', '            self._checkpoint(e, e.ident)    # (Claude steering', '            self._checkpoint(e)    # (Claude steering'),
    ('NS', 'X the final handoff tried once more', '            for attempt in (1, 2):\n', '            for attempt in (2,):\n'),
    ('NS', 'R rung (ii) keeps a Codex extension', '                                                              and v.get("outcome") == "next_turn")}', '                                                              )}'),
    ('NS', 'R rung (ii) removes the Claude pair', '            out["modes"] = [m for m in body["modes"] if not (m == "held" or (claude and m == "steer"))]', '            out["modes"] = list(body["modes"])'),
    ('NS', "Y main's forget only after a 422", 'refused is not None and refused[1] == 422 and gen == self.features_gen:', 'refused is not None and gen == self.features_gen:'),
    ('H', 'Z a failed step keeps a live group owned', '                if failed_here and going(s):', '                if False:'),
    ('NS', 'R7-1 the steer deadlines inside the one section', '            try:\n                self._steer_watch(now)\n                self.host.steer_deadlines(into=self.watched_steer, watchdog=True)\n            except Exception:\n                log.exception("hosted sessions: the steer deadlines failed")\n', '        if not self.lock.acquire(timeout=self.watch_seconds):\n            return\n        try:\n            self._steer_watch(now)\n            self.host.steer_deadlines(into=self.watched_steer, watchdog=True)\n        except Exception:\n            log.exception("hosted sessions: the steer deadlines failed")\n        finally:\n            self.lock.release()\n'),
    ('L', 'R7-2 stop_all is an entry point (successor)', '        if not name.startswith("_") and type(value) is type(entries)', '        if not name.startswith("_") and name != "stop_all" and type(value) is type(entries)'),
    ('L', "A3 the exception's type only (successor)", '    return _type_name(value), tuple(frames)', '    return f"{_type_name(value)}: {value}", tuple(frames)'),
    ('H', "T stop_all's final save in a finally (re-anchored)", '        finally:\n            self._save_state()\n        return self._take_outbox()', '        finally:\n            pass\n        self._save_state()\n        return self._take_outbox()'),
    ('H', "D1 a raising SIGTERM keeps main's clock", '                s.child.terminate()\n\n    def _release_steer', '                try:\n                    s.child.terminate()\n                except BaseException:\n                    s.stopping_since = None\n                    raise\n\n    def _release_steer'),
    ('H', 'E2 a registry miss is not a refusal (re-anchored)', '            if got == self.groups.BUSY:\n', '            if got in (self.groups.BUSY, self.groups.MISSING):\n'),
    ('G', 'D3 a barrier there before is ALREADY', '                    if groups[token][0] == self.STOPPING:\n                        return self.ALREADY\n', ''),
    ('A', 'F the adapter steers only with the switch on', '        return (obj.steering_on and obj.verified', '        return (obj.verified'),
    ('H', 'F the host sets the switch on each adapter', '            s.provider.steering_on = self.claude_steer()', '            s.provider.steering_on = True'),
    ('H', 'F the one place a hold is made asks the switch', '            if steer is HOLD and not self.claude_steer():', '            if False:'),
    ('H', 'C1 registry busy: no stop committed (re-anchored)', '            if got == self.groups.BUSY:\n                return False', '            if got == self.groups.BUSY:\n                pass'),
    ('H', "S1 the hold's causes reserve only with a held end", '        if cause in (CAUSE_HOLD_CUT, CAUSE_TIMEOUT, CAUSE_CANCEL):\n            return s.held is not None', '        if cause in (CAUSE_HOLD_CUT, CAUSE_TIMEOUT, CAUSE_CANCEL):\n            return True'),
    ('H', 'S1 steer bytes reserve only for a Claude attempt', '        if cause == CAUSE_STEER_BYTES:\n            return s.steer is not None and s.steer.holds', '        if cause == CAUSE_STEER_BYTES:\n            return s.steer is not None'),
    ('H', "S1 any other cause is main's", '            return s.steer is not None and s.steer.holds\n        return False', '            return s.steer is not None and s.steer.holds\n        return True'),
    ('H', 'S1 a held cancel reserves', '            if self._reserves(s, CAUSE_CANCEL):', '            if False:'),
    ('H', "S1 a steer frame's broken pipe reserves", '            if steered is not None and self._reserves(s, CAUSE_STEER_BYTES):', '            if False:'),
    ('H', "S1 a partial frame's cut reserves", "        if self._reserves(s, CAUSE_STEER_BYTES):\n            # (Enabling PR, rulings S1 and S2) A Claude attempt's frame", "        if False:\n            # (Enabling PR, rulings S1 and S2) A Claude attempt's frame"),
    ('H', "S1 the held timeout's cause", '            self._cutoff_stop(s, detail, now, out, live=live, cause=CAUSE_TIMEOUT)', '            self._cutoff_stop(s, detail, now, out, live=live, cause=CAUSE_WRITE)'),
    ('H', "S1 the hold cut's cause", 'cause=CAUSE_HOLD_CUT)', 'cause=CAUSE_WRITE)'),
    ('H', 'S2 busy commits nothing (re-anchored)', '            if got == self.groups.BUSY:\n                return False\n            if fresh', '            if fresh'),
    ('H', 'S2 escalation armed before the commit (re-anchored)', '            try:\n                self._stop(s, now, at_signal=True, reserved=True)\n            finally:\n                self._claimer = threading.get_ident()\n                try:\n                    commit()\n                finally:\n                    self._claimer = None', '            self._claimer = threading.get_ident()\n            try:\n                commit()\n            finally:\n                self._claimer = None\n            self._stop(s, now, at_signal=True, reserved=True)'),
    ('H', 'S2 the partial cut clears the buffer only after the reservation', '            if self.steer_stopped:\n                return\n            now = self._drain_before_stop(s, now, out)\n\n            def commit() -> None:', '            if self.steer_stopped:\n                return\n            s.outbuf.clear()\n            now = self._drain_before_stop(s, now, out)\n\n            def commit() -> None:'),
    ('H', 'S2 the steer pipe clears the buffer only after the reservation', '                if self.live_attempt(s):\n                    now = self._drain_before_stop(s, now, out)\n                self._reserved_stop(', '                s.outbuf.clear()\n                if self.live_attempt(s):\n                    now = self._drain_before_stop(s, now, out)\n                self._reserved_stop('),
    ('H', 'S3 a live child with no token gets one (re-anchored)', '            if fresh and got == self.groups.RESERVED:\n                s.group_token = token', '            if False:\n                s.group_token = token'),
    ('G', 'S3 the registry inserts the barrier on a miss', '            if barrier is not None:\n                k, cwd = barrier', '            if False:\n                k, cwd = barrier'),
    ('D', 'S4 on by default', '    claude_steer: bool = True', '    claude_steer: bool = False'),
    ('NS', "S5c the watchdog's lane waits during reconciliation", '        self._deliver(late=not self.reconciling)', '        self._deliver(late=True)'),
    ('NS', 'S5c the phase is marked', '        self.reconciling = True\n        try:\n            self._reconcile_listing', '        self.reconciling = False\n        try:\n            self._reconcile_listing'),
    ('H', "R1 the claim runs the route's own stop", '        try:\n            stop()\n        except BaseException:\n            if first is None:', '        try:\n            pass\n        except BaseException:\n            if first is None:'),
    ('H', 'A1 the stop runs whatever the decision raised', '            except BaseException as e:      # (A1: the claim is made; its stop still runs)\n                first = e', '            except BaseException as e:      # (A1: the claim is made; its stop still runs)\n                raise'),
    ('H', 'A1 the first exception propagates (re-anchored)', '        except BaseException:\n            if first is None:\n                raise\n        if first is not None:', '        except BaseException:\n            raise\n        if first is not None:'),
    ('NS', 'A2 the runner lock is a host lock (successor)', '        self.lock = hostlog.HostLock(threading.Lock())', '        self.lock = threading.Lock()'),
    ('G', 'A2 the registry lock is a host lock (successor)', '        self.lock = hostlog.HostLock(threading.RLock())', '        self.lock = threading.RLock()'),
    ('H', "A2 the host's public methods are entry points (successor)", '@hostlog.entries       # (host-side logging)', '#hostlog.entries       # (host-side logging)'),
    ('H', 'A3 the key cached at spawn', '            s.group_token, s.group_key = token, k', '            s.group_token = token'),
    ('H', 'A3 the key cleared at the detach', '        self.groups.gone(s.group_token)      # the whole group is gone\n        s.group_token = s.group_key = None', '        self.groups.gone(s.group_token)      # the whole group is gone\n        s.group_token = None'),
    ('H', "A3 a live child with no key is main's", '        if s.group_key is None:\n            # (Review round 1, A3)', '        if False:\n            # (Review round 1, A3)'),
    ('H', 'R2 the barrier is under the cached key', '        barrier = (s.group_key, s.cwd) if alive else None', '        barrier = (node_groups.key(s.cwd), s.cwd) if alive else None'),
    ('H', 'S3 a verified exit needs no entry (re-anchored)', '        barrier = (s.group_key, s.cwd) if alive else None', '        barrier = (s.group_key, s.cwd)'),
    ('H', 'S3 a live miss gets a barrier (re-anchored)', '        barrier = (s.group_key, s.cwd) if alive else None', '        barrier = None'),
    ('H', "S1 main's route of a steering session is claimed (re-anchored)", '        if self._steering(s):\n            return self._claim(s, lambda: self._fail(s, why, out), lambda: self._stop_from(s, now, live))\n        self._fail(s, why, out)', '        self._fail(s, why, out)'),
    ('H', 'S1 the switch (re-anchored)', '        if not (self.claude_steer() and getattr(s.provider, "steering_on", False) is True):\n            return False\n        if s.group_key is None:', '        if not (getattr(s.provider, "steering_on", False) is True):\n            return False\n        if s.group_key is None:'),
    ('NS', "A1 a close's session's earlier watched events go first", '            if not late and e.kind == "ended" and e.ident is not None:', '            if False:'),
    ('NS', "A1 only that session incarnation's", '                           and w.ident[:2] == e.ident[:2] and w.ident[2] < e.ident[2]), None)', '                           and w.ident[2] < e.ident[2]), None)'),
    ('H', 'A3 the reap settles an armed hold whose decision never ran', '        if s.held is not None and (s.held.fenced_at is not None or s.stopping_since is not None):', '        if s.held is not None and s.held.fenced_at is not None:'),
    ('H', 'A3 an undecided hold is finalized at the reap', '            if s.held.decided is None:\n                self._finalize_hold(s, "the process was stopped", out)', '            if False:\n                self._finalize_hold(s, "the process was stopped", out)'),
    ('NS', 'A1 only earlier watched events', '                           and w.ident[:2] == e.ident[:2] and w.ident[2] < e.ident[2]), None)', '                           and w.ident[:2] == e.ident[:2]), None)'),
    ('NS', 'A1 the generation counts', '                           and w.ident[:2] == e.ident[:2] and w.ident[2] < e.ident[2]), None)', '                           and w.ident[0] == e.ident[0] and w.ident[2] < e.ident[2]), None)'),
    ('L', 'W3 nobody waits inside a boundary (the predicate)', '    return getattr(_local, "depth", 0) == 0 and threading.get_ident() not in ENQUEUE_ONLY', '    return threading.get_ident() not in ENQUEUE_ONLY'),
    ('L', 'L the level is checked at the call', '        if self.logger.isEnabledFor(level):\n            with BOUNDARY:', '        if True:\n            with BOUNDARY:'),
    ('L', 'L the arguments are reduced at the call', '        r.args = _reduce_args(args)', '        r.args = args'),
    ('L', "L the record's caller is the call's", '    fn, ln, func = r.caller\n', '    fn, ln, func = "(hostlog)", 0, "drain"\n'),
    ('L', "L the record's time is the call's", '    rec.created = r.created\n', ''),
    ('L', 'L a host lock enters a boundary', '    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:\n        BOUNDARY.__enter__()\n', '    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:\n'),
    ('NS', 'B8 the promoted event is taken from where it is', '                self.watched.pop(at)', '                self.watched.pop(0)'),
    ('NS', 'B8 only an ended promotes', '            if not late and e.kind == "ended" and e.ident is not None:', '            if not late and e.ident is not None:'),
    ('NS', 'R1 the report lock is a boundary', '        self.report_lock = hostlog.HostLock(threading.Lock())', '        self.report_lock = threading.Lock()'),
    ('NS', 'R1 the ack lock is a boundary', '        self.ack_lock = hostlog.HostLock(threading.RLock())', '        self.ack_lock = threading.RLock()'),
    ('H', "R1 a steer attempt's record lock is a boundary", 'field(default_factory=lambda: hostlog.HostLock(threading.RLock())', 'field(default_factory=threading.RLock'),
    ('NS', 'R2 the checkpoint sender is enqueue-only', '        with hostlog.enqueue_only():\n            self._checkpoint_loop()', '        if True:\n            self._checkpoint_loop()'),
    ('NS', 'R2 the watchdog is enqueue-only', '            with hostlog.enqueue_only():        # (review round 1, R2)', '            with __import__("contextlib").nullcontext():        # (review round 1, R2)'),
    ('L', 'R3 facts are rendered by the writer', '    return _Later(_render_facts, (_type_name(exc), status, errno, returncode))', '    return _render_facts(_type_name(exc), status, errno, returncode)'),
    ('L', "R3 an errno is read with no code of the exception's", '            errno = _int(OSError.__dict__["errno"].__get__(exc))', '            errno = _int(exc.errno)'),
    ('A', 'R4 the version is rendered by the writer', 'hostlog.later(_shown, version[:VERSION_SHOWN], len(version)) if isinstance(version, str)', 'shown_version(version) if isinstance(version, str)'),
    ('L', 'R7 a CalledProcessError subclass runs no code', '        if t is subprocess.CalledProcessError:', '        if issubclass(t, subprocess.CalledProcessError):'),
    ('L', 'R7 an HTTPStatusError subclass runs no code', '        if t is httpx.HTTPStatusError:', '        if issubclass(t, httpx.HTTPStatusError):'),
    ('G', 'R6 no lock through an import alias', 'import os\nimport threading\n', 'import os\nimport threading\nfrom threading import Lock as _Lock\n'),
    ('L', 'W1 the writer is a lifecycle thread (a handler that logs never waits)', '    ENQUEUE_ONLY.add(threading.get_ident())\n    while True:', '    while True:'),
    ('L', 'W1 a dead writer is replaced', '    if w is None or not w.is_alive():', '    if w is None:'),
    ('L', 'W1 a handler raising anything is skipped per handler', "                except BaseException:   # (one handler's failure never stops the others", "                except Exception:   # (one handler's failure never stops the others"),
    ('L', 'C3 a waiter on an evicted record is released', '            if _state["done"] >= target or _state["evicted"] >= target:\n                return True', '            if _state["done"] >= target:\n                return True'),
    ('L', 'C3 the stall latch', '            if _state["stalled_at"] is not None:\n                return False        # (the latch', '            if False:\n                return False        # (the latch'),
    ('L', 'C3 the latch clears on a later completion', '                    if _state["stalled_at"] is not None and _state["done"] > _state["stalled_at"]:', '                    if False:'),
    ('L', 'W5 flush is bounded', '            remaining = deadline - time.monotonic()\n            if remaining <= 0:\n                return False\n            _cond.wait(remaining)\n        return True', '            remaining = deadline - time.monotonic()\n            if False:\n                return False\n            _cond.wait(max(remaining, 0.05))\n        return True'),
    ('N', 'C4 a stalled writer does not hold the exit', '    if not hostlog.final_flush(EXIT_FLUSH_SECONDS):\n        os._exit(status)', '    if False:\n        os._exit(status)'),
    ('N', "C1 the node's main thread is a lifecycle thread", '    with hostlog.enqueue_only():\n        try:\n            _run(', '    with __import__("contextlib").nullcontext():\n        try:\n            _run('),
    ('NS', 'W2 the session loop is a lifecycle thread', '        with hostlog.enqueue_only():    # (host-side logging: the session loop never waits for the log writer)', '        with __import__("contextlib").nullcontext():    # (host-side logging: the session loop never waits for the log writer)')]
FIRST = ["tests/test_claude_steering.py", "tests/test_claude_steering_enable.py", "tests/test_steering_followups.py",
         "tests/test_hostlog.py"]
REST = ["tests/test_claude_correlation.py", "tests/test_codex_steering.py", "tests/test_steering_off_tripwire.py", "tests/test_session_restart.py",
        "tests/test_session_restart_hub.py", "tests/test_structured_results.py", "tests/test_hosted_sessions.py",
        "tests/test_session_host.py"]


def main() -> int:
    root = Path(sys.argv[1])
    only = set(sys.argv[2].split("|")) if len(sys.argv) > 2 and sys.argv[2] else None
    results = []
    for key, name, old, new in ROWS:
        if only is not None and name not in only:
            continue
        path = root / FILES[key]
        text = path.read_text()
        if text.count(old) != 1:
            print(f"STALE  {name} ({text.count(old)})", flush=True)
            results.append((name, None))
            continue
        path.write_text(text.replace(old, new, 1))
        try:
            failed = []
            for sel in (FIRST, REST):
                r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-x", *sel],
                                   cwd=root, capture_output=True, text=True, timeout=1800)
                failed = sorted(set(re.findall(r"^FAILED (\S+)", r.stdout, re.M)))
                if r.returncode not in (0, 1, 5):
                    failed = None       # (the mutant did not run: a collection error or an interruption)
                    break
                if failed or r.returncode == 1:
                    failed = failed or ["(exit 1)"]
                    break
        finally:
            path.write_text(text)
        if failed is None:
            print(f"BROKEN  {name}  (the mutant did not run)", flush=True)
            results.append((name, None))
            continue
        results.append((name, failed))
        print(f"{'CAUGHT' if failed else 'MISSED'}  {name}  {failed[0].split('::', 1)[-1] if failed else ''}",
              flush=True)
    print("rows:", len(results), flush=True)
    print("stale or broken:", [n for n, f in results if f is None], flush=True)
    print("missed:", [n for n, f in results if f == []], flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
