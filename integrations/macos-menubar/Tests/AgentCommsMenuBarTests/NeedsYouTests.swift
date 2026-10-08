import Darwin
import Foundation
import XCTest

@testable import AgentCommsKit

final class NeedsYouTests: XCTestCase {
    func testSharedIssueHasDistinctIdentityAndViewOnly() throws {
        let data = Data(#"{"count":1,"items":[{"issue_id":7,"post_id":null,"thread_id":2,"agent":"codex","type":"issue","task_id":3,"task_status":"proposed","preview":"Access blocked"}],"projects":{}}"#.utf8)
        let item = try XCTUnwrap(NeedsYouList.decode(data).items.first)
        XCTAssertNil(item.postId)
        XCTAssertEqual(item.identity, "issue-7")
        XCTAssertEqual(item.dashboardPage, .issue(7))
        XCTAssertEqual(ItemAction.available(for: item), [.view])
        XCTAssertEqual(Display.needsYouItem(item, project: nil), "Issue #7 · awaiting your decision")
        XCTAssertEqual(DashboardPage.issue(7).next, "/#issue-7")
        XCTAssertEqual(Endpoint(port: 8787).page(.issue(7)).absoluteString, "http://127.0.0.1:8787/#issue-7")
    }

    func fixture() throws -> NeedsYouList {
        let url = try XCTUnwrap(Bundle.module.url(forResource: "needs_you", withExtension: "json",
                                                  subdirectory: "Fixtures"))
        return try NeedsYouList.decode(try Data(contentsOf: url))
    }

    func token() throws -> BearerToken {
        let dir = FileManager.default.temporaryDirectory.appendingPathComponent("acmb-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: dir) }
        let path = dir.appendingPathComponent("h.token").path
        try Data("ac_TOPSECRET".utf8).write(to: URL(fileURLWithPath: path))
        chmod(path, 0o600)
        return try TokenFile.load(path: path)
    }

    // MARK: decoding

    func testDecodesTheFixture() throws {
        let list = try fixture()
        XCTAssertEqual(list.count, 12)
        XCTAssertEqual(list.items.map(\.postId), [51, 49, 47, 44])
        let p = list.items[0]
        XCTAssertEqual(p, NeedsYouItem(postId: 51, threadId: 12, agent: "claude", type: "proposal", needsResponse: true,
                                       taskId: 9, taskStatus: "proposed", decisionStatus: nil, sealed: false,
                                       preview: "Split the parser into a tokenizer and a recursive-descent pass; accept the task?"))
        XCTAssertEqual(list.items[2].preview, "sealed post")
        XCTAssertTrue(list.items[2].sealed)
        XCTAssertEqual(list.project(forThread: 12), "spfx-kit")
        XCTAssertNil(list.project(forThread: 7))
    }

    func testOlderServerFieldsAreOptional() throws {
        let json = #"{"count": 1, "items": [{"post_id": 3, "thread_id": 4, "agent": "codex", "type": "question"}], "projects": {}}"#
        let list = try NeedsYouList.decode(Data(json.utf8))
        XCTAssertEqual(list.items[0], NeedsYouItem(postId: 3, threadId: 4, agent: "codex", type: "question"))
        XCTAssertEqual(ItemAction.available(for: list.items[0]), [.view])
    }

    // MARK: which actions each item gets

    func testSubmenuActionsPerItemKind() throws {
        let items = try fixture().items
        XCTAssertEqual(ItemAction.available(for: items[0]), [.view, .acceptTask(taskId: 9)])        // proposed task
        XCTAssertEqual(ItemAction.available(for: items[1]), [.view, .finalizeDecision(postId: 49)])  // open decision
        XCTAssertEqual(ItemAction.available(for: items[2]), [.view])                                  // sealed decision
        XCTAssertEqual(ItemAction.available(for: items[3]), [.view])                                  // task accepted

        let final = NeedsYouItem(postId: 1, threadId: 1, agent: "codex", type: "decision", needsResponse: true,
                                 decisionStatus: "final")
        XCTAssertEqual(ItemAction.available(for: final), [.view])
        let both = NeedsYouItem(postId: 2, threadId: 1, agent: "codex", type: "decision", taskId: 5,
                                taskStatus: "proposed", decisionStatus: "proposal")
        XCTAssertEqual(ItemAction.available(for: both), [.view, .finalizeDecision(postId: 2), .acceptTask(taskId: 5)])
        let fromSummary = NeedsYouItem(BoardSummary.Item(postId: 8, threadId: 2, agent: "claude", type: "decision"))
        XCTAssertEqual(ItemAction.available(for: fromSummary), [.view])   // no decision_status: no finalize offered
    }

    // MARK: previews are plain, single-line text

    func testPreviewCleaning() throws {
        XCTAssertEqual(Display.preview("a\r\nb\u{2028}c\u{202E}d\u{200B}e\u{7}f\tg"), "a b c d e f g")
        let long = String(repeating: "x", count: 200)
        XCTAssertEqual(Display.preview(long).count, 80)
        XCTAssertTrue(Display.preview(long).hasSuffix("…"))
        XCTAssertEqual(Display.preview("short", limit: 80), "short")
        let list = try fixture()
        XCTAssertEqual(Display.needsYouTitle(list.items[1], project: list.project(forThread: 9)),
                       "#49 · codex · decision · thread 9 (agent-comms) — “Use SQLite WAL with a 30 s busy timeout”")
        // Markup stays literal text (the menu renders it with Text(verbatim:))
        XCTAssertTrue(Display.preview(list.items[3].preview).hasPrefix("<b>Should</b>"))
        let bad = NeedsYouItem(postId: 1, threadId: 2, agent: "Not A Name", type: "evil", preview: "x")
        XCTAssertEqual(Display.needsYouItem(bad, project: "../etc"), "#1 · ? · ? · thread 2")
    }

    // MARK: request building

    func testFinalizeAcceptAndLoginLinkRequests() throws {
        let t = try token()
        let client = BoardClient(endpoint: Endpoint(port: 8799))

        let fin = try XCTUnwrap(client.finalizeRequest(postId: 49, token: t))
        XCTAssertEqual(fin.httpMethod, "POST")
        XCTAssertEqual(fin.url?.absoluteString, "http://127.0.0.1:8799/api/posts/49/finalize")
        XCTAssertEqual(fin.httpBody, Data("{}".utf8))

        let acc = try XCTUnwrap(client.acceptTaskRequest(taskId: 9, token: t))
        XCTAssertEqual(acc.url?.absoluteString, "http://127.0.0.1:8799/api/tasks/9/transition")
        let body = try XCTUnwrap(JSONSerialization.jsonObject(with: acc.httpBody!) as? [String: String])
        XCTAssertEqual(body, ["status": "accepted", "note": "accepted from menu bar"])

        for (page, next) in [(DashboardPage.post(51), "/#post-51"), (.home, "/"), (.settings, "/#settings")] {
            let r = try XCTUnwrap(client.loginLinkRequest(page: page, token: t))
            XCTAssertEqual(r.url?.absoluteString, "http://127.0.0.1:8799/api/login-links")
            let b = try XCTUnwrap(JSONSerialization.jsonObject(with: r.httpBody!) as? [String: String])
            XCTAssertEqual(b, ["next": next])
        }

        for r in [fin, acc, try XCTUnwrap(client.loginLinkRequest(page: .home, token: t))] {
            XCTAssertEqual(r.value(forHTTPHeaderField: "Authorization"), "Bearer ac_TOPSECRET")
            XCTAssertEqual(r.value(forHTTPHeaderField: "Content-Type"), "application/json")
            XCTAssertEqual(r.url?.host, "127.0.0.1")
            XCTAssertFalse(r.url!.absoluteString.contains("TOPSECRET"))
            XCTAssertFalse(String(data: r.httpBody ?? Data(), encoding: .utf8)!.contains("TOPSECRET"))
        }
        // Never to another host, port or a URL with a query or fragment
        for other in ["http://localhost:8799/api/login-links", "http://127.0.0.1:8787/api/login-links",
                      "http://127.0.0.1:8799/api/login-links?next=/", "http://127.0.0.1:8799/#post-1"] {
            XCTAssertNil(client.request(URL(string: other)!, method: "POST", token: t, json: [:]), other)
        }
    }

    // MARK: login link outcome and the 404 fallback

    func testLoginLinkOutcomes() throws {
        let e = Endpoint(port: 8799)
        let ok = Data(#"{"url": "http://127.0.0.1:8799/login/AbC-123_xyzXYZ0", "expires_in_seconds": 60}"#.utf8)
        XCTAssertEqual(try LoginLink.resolve(.success(ok), page: .post(5), endpoint: e),
                       .signedIn(URL(string: "http://127.0.0.1:8799/login/AbC-123_xyzXYZ0")!))

        // A server without login links: open the plain page; the browser's own sign-in applies.
        XCTAssertEqual(try LoginLink.resolve(.failure(.http(404)), page: .post(5), endpoint: e),
                       .fallback(URL(string: "http://127.0.0.1:8799/#post-5")!))
        XCTAssertEqual(try LoginLink.resolve(.failure(.http(404)), page: .settings, endpoint: e),
                       .fallback(URL(string: "http://127.0.0.1:8799/#settings")!))
        XCTAssertEqual(try LoginLink.resolve(.failure(.http(404)), page: .home, endpoint: e),
                       .fallback(URL(string: "http://127.0.0.1:8799/")!))

        // Other failures are errors, not silent fallbacks
        for f in [BoardClientError.offline, .unauthorized, .forbidden, .http(500), .http(422)] {
            XCTAssertThrowsError(try LoginLink.resolve(.failure(f), page: .home, endpoint: e)) { err in
                XCTAssertEqual(err as? BoardClientError, f)
            }
        }

        // A reply URL that is not a login link on this board is never opened
        for bad in ["https://evil.example/login/abcdefgh12", "http://127.0.0.1:8787/login/abcdefgh12",
                    "http://localhost:8799/login/abcdefgh12", "http://127.0.0.1:8799/login/abcdefgh12?next=x",
                    "http://127.0.0.1:8799/login/abcdefgh12#x", "http://127.0.0.1:8799/login/../api/x",
                    "http://127.0.0.1:8799/login/a/b", "http://127.0.0.1:8799/login/short",
                    "http://u:p@127.0.0.1:8799/login/abcdefgh12", "javascript:alert(1)", "not a url"] {
            let data = try JSONSerialization.data(withJSONObject: ["url": bad])
            XCTAssertThrowsError(try LoginLink.resolve(.success(data), page: .home, endpoint: e), bad)
        }
        XCTAssertThrowsError(try LoginLink.resolve(.success(Data("{}".utf8)), page: .home, endpoint: e))
    }

    func testPagePlainURLs() {
        let e = Endpoint(port: 8787)
        XCTAssertEqual(e.page(.post(12)).absoluteString, "http://127.0.0.1:8787/#post-12")
        XCTAssertEqual(e.page(.settings), e.settings)
        XCTAssertEqual(e.page(.home), e.dashboard)
        XCTAssertEqual(e.needsYou.absoluteString, "http://127.0.0.1:8787/api/needs-you")
    }
}
