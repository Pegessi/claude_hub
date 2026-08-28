// Set the macOS general pasteboard to an image file.
//
// Usage: clipboard_set_image <path> <type=PNG|TIFF|JPEG|GIF>
//
// We avoid AppleScript's `set the clipboard to (read POSIX file ...)` route.
// When osascript runs from a launchd-descended (daemonized) backend process,
// AppleScript's `read` / `POSIX file` operations come from StandardAdditions
// .osax, which is not loaded into the default script context inherited from
// launchd, yielding "«script» doesn't understand the read message (-1708)".
// Even when StandardAdditions loads, `read POSIX file` is gated by macOS
// TCC against the responsible application — daemonized bash ancestors are
// not in the Automation allowlist.
//
// JXA (osascript -l JavaScript) + ObjC bridge avoids the StandardAdditions
// dependency, but is fragile: NSPasteboard method dispatch on instance
// methods like `setDataForType:` was observed failing intermittently inside
// uvicorn's worker process even though the same JXA script runs cleanly
// outside it ("pb.setDataForType is not a function").
//
// A native Swift binary calls NSPasteboard directly with no TCC gating and
// no scripting-bridge fragility, at the cost of a one-time ~4s `swiftc`
// compile (cached after that).
import AppKit
import Foundation

guard CommandLine.arguments.count >= 2 else {
    fputs("usage: clipboard_set_image <path> [type=PNG|TIFF|JPEG|GIF]\n", stderr)
    exit(2)
}
let path = CommandLine.arguments[1]
let typeArg = CommandLine.arguments.count >= 3 ? CommandLine.arguments[2] : "PNG"

let url = URL(fileURLWithPath: path)
guard let data = try? Data(contentsOf: url) else {
    fputs("read_failed:\(path)\n", stderr)
    exit(3)
}

let pbType: NSPasteboard.PasteboardType
switch typeArg {
case "PNG":  pbType = .png
case "TIFF": pbType = .tiff
case "JPEG": pbType = NSPasteboard.PasteboardType("public.jpeg")
case "GIF":  pbType = NSPasteboard.PasteboardType("com.compuserve.gif")
default:
    fputs("unknown_type:\(typeArg)\n", stderr)
    exit(4)
}

let pb = NSPasteboard.general
pb.clearContents()
pb.declareTypes([pbType], owner: nil)
let ok = pb.setData(data, forType: pbType)
print(ok ? "OK" : "setData_returned_false")
exit(ok ? 0 : 5)
