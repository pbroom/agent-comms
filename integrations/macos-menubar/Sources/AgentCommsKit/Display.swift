import Foundation

/// Every string the menu shows is built here from ids, counts and names that pass the same rules the server
/// applies (agent names, post types, project basenames). Anything else is replaced, so even a misbehaving or
/// older server cannot put agent-written text in the menu.
public enum Display {
    static let postTypes: Set<String> = ["question", "proposal", "status", "finding", "handoff", "request", "decision"]

    /// agent_comms.config.NAME_RE: ^[a-z][a-z0-9_-]{0,31}$
    public static func agentName(_ s: String?) -> String {
        guard let s, (1...32).contains(s.count), let first = s.unicodeScalars.first,
              ("a"..."z").contains(first),
              s.unicodeScalars.allSatisfy({ ("a"..."z").contains($0) || ("0"..."9").contains($0) || $0 == "_" || $0 == "-" })
        else { return "?" }
        return s
    }

    public static func postType(_ s: String) -> String {
        postTypes.contains(s) ? s : "?"
    }

    /// agent_comms.summary.PROJECT_NAME_RE: ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$
    public static func projectName(_ s: String) -> String? {
        func plain(_ c: Unicode.Scalar) -> Bool {
            ("a"..."z").contains(c) || ("A"..."Z").contains(c) || ("0"..."9").contains(c)
        }
        guard (1...64).contains(s.unicodeScalars.count), let first = s.unicodeScalars.first, plain(first),
              s.unicodeScalars.allSatisfy({ plain($0) || $0 == "." || $0 == "_" || $0 == "-" })
        else { return nil }
        return s
    }

    public static func thread(_ id: Int?, in summary: BoardSummary) -> String {
        guard let id else { return "thread ?" }
        if let project = summary.project(forThread: id) {
            return "thread \(id) (\(project))"
        }
        return "thread \(id)"
    }

    /// "#41 · claude · question · thread 12 (spfx-kit)"
    public static func needsYouItem(_ item: BoardSummary.Item, in summary: BoardSummary) -> String {
        "#\(item.postId) · \(agentName(item.agent)) · \(postType(item.type)) · \(thread(item.threadId, in: summary))"
    }

    static func thread(_ id: Int, project: String?) -> String {
        if let project, let name = projectName(project) { return "thread \(id) (\(name))" }
        return "thread \(id)"
    }

    /// "#51 · claude · proposal · thread 12 (spfx-kit)"
    public static func needsYouItem(_ item: NeedsYouItem, project: String?) -> String {
        "#\(item.postId) · \(agentName(item.agent)) · \(postType(item.type)) · \(thread(item.threadId, project: project))"
    }

    /// The submenu title: the item line, then a short quoted preview when there is one.
    public static func needsYouTitle(_ item: NeedsYouItem, project: String?) -> String {
        let line = needsYouItem(item, project: project)
        let short = preview(item.preview, limit: 40)
        return short.isEmpty ? line : "\(line) — “\(short)”"
    }

    /// Agent-written preview text as one plain line: control, format, separator and private-use characters
    /// (bidi overrides included) become spaces, whitespace collapses, and it is cut to `limit` characters.
    /// The server does the same; this is the client's own check. Always shown as plain text.
    public static func preview(_ s: String, limit: Int = 80) -> String {
        let dropped: Set<Unicode.GeneralCategory> = [.control, .format, .surrogate, .privateUse, .unassigned,
                                                      .lineSeparator, .paragraphSeparator]
        var scalars = String.UnicodeScalarView()
        for u in s.unicodeScalars {
            scalars.append(dropped.contains(u.properties.generalCategory) ? " " : u)
        }
        let line = String(scalars).split(whereSeparator: { $0.isWhitespace }).joined(separator: " ")
        guard line.count > limit else { return line }
        let cut = line.prefix(max(1, limit - 1)).trimmingCharacters(in: .whitespaces)
        return cut + "…"
    }

    /// "codex · thread 12 (spfx-kit) · 3m 20s"
    public static func run(_ run: BoardSummary.Run, in summary: BoardSummary, extraSeconds: Int = 0) -> String {
        var parts = [agentName(run.agent), thread(run.threadId, in: summary)]
        if let elapsed = run.elapsedSeconds {
            parts.append(duration(elapsed + max(0, extraSeconds)))
        }
        if run.status == "orphaned" {
            parts.append("orphaned")
        }
        return parts.joined(separator: " · ")
    }

    /// "rule 4 · thread 12 (spfx-kit) · codex, claude · 7/10 left"
    public static func approval(_ a: BoardSummary.Approval, in summary: BoardSummary) -> String {
        let agents = a.agents.map { agentName($0) }.joined(separator: ", ")
        return "rule \(a.ruleId) · \(thread(a.threadId, in: summary)) · \(agents) · \(a.launchesLeft)/\(a.maxLaunches) left"
    }

    /// "claude ×2"
    public static func liveSession(_ s: BoardSummary.AgentCount) -> String {
        s.count == 1 ? agentName(s.agent) : "\(agentName(s.agent)) ×\(s.count)"
    }

    public static func duration(_ seconds: Int) -> String {
        let s = max(0, seconds)
        if s < 60 { return "\(s)s" }
        if s < 3600 { return "\(s / 60)m \(s % 60)s" }
        return "\(s / 3600)h \((s % 3600) / 60)m"
    }

    /// One line at the top of the menu.
    public static func statusLine(_ s: BoardSummary) -> String {
        var parts = [s.paused ? "Board paused" : "Board running"]
        parts.append("\(s.threads.open) open thread\(s.threads.open == 1 ? "" : "s")")
        parts.append("\(s.tasks.open) open task\(s.tasks.open == 1 ? "" : "s")")
        if s.unreadForHuman > 0 {
            parts.append("\(s.unreadForHuman) unread")
        }
        return parts.joined(separator: " · ")
    }

    public static func dispatcherLine(_ d: BoardSummary.Dispatcher) -> String {
        guard d.running else { return "Dispatcher not running" }
        if let age = d.heartbeatSecondsAgo {
            return "Dispatcher running (heartbeat \(duration(Int(age.rounded()))) ago)"
        }
        return "Dispatcher running"
    }
}
