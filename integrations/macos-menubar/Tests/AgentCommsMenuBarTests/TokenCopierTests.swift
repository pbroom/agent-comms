import AppKit
import Foundation
import XCTest

@testable import AgentCommsKit

/// A pasteboard that records writes. `changeCount` goes up on every change, as NSPasteboard's does.
final class FakePasteboard: TokenPasteboard {
    private(set) var changeCount = 0
    private(set) var string: String?
    private(set) var types: [String] = []
    private(set) var writes = 0
    private(set) var clears = 0
    var refuseWrites = false

    func replaceContents(string: String, markerTypes: [String]) -> Bool {
        if refuseWrites { return false }
        changeCount += 1
        writes += 1
        self.string = string
        types = ["public.utf8-plain-text"] + markerTypes
        return true
    }

    func clearContents() {
        changeCount += 1
        clears += 1
        string = nil
        types = []
    }

    /// The human copies something else.
    func humanCopies(_ text: String) {
        changeCount += 1
        string = text
        types = ["public.utf8-plain-text"]
    }
}

/// Holds scheduled clears until the test runs them.
final class ManualScheduler {
    private(set) var delays: [TimeInterval] = []
    private var pending: [() -> Void] = []

    var schedule: TokenCopier.Scheduler {
        { [unowned self] seconds, work in
            self.delays.append(seconds)
            self.pending.append(work)
        }
    }

    func runAll() {
        let work = pending
        pending = []
        work.forEach { $0() }
    }

    func runFirst() { pending.removeFirst()() }
}

final class TokenCopierTests: XCTestCase {
    static let secret = "ac_TOPSECRET-copy-value"
    let token = BearerToken(TokenCopierTests.secret)

    func testWritesTheTokenWithConcealedAndTransientTypes() {
        let pb = FakePasteboard()
        let timer = ManualScheduler()
        let copier = TokenCopier(pasteboard: pb, schedule: timer.schedule)
        XCTAssertEqual(copier.copy(load: { self.token }), .copied(clearsInSeconds: 60))
        XCTAssertEqual(pb.string, Self.secret)
        XCTAssertTrue(pb.types.contains("org.nspasteboard.ConcealedType"))
        XCTAssertTrue(pb.types.contains("org.nspasteboard.TransientType"))
        XCTAssertEqual(timer.delays, [60])
        XCTAssertTrue(copier.hasPendingClear)
    }

    func testClearsAfterTheTimerWhenNothingChanged() {
        let pb = FakePasteboard()
        let timer = ManualScheduler()
        let copier = TokenCopier(pasteboard: pb, schedule: timer.schedule)
        var outcomes: [TokenClearOutcome] = []
        copier.copy(load: { self.token }, onClear: { outcomes.append($0) })
        timer.runAll()
        XCTAssertEqual(outcomes, [.cleared])
        XCTAssertNil(pb.string)
        XCTAssertEqual(pb.clears, 1)
        XCTAssertFalse(copier.hasPendingClear)
    }

    func testNeverClearsSomethingCopiedAfterwards() {
        let pb = FakePasteboard()
        let timer = ManualScheduler()
        let copier = TokenCopier(pasteboard: pb, schedule: timer.schedule)
        var outcomes: [TokenClearOutcome] = []
        copier.copy(load: { self.token }, onClear: { outcomes.append($0) })
        pb.humanCopies("my own text")
        timer.runAll()
        XCTAssertEqual(outcomes, [.changedSince])
        XCTAssertEqual(pb.string, "my own text")
        XCTAssertEqual(pb.clears, 0)
        XCTAssertNil(copier.clearNowIfUnchanged())   // nothing of ours left
    }

    func testASecondCopyOwnsTheClear() {
        let pb = FakePasteboard()
        let timer = ManualScheduler()
        let copier = TokenCopier(pasteboard: pb, schedule: timer.schedule)
        var outcomes: [TokenClearOutcome] = []
        copier.copy(load: { self.token }, onClear: { outcomes.append($0) })
        copier.copy(load: { self.token }, onClear: { outcomes.append($0) })
        timer.runFirst()                             // the first copy's 60 s: the second copy is only just made
        XCTAssertEqual(outcomes, [.superseded])
        XCTAssertEqual(pb.string, Self.secret)
        XCTAssertEqual(pb.clears, 0)
        timer.runFirst()
        XCTAssertEqual(outcomes, [.superseded, .cleared])
        XCTAssertNil(pb.string)
    }

