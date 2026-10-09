import AppKit
import Foundation
import XCTest

@testable import AgentCommsKit

/// A pasteboard that records writes. Like NSPasteboard, `changeCount` goes up when the contents are cleared or
/// prepared for new contents, not when the owner writes to contents it prepared.
final class FakePasteboard: TokenPasteboard {
    private(set) var changeCount = 0
    private(set) var string: String?
    private(set) var types: [String] = []
    private(set) var prepares: [Bool] = []        // the currentHostOnly flag of each prepare
    private(set) var writes = 0
    private(set) var clears = 0
    var refuseWrites = false
    /// Runs inside `writeItem`, before the write: another app writing at the same moment.
    var duringWrite: (() -> Void)?

    func prepareForNewContents(currentHostOnly: Bool) -> Int {
        prepares.append(currentHostOnly)
        changeCount += 1
        string = nil
        types = []
        return changeCount
    }

    func writeItem(string: String, markerTypes: [String]) -> Bool {
        duringWrite?()
        if refuseWrites { return false }
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

    /// Universal Clipboard must not carry the token to other devices, where the 60 s clear cannot reach it.
    func testAsksForCurrentHostOnlyContents() {
        let pb = FakePasteboard()
        let timer = ManualScheduler()
        let copier = TokenCopier(pasteboard: pb, schedule: timer.schedule)
        copier.copy(load: { self.token })
        copier.copy(load: { self.token })
        XCTAssertEqual(pb.prepares, [true, true])
    }

    /// Another app writing between our prepare and our write must not be mistaken for our copy and cleared later.
    func testAWriteByAnotherAppDuringTheCopySchedulesNoClear() {
        let pb = FakePasteboard()
        let timer = ManualScheduler()
        let copier = TokenCopier(pasteboard: pb, schedule: timer.schedule)
        pb.duringWrite = { pb.humanCopies("another app") }
        let result = copier.copy(load: { self.token })
        XCTAssertEqual(result, .copiedWithoutAutoClear)
        XCTAssertFalse(result.description.contains("clears"))
        XCTAssertTrue(timer.delays.isEmpty)
        XCTAssertFalse(copier.hasPendingClear)
        XCTAssertNil(copier.clearNowIfUnchanged())
        XCTAssertEqual(pb.clears, 0)
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

    func testARefusedWriteReportsFailureAndSchedulesNothing() {
        let pb = FakePasteboard()
        pb.humanCopies("previous contents")
        pb.refuseWrites = true
        let timer = ManualScheduler()
        let copier = TokenCopier(pasteboard: pb, schedule: timer.schedule)
        let result = copier.copy(load: { self.token })
        guard case .notCopied = result else { return XCTFail("\(result)") }
        XCTAssertTrue(result.description.hasPrefix("Could not copy the board token"), result.description)
        XCTAssertTrue(result.description.contains("previous contents were cleared"), result.description)
        XCTAssertFalse(result.description.contains("copied —"))
        XCTAssertNil(pb.string)                      // nothing half-written, and honest about the lost contents
        XCTAssertTrue(timer.delays.isEmpty)
        XCTAssertFalse(copier.hasPendingClear)
    }

    /// The seam behind BoardModel.canCopyToken: offered only when signed in with a loaded token.
    func testOfferedOnlyWhenSignedInWithALoadedToken() {
        XCTAssertTrue(TokenCopier.isOffered(signedIn: true, tokenLoaded: true))
        XCTAssertFalse(TokenCopier.isOffered(signedIn: true, tokenLoaded: false))
        XCTAssertFalse(TokenCopier.isOffered(signedIn: false, tokenLoaded: true))
        XCTAssertFalse(TokenCopier.isOffered(signedIn: false, tokenLoaded: false))
    }

    /// The seam behind BoardModel's willTerminate observer: the clear runs synchronously when it is posted.
    func testClearsSynchronouslyWhenTheQuitNotificationIsPosted() {
        let center = NotificationCenter()
        let quit = Notification.Name("test.willTerminate")
        let pb = FakePasteboard()
        let timer = ManualScheduler()
        let copier = TokenCopier(pasteboard: pb, schedule: timer.schedule)
        let observer = copier.clearWhenPosted(quit, center: center)
        defer { center.removeObserver(observer) }

        copier.copy(load: { self.token })
        center.post(name: quit, object: nil)
        XCTAssertNil(pb.string)
        XCTAssertEqual(pb.clears, 1)
        XCTAssertFalse(copier.hasPendingClear)

        copier.copy(load: { self.token })
        pb.humanCopies("copied afterwards")
        center.post(name: quit, object: nil)
        XCTAssertEqual(pb.string, "copied afterwards")
        XCTAssertEqual(pb.clears, 1)
        center.post(name: Notification.Name("something.else"), object: nil)
        XCTAssertEqual(pb.clears, 1)
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
        texts.append(TokenCopyResult.copiedWithoutAutoClear.description)
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
        // .copied (not .copiedWithoutAutoClear) also shows that writing to prepared contents keeps changeCount.
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
