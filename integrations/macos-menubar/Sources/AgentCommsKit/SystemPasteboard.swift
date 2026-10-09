#if canImport(AppKit)
import AppKit

/// `TokenPasteboard` over an `NSPasteboard` (the general pasteboard by default; tests use a private named one).
public final class SystemPasteboard: TokenPasteboard {
    private let pasteboard: NSPasteboard

    public init(_ pasteboard: NSPasteboard = .general) {
        self.pasteboard = pasteboard
    }

    public var changeCount: Int { pasteboard.changeCount }

    /// Builds one item with the string and the marker types, then writes it in a single `writeObjects`, so a
    /// clipboard manager polling the pasteboard never sees the string without its concealed/transient markers.
    public func replaceContents(string: String, markerTypes: [String]) -> Bool {
        let item = NSPasteboardItem()
        guard item.setString(string, forType: .string) else { return false }
        for type in markerTypes {
            guard item.setData(Data(), forType: NSPasteboard.PasteboardType(type)) else { return false }
        }
        pasteboard.clearContents()
        return pasteboard.writeObjects([item])
    }

    public func clearContents() {
        pasteboard.clearContents()
    }
}
#endif