    func testClearNowOnQuitOnlyWhenUnchanged() {
        let pb = FakePasteboard()
        let timer = ManualScheduler()
        let copier = TokenCopier(pasteboard: pb, schedule: timer.schedule)
        XCTAssertNil(copier.clearNowIfUnchanged())   // nothing copied yet
        XCTAssertEqual(pb.clears, 0)
        copier.copy(load: { self.token })
        XCTAssertEqual(copier.clearNowIfUnchanged(), .cleared)
        XCTAssertNil(pb.string)
        var outcomes: [TokenClearOutcome] = []
        copier.copy(load: { self.token }, onClear: { outcomes.append($0) })
        pb.humanCopies("later")
        XCTAssertEqual(copier.clearNowIfUnchanged(), .changedSince)
        XCTAssertEqual(pb.string, "later")
        timer.runAll()                               // the pending timers are now no-ops
        XCTAssertEqual(pb.string, "later")
        XCTAssertEqual(outcomes, [.superseded])
    }

    func testNothingHappensWhenTheTokenCannotBeLoaded() {
        let pb = FakePasteboard()
        pb.humanCopies("untouched")
        let before = pb.changeCount
        let timer = ManualScheduler()
        let copier = TokenCopier(pasteboard: pb, schedule: timer.schedule)
        let result = copier.copy(load: { throw TokenFileError.tooPermissive(path: "/x/human.token", mode: 0o644) })
        guard case .notCopied(let reason) = result else { return XCTFail("\(result)") }
        XCTAssertTrue(reason.contains("chmod 600"))
        XCTAssertEqual(pb.changeCount, before)
        XCTAssertEqual(pb.string, "untouched")
        XCTAssertEqual(pb.writes, 0)
        XCTAssertTrue(timer.delays.isEmpty)
        XCTAssertFalse(copier.hasPendingClear)
    }

    func testARefusedWriteSchedulesNothing() {
        let pb = FakePasteboard()
        pb.refuseWrites = true
        let timer = ManualScheduler()
        let copier = TokenCopier(pasteboard: pb, schedule: timer.schedule)
        XCTAssertEqual(copier.copy(load: { self.token }), .notCopied(reason: "the clipboard refused the write"))
        XCTAssertTrue(timer.delays.isEmpty)
        XCTAssertFalse(copier.hasPendingClear)
    }

    func testTheTokenNeverAppearsInDescriptionsOrDumps() {
        let pb = FakePasteboard()
        let timer = ManualScheduler()
        let copier = TokenCopier(pasteboard: pb, schedule: timer.schedule)
        var outcomes: [TokenClearOutcome] = []
        let copied = copier.copy(load: { self.token }, onClear: { outcomes.append($0) })
        let failed = TokenCopier(pasteboard: FakePasteboard(), schedule: timer.schedule)
            .copy(load: { throw TokenFileError.malformed(path: "/x/human.token") })
        let pendingCopier = String(describing: copier)
        timer.runAll()

        var texts = [pendingCopier, String(describing: copier), String(reflecting: copier),
                     copied.description, failed.description, "\(copied)", "\(failed)",
                     String(describing: token), String(reflecting: token), "\(token)"]
        texts += outcomes.map(\.description)
        texts += [TokenClearOutcome.changedSince, .superseded].map(\.description)
        for value in [copier, copied, failed, token, outcomes] as [Any] {
            var dumped = ""
            dump(value, to: &dumped)
            texts.append(dumped)
            texts.append(String(describing: Mirror(reflecting: value).children.map { "\($0.label ?? ""): \($0.value)" }))
        }
        XCTAssertEqual(copied.description, "Board token copied — clears from the clipboard in 60 s")
        for text in texts {
            XCTAssertFalse(text.contains("TOPSECRET"), text)
        }
    }

    /// The real adapter, on a private named pasteboard (never the general one).
    func testSystemPasteboardWritesOneItemWithTheMarkerTypes() throws {
        let ns = NSPasteboard(name: NSPasteboard.Name("dev.agentcomms.tests.\(UUID().uuidString)"))
        defer { ns.releaseGlobally() }
        let pb = SystemPasteboard(ns)
        let timer = ManualScheduler()
        let copier = TokenCopier(pasteboard: pb, schedule: timer.schedule)
        XCTAssertEqual(copier.copy(load: { self.token }), .copied(clearsInSeconds: 60))
        XCTAssertEqual(ns.pasteboardItems?.count, 1)
        XCTAssertEqual(ns.string(forType: .string), Self.secret)
        let types = Set((ns.types ?? []).map(\.rawValue))
        XCTAssertTrue(types.isSuperset(of: ["org.nspasteboard.ConcealedType", "org.nspasteboard.TransientType"]),
                      "\(types)")
        timer.runAll()
        XCTAssertNil(ns.string(forType: .string))

        copier.copy(load: { self.token })
        ns.clearContents()
        ns.setString("copied afterwards", forType: .string)
        timer.runAll()
        XCTAssertEqual(ns.string(forType: .string), "copied afterwards")
    }
}
