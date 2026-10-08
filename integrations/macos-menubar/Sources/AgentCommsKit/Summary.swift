import Foundation

/// `GET /api/summary`: counts and server-stamped identifiers only (see agent_comms/summary.py).
/// The server never sends post bodies, titles, summaries, task titles or rule purposes, and the app re-checks
/// every string it shows (`Display`), so nothing an agent wrote reaches the menu.
public struct BoardSummary: Decodable, Equatable, Sendable {
    public var summaryVersion: Int
    public var generatedAt: String?
    public var paused: Bool
    public var needsYou: NeedsYou
    public var unreadForHuman: Int
    public var threads: Threads
    public var tasks: Tasks
    public var liveSessions: LiveSessions
    public var dispatcher: Dispatcher
    public var approvals: [Approval]
    /// Thread id (as a string) -> project basename, or nil when the server would not vouch for it.
    public var projects: [String: String?]

    enum CodingKeys: String, CodingKey {
        case summaryVersion = "summary_version", generatedAt = "generated_at", paused
        case needsYou = "needs_you", unreadForHuman = "unread_for_human", threads, tasks
        case liveSessions = "live_sessions", dispatcher, approvals, projects
    }

    public struct NeedsYou: Decodable, Equatable, Sendable {
        public var count: Int
        public var items: [Item]
    }

    public struct Item: Decodable, Equatable, Sendable {
        public var issueId: Int?
        public var postId: Int?
        public var threadId: Int
        public var agent: String
        public var type: String

        enum CodingKeys: String, CodingKey {
            case issueId = "issue_id"
            case postId = "post_id", threadId = "thread_id", agent, type
        }
    }

    public struct Threads: Decodable, Equatable, Sendable {
        public var open: Int
    }

    public struct Tasks: Decodable, Equatable, Sendable {
        public var open: Int
        public var byStatus: [String: Int]

        enum CodingKeys: String, CodingKey {
            case open, byStatus = "by_status"
        }
    }

    public struct LiveSessions: Decodable, Equatable, Sendable {
        public var windowMinutes: Int
        public var agents: [AgentCount]

        enum CodingKeys: String, CodingKey {
            case windowMinutes = "window_minutes", agents
        }
    }

    public struct AgentCount: Decodable, Equatable, Sendable {
        public var agent: String
        public var count: Int
    }

    public struct Dispatcher: Decodable, Equatable, Sendable {
        public var running: Bool
        public var heartbeatSecondsAgo: Double?
        public var runs: [Run]

        enum CodingKeys: String, CodingKey {
            case running, heartbeatSecondsAgo = "heartbeat_seconds_ago", runs
        }
    }

    public struct Run: Decodable, Equatable, Sendable {
        public var agent: String?
        public var threadId: Int?
        public var ruleId: Int?
        public var status: String?
        public var startedAt: String?
        public var elapsedSeconds: Int?

        enum CodingKeys: String, CodingKey {
            case agent, threadId = "thread_id", ruleId = "rule_id", status
            case startedAt = "started_at", elapsedSeconds = "elapsed_seconds"
        }
    }

    public struct Approval: Decodable, Equatable, Sendable {
        public var ruleId: Int
        public var threadId: Int
        public var agents: [String]
        public var launchesLeft: Int
        public var maxLaunches: Int
        public var expiresAt: String?

        enum CodingKeys: String, CodingKey {
            case ruleId = "rule_id", threadId = "thread_id", agents, launchesLeft = "launches_left"
            case maxLaunches = "max_launches", expiresAt = "expires_at"
        }
    }

    public static func decode(_ data: Data) throws -> BoardSummary {
        try JSONDecoder().decode(BoardSummary.self, from: data)
    }

    /// The project basename for a thread, if the server sent a plain one.
    public func project(forThread id: Int) -> String? {
        guard let entry = projects[String(id)], let name = entry else { return nil }
        return Display.projectName(name)
    }
}
