import Foundation

/// `GET /api/needs-you` (human only): the dashboard's "Needs you" items, newest first (at most 10), each with a
/// short preview of the post. The preview is agent-written text: the server cleans it to one line of at most 80
/// characters, the app cleans it again (`Display.preview`) and shows it only as plain text (`Text(verbatim:)`,
/// NSAlert's plain informative text), never as markup.
public struct NeedsYouList: Decodable, Equatable, Sendable {
    public var count: Int
    public var items: [NeedsYouItem]
    public var projects: [String: String?]

    public static func decode(_ data: Data) throws -> NeedsYouList {
        try JSONDecoder().decode(NeedsYouList.self, from: data)
    }

    public func project(forThread id: Int) -> String? {
        guard let entry = projects[String(id)], let name = entry else { return nil }
        return Display.projectName(name)
    }
}

public struct NeedsYouItem: Decodable, Equatable, Sendable {
    public var postId: Int
    public var threadId: Int
    public var agent: String
    public var type: String
    public var needsResponse: Bool
    public var taskId: Int?
    public var taskStatus: String?
    public var decisionStatus: String?
    public var sealed: Bool
    public var preview: String

    enum CodingKeys: String, CodingKey {
        case postId = "post_id", threadId = "thread_id", agent, type, needsResponse = "needs_response"
        case taskId = "task_id", taskStatus = "task_status", decisionStatus = "decision_status", sealed, preview
    }

    public init(postId: Int, threadId: Int, agent: String, type: String, needsResponse: Bool = false,
                taskId: Int? = nil, taskStatus: String? = nil, decisionStatus: String? = nil, sealed: Bool = false,
                preview: String = "") {
        self.postId = postId
        self.threadId = threadId
        self.agent = agent
        self.type = type
        self.needsResponse = needsResponse
        self.taskId = taskId
        self.taskStatus = taskStatus
        self.decisionStatus = decisionStatus
        self.sealed = sealed
        self.preview = preview
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        postId = try c.decode(Int.self, forKey: .postId)
        threadId = try c.decode(Int.self, forKey: .threadId)
        agent = try c.decode(String.self, forKey: .agent)
        type = try c.decode(String.self, forKey: .type)
        needsResponse = try c.decodeIfPresent(Bool.self, forKey: .needsResponse) ?? false
        taskId = try c.decodeIfPresent(Int.self, forKey: .taskId)
        taskStatus = try c.decodeIfPresent(String.self, forKey: .taskStatus)
        decisionStatus = try c.decodeIfPresent(String.self, forKey: .decisionStatus)
        sealed = try c.decodeIfPresent(Bool.self, forKey: .sealed) ?? false
        preview = try c.decodeIfPresent(String.self, forKey: .preview) ?? ""
    }

    /// An item from the counts-only summary (an older server without /api/needs-you): View only.
    public init(_ s: BoardSummary.Item) {
        self.init(postId: s.postId, threadId: s.threadId, agent: s.agent, type: s.type)
    }
}

/// What the per-item submenu offers.
public enum ItemAction: Equatable, Sendable {
    case view
    case finalizeDecision(postId: Int)
    case acceptTask(taskId: Int)

    public static func available(for item: NeedsYouItem) -> [ItemAction] {
        var out: [ItemAction] = [.view]
        // A sealed decision must be unsealed (in the dashboard) before it can be finalized.
        if item.type == "decision", item.decisionStatus == "proposal", !item.sealed {
            out.append(.finalizeDecision(postId: item.postId))
        }
        if let task = item.taskId, item.taskStatus == "proposed" {
            out.append(.acceptTask(taskId: task))
        }
        return out
    }
}

/// A dashboard page the menu can open.
public enum DashboardPage: Equatable, Sendable {
    case home
    case settings
    case post(Int)

    /// The `next` sent to POST /api/login-links.
    public var next: String {
        switch self {
        case .home: return "/"
        case .settings: return "/#settings"
        case .post(let id): return "/#post-\(id)"
        }
    }

    var fragment: String? {
        switch self {
        case .home: return nil
        case .settings: return "settings"
        case .post(let id): return "post-\(id)"
        }
    }
}

/// Turning a POST /api/login-links reply into the URL to open. The contract: 200 with
/// `{"url": "http://127.0.0.1:<port>/login/<code>", "expires_in_seconds": 60}`. A server without the feature
/// answers 404, and then the plain dashboard URL is opened (the browser may ask the human to sign in). A reply URL
/// that is not exactly a one-segment `/login/<code>` path on this board is never opened.
public enum LoginLink {
    struct Reply: Decodable {
        let url: String
    }

    public enum Outcome: Equatable {
        case signedIn(URL)
        case fallback(URL)
    }

    public static func resolve(_ result: Result<Data, BoardClientError>, page: DashboardPage,
                               endpoint: Endpoint) throws -> Outcome {
        switch result {
        case .success(let data):
            guard let reply = try? JSONDecoder().decode(Reply.self, from: data),
                  let url = URL(string: reply.url), isLoginURL(url, endpoint: endpoint)
            else { throw BoardClientError.badResponse }
            return .signedIn(url)
        case .failure(.http(404)):
            return .fallback(endpoint.page(page))
        case .failure(let e):
            throw e
        }
    }

    public static func isLoginURL(_ url: URL, endpoint: Endpoint) -> Bool {
        guard url.scheme == "http", url.host == Endpoint.host, url.port == endpoint.port,
              url.user == nil, url.password == nil, url.query == nil, url.fragment == nil else { return false }
        let parts = url.path.split(separator: "/", omittingEmptySubsequences: false)
        // "", "login", "<code>"
        guard parts.count == 3, parts[0].isEmpty, parts[1] == "login" else { return false }
        let code = parts[2]
        return (8...256).contains(code.count) && code.unicodeScalars.allSatisfy {
            ("a"..."z").contains($0) || ("A"..."Z").contains($0) || ("0"..."9").contains($0) || $0 == "-" || $0 == "_"
        }
    }
}
