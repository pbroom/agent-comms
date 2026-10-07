import Foundation

public enum BoardClientError: Error, Equatable, CustomStringConvertible {
    case offline                    // nothing listening, or it did not answer in time
    case unauthorized               // 401: the token was not accepted
    case forbidden                  // 403: not the human's token (or not from localhost)
    case http(Int)
    case badResponse

    public var description: String {
        switch self {
        case .offline: return "Board server not running"
        case .unauthorized: return "The board did not accept the human token"
        case .forbidden: return "That token is not the human's"
        case .http(let code): return "The board answered HTTP \(code)"
        case .badResponse: return "The board sent an unexpected response"
        }
    }
}

/// A tiny client for the human-only routes the menu uses. Short timeouts so a hung server never freezes the
/// menu; no cache, cookies, proxies or redirects, and the token only ever goes to `Endpoint`.
public final class BoardClient: NSObject, URLSessionTaskDelegate, @unchecked Sendable {
    public let endpoint: Endpoint
    private let session: URLSession
    public static let requestTimeout: TimeInterval = 4

    public init(endpoint: Endpoint) {
        self.endpoint = endpoint
        let config = URLSessionConfiguration.ephemeral
        config.timeoutIntervalForRequest = BoardClient.requestTimeout
        config.timeoutIntervalForResource = BoardClient.requestTimeout + 2
        config.requestCachePolicy = .reloadIgnoringLocalAndRemoteCacheData
        config.urlCache = nil
        config.httpCookieStorage = nil
        config.httpShouldSetCookies = false
        config.connectionProxyDictionary = [:]
        config.waitsForConnectivity = false
        let queue = OperationQueue()
        queue.maxConcurrentOperationCount = 1
        self.session = URLSession(configuration: config, delegate: nil, delegateQueue: queue)
        super.init()
    }

    deinit { session.invalidateAndCancel() }

    /// Never follow a redirect: the Authorization header must not travel anywhere else.
    public func urlSession(_ session: URLSession, task: URLSessionTask,
                           willPerformHTTPRedirection response: HTTPURLResponse,
                           newRequest request: URLRequest) async -> URLRequest? {
        nil
    }

    /// Every request is built here: only to `endpoint`, the token only in the Authorization header, and for a POST
    /// a JSON body (`{}` when there is nothing to send).
    func request(_ url: URL, method: String, token: BearerToken, json: [String: String]? = nil) -> URLRequest? {
        guard endpoint.isBoardURL(url), url.fragment == nil else { return nil }
        var r = URLRequest(url: url, cachePolicy: .reloadIgnoringLocalAndRemoteCacheData,
                           timeoutInterval: BoardClient.requestTimeout)
        r.httpMethod = method
        r.setValue(token.headerValue, forHTTPHeaderField: "Authorization")
        r.setValue("application/json", forHTTPHeaderField: "Accept")
        if method == "POST" {
            r.setValue("application/json", forHTTPHeaderField: "Content-Type")
            r.httpBody = (try? JSONSerialization.data(withJSONObject: json ?? [:], options: [.sortedKeys])) ?? Data("{}".utf8)
        }
        return r
    }

    func finalizeRequest(postId: Int, token: BearerToken) -> URLRequest? {
        request(endpoint.finalize(postId: postId), method: "POST", token: token)
    }

    func acceptTaskRequest(taskId: Int, token: BearerToken) -> URLRequest? {
        request(endpoint.transition(taskId: taskId), method: "POST", token: token,
                json: ["status": "accepted", "note": "accepted from menu bar"])
    }

    func loginLinkRequest(page: DashboardPage, token: BearerToken) -> URLRequest? {
        request(endpoint.loginLinks, method: "POST", token: token, json: ["next": page.next])
    }

    private func send(_ url: URL, method: String, token: BearerToken) async throws -> Data {
        guard let req = request(url, method: method, token: token) else { throw BoardClientError.badResponse }
        return try await send(req)
    }

    private func send(_ req: URLRequest?) async throws -> Data {
        guard let req else { throw BoardClientError.badResponse }
        let data: Data
        let response: URLResponse
        do {
            (data, response) = try await session.data(for: req, delegate: self)
        } catch {
            throw BoardClientError.offline
        }
        guard let http = response as? HTTPURLResponse else { throw BoardClientError.badResponse }
        switch http.statusCode {
        case 200..<300: return data
        case 401: throw BoardClientError.unauthorized
        case 403: throw BoardClientError.forbidden
        default: throw BoardClientError.http(http.statusCode)
        }
    }

    public func summary(token: BearerToken) async throws -> BoardSummary {
        let data = try await send(endpoint.summary, method: "GET", token: token)
        do {
            return try BoardSummary.decode(data)
        } catch {
            throw BoardClientError.badResponse
        }
    }

    public func setPaused(_ paused: Bool, token: BearerToken) async throws {
        _ = try await send(paused ? endpoint.pause : endpoint.unpause, method: "POST", token: token)
    }

    /// Returns whether a running dispatcher was asked to stop (false: none was running).
    public func stopDispatcher(token: BearerToken) async throws -> Bool {
        let data = try await send(endpoint.stopDispatcher, method: "POST", token: token)
        struct Reply: Decodable { let requested: Bool }
        return (try? JSONDecoder().decode(Reply.self, from: data))?.requested ?? false
    }

    public func needsYou(token: BearerToken) async throws -> NeedsYouList {
        let data = try await send(endpoint.needsYou, method: "GET", token: token)
        do {
            return try NeedsYouList.decode(data)
        } catch {
            throw BoardClientError.badResponse
        }
    }

    public func finalize(postId: Int, token: BearerToken) async throws {
        _ = try await send(finalizeRequest(postId: postId, token: token))
    }

    public func acceptTask(taskId: Int, token: BearerToken) async throws {
        _ = try await send(acceptTaskRequest(taskId: taskId, token: token))
    }

    /// A one-time sign-in link for a dashboard page, or the plain page URL when the server has no login links (404).
    public func loginLink(page: DashboardPage, token: BearerToken) async throws -> LoginLink.Outcome {
        let result: Result<Data, BoardClientError>
        do {
            result = .success(try await send(loginLinkRequest(page: page, token: token)))
        } catch let e as BoardClientError {
            result = .failure(e)
        }
        return try LoginLink.resolve(result, page: page, endpoint: endpoint)
    }
}
